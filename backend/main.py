"""作問支援システム API。

- 式・フェーズは app_config（database.get_config）から取得する。ハードコードしない。
- フェーズ1・3：classify / judge は動かしログに全記録するが、児童には「おくったよ」だけ返す。
- フェーズ2：学習者状態（S0〜S3）と支援水準（form / level1〜3 / discover / goal / talk）を
  ここで決定論的に算出し、ai_dialogue に声かけを組み立てさせる。AIには判定させない。
  水準を上げるのは「成立作問の反復」と「対話での困り表明」の2つ（どちらも停滞のシグナル）。
- 管理画面（/admin, /admin/api/*）は HTTP Basic 認証（ADMIN_PASSWORD）。未設定なら起動しない。
"""

import csv
import io
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import database
import ai_judge
import ai_classify
import ai_dialogue

if not config.ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD が未設定です。ローカルは .env に、本番は Render の Environment に設定してください。"
    )

STRUCTURES = {"tobun", "hougan", "bai"}
LEVEL_NAMES = database.LEVEL_NAMES
MAX_LEVEL = len(LEVEL_NAMES)          # 水準3（場面想起）が上限
USER_ID_PATTERN = re.compile(r"^[0-9a-z]{2}$")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# 児童側の設定ポーリングを心拍として使う（user_id → 最終受信）。単一プロセス前提のメモリ保持。
_last_seen: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

class _RevalidatingStaticFiles(StaticFiles):
    """静的ファイルに Cache-Control: no-cache を付ける。

    既定では Cache-Control が付かず、ブラウザが独自の推測でキャッシュを再利用してしまう
    （＝デプロイしても古い index.js / admin.js が使われ続ける）。no-cache は
    「毎回サーバに確認する（変わっていなければ304で軽い）」の意味で、no-store ではない。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatingStaticFiles(directory=FRONTEND_DIR), name="static")


def _asset_version() -> str:
    """frontend/ の js・css の最終更新時刻。HTMLの `?v=__V__` に埋め込む。

    デプロイのたびに値が変わるので、ブラウザに残っている古いキャッシュを
    確実に切れる（URLが変わる＝別ファイル扱いになる）。
    """
    try:
        mtimes = [f.stat().st_mtime for f in FRONTEND_DIR.iterdir() if f.suffix in (".js", ".css")]
        return str(int(max(mtimes))) if mtimes else "0"
    except OSError:
        return "0"


def _html_page(filename: str) -> HTMLResponse:
    """HTML本体はキャッシュさせない（中の ?v= を必ず最新にするため）。"""
    path = FRONTEND_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    html = path.read_text(encoding="utf-8").replace("__V__", _asset_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ===== Models =====

class LoginRequest(BaseModel):
    user_id: str


class NewSessionRequest(BaseModel):
    user_id: str


class ResumeSessionRequest(BaseModel):
    session_id: int
    user_id: str


class JudgeRequest(BaseModel):
    session_id: int
    user_id: str
    message: str
    button_pressed: str | None = None


class PhaseRequest(BaseModel):
    phase: int


class ExpressionsRequest(BaseModel):
    expression_a: str
    expression_b: str


# ===== 共通ヘルパ =====

def _normalize_user_id(user_id: str) -> str:
    uid = (user_id or "").strip().lower()
    if not USER_ID_PATTERN.match(uid):
        raise HTTPException(status_code=400, detail="学籍番号は半角英数字2桁で入力してください")
    return uid


def _touch(user_id: str, session_id: int | None):
    _last_seen[user_id] = {"session_id": session_id, "ts": time.time()}


def _cfg_public(cfg: dict) -> dict:
    phase = cfg["current_phase"]
    dividend, divisor = config.parse_expression(database.expression_for_phase(cfg, phase))
    return {
        "phase": phase,
        "expression": f"{dividend} ÷ {divisor}",
        "dividend": dividend,
        "divisor": divisor,
        "run_id": cfg["run_id"],
        "updated_at": cfg["updated_at"],
        "poll_seconds": config.CONFIG_POLL_SECONDS,
    }


def _owned_session(session_id: int, user_id: str) -> dict:
    """セッションの存在と所有権（user_id 一致）を検証する。"""
    session = database.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="このセッションはあなたのものではありません")
    return session


def _enter_payload(session_id: int, cfg: dict) -> dict:
    """ログイン／再入場時にクライアントへ返す一式。フェーズ1・3では支援に関わる情報を伏せる。"""
    session = database.get_session(session_id)
    phase = cfg["current_phase"]
    show = phase == 2
    history = database.get_history(session_id)
    return {
        "user_id": session["user_id"],
        "session_id": session_id,
        "phase": phase,
        "run_id": cfg["run_id"],
        "expression": _cfg_public(cfg)["expression"],
        "show_support": show,
        "history": history if show else [],
        "problems": ([{"text": p["text"], "structure": p["structure"]} for p in database.get_valid_problems(session_id)]
                     if show else []),
        "conversation": database.get_conversation(session_id),
        "ui_level": database.get_max_level(session_id, 2) if show else 0,
        "all_reached": show and set(history) >= STRUCTURES,
        "poll_seconds": config.CONFIG_POLL_SECONDS,
    }


def _enter_current(user_id: str) -> dict:
    """現在の run・フェーズに対応するセッションを探し（無ければ作り）、入場情報を返す。"""
    cfg = database.get_config()
    expression = database.expression_for_phase(cfg, cfg["current_phase"])
    session_id, _created = database.find_or_create_session(user_id, cfg["run_id"], cfg["current_phase"], expression)
    _touch(user_id, session_id)
    return _enter_payload(session_id, cfg)


# ===== 学習者状態（決定論） =====

def _learner_state(session_id: int, current_valid: bool | None, history_after: set,
                   is_new: bool, phase: int, last_turn: dict | None) -> str:
    """S0〜S3。S2/S3 の区別はリアルタイムには暫定値（事後にログから判定する）。

    S0: 今回の作問が不成立で、直前2問のうち1問以上が不成立（＝直近2問連続不成立、または直近3問中2問不成立）。
        今回が成立なら必ず脱出（構造支援へ）。対話ターンは直近の作問提出の並びで判定する。
    S1: 到達構造が1以下。  S2: 2以上。
    S3: 2以上で、フェーズ2の水準1以上の声かけ直後の提出で新構造が出たことがある（暫定）。
    """
    if current_valid is False:
        recent = database.get_recent_sakumon_validity(session_id, 2)
        if any(not v for v in recent):
            return "S0"
    elif current_valid is None:
        recent = database.get_recent_sakumon_validity(session_id, 3)
        if len(recent) >= 2 and not recent[0] and not recent[1]:
            return "S0"
        if len(recent) >= 3 and sum(1 for v in recent if not v) >= 2:
            return "S0"
    if len(history_after) <= 1:
        return "S1"
    if database.has_intentional_evidence(session_id):
        return "S3"
    if is_new and phase == 2 and last_turn and last_turn.get("support_level") in LEVEL_NAMES:
        return "S3"
    return "S2"


# ===== 水準1の問い（同じ？ちがう？）への応答検出 =====

_DIFF_RE = re.compile(r"ちがう|違う|ちがい|違い|べつ|別")
_SAME_RE = re.compile(r"同じ|おなじ|おんなじ|いっしょ|一緒")
_NEG_RE = re.compile(r"じゃない|ではない|くない|ない")


def _answer_kind(text: str, button: str | None) -> str | None:
    """「同じ」「ちがう」のどちらの応答か。送信テキストを優先し、決められなければボタン値。"""
    t = (text or "").strip()
    if _DIFF_RE.search(t) or (_SAME_RE.search(t) and _NEG_RE.search(t)):
        return "different"
    if _SAME_RE.search(t):
        return "same"
    if button == "同じ":
        return "same"
    if button == "ちがう":
        return "different"
    return None


# ===== 困り表明の検出 =====

# 児童が「進めない」と言葉で訴えている状態。作問の提出と同じく停滞のシグナルとして扱い、
# 支援水準を1段上げる（talk のままだと「自分で考えてみよう」「少し休んでいいよ」を繰り返すだけで
# 前に進まないため）。「つくれません」「作れない」は水準2の問いへの答えとして最も出やすいので、
# ここで確実に拾う。
_STUCK_RE = re.compile(
    r"わからな|わかんな|分からな|わからん|わかりませ|"
    r"むずかし|難し|できな|できませ|むり|無理|"
    r"つくれ(な|ま[せし])|作れ(な|ま[せし])|かけな|書けな|"
    r"どうしたら|どうすれば|どうやって|どうすると|どうする|どうしよう|どういう|"
    r"思いつか|おもいつか|うかばな|浮かばな|"
    r"ヒント|たすけて|助けて|おしえて|教えて|こまった|困った|"
    r"[なに何](を|に)(すれ|したら|書|かけ|かい)"
)


def _is_stuck(text: str) -> bool:
    return bool(_STUCK_RE.search((text or "").strip()))


def _stuck_support_level(session_id: int, history: list, learner_state: str,
                         has_problems: bool) -> str:
    """困り表明を受けたときに返す支援水準。

    3構造そろっていれば上げる先がないので talk のまま。
    まだ1問も成立していない／不成立が続いている（S0）ときは、産出の比較（水準1）も
    産出の作り直し（水準3）も成り立たないので、場面想起の水準3へ直行する
    （水準3は産出の有無で中身が変わる。ai_dialogue.dialogue 参照）。
    それ以外は作問の反復と同じく1段上げる。
    """
    if set(history) >= STRUCTURES:
        return "talk"
    if not has_problems or learner_state == "S0":
        return "level3"
    return f"level{min(MAX_LEVEL, database.get_current_level(session_id, 2) + 1)}"


# ===== Routes（児童） =====

@app.get("/api/config")
def get_public_config(user_id: str | None = None, session_id: int | None = None):
    """現在のフェーズと式（認証不要）。児童側はこれを5秒間隔でポーリングする（心拍にもなる）。"""
    if user_id and USER_ID_PATTERN.match(user_id):
        _touch(user_id, session_id)
    return _cfg_public(database.get_config())


@app.post("/api/login")
def login(req: LoginRequest):
    """出席番号でログイン。現在のフェーズに対応するセッションを探すか作って返す。"""
    return _enter_current(_normalize_user_id(req.user_id))


@app.post("/api/session/new")
def new_session(req: NewSessionRequest):
    """フェーズ切替時にクライアントが呼ぶ。現在フェーズのセッションを探すか作る（重複作成しない）。"""
    return _enter_current(_normalize_user_id(req.user_id))


@app.post("/api/session/resume")
def resume_session(req: ResumeSessionRequest):
    user_id = _normalize_user_id(req.user_id)
    _owned_session(req.session_id, user_id)
    cfg = database.get_config()
    _touch(user_id, req.session_id)
    return _enter_payload(req.session_id, cfg)


@app.post("/api/judge")
def judge(req: JudgeRequest):
    user_id = _normalize_user_id(req.user_id)
    session = _owned_session(req.session_id, user_id)
    _touch(user_id, req.session_id)

    cfg = database.get_config()
    phase = cfg["current_phase"]
    expression = database.expression_for_phase(cfg, phase)
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")
    button = (req.button_pressed or "").strip() or None

    history = database.get_history(req.session_id)
    recent = database.get_recent_turns(req.session_id)
    last = database.get_last_turn(req.session_id)
    ctx = dict(session=session, phase=phase, expression=expression, history=history,
               recent=recent, last=last, button=button)

    # 水準1の問いに、ボタン押下＋短い応答で答えた場合は classify を通さない
    if phase == 2 and button in ai_dialogue.COMPARE_BUTTONS and len(message) <= 8:
        kind = _answer_kind(message, button)
        if kind:
            result = _handle_compare_answer(req, user_id, message, kind, ctx)
            return _finish(result, req.session_id, phase)

    input_kind = ai_classify.classify(message, recent, expression)
    if input_kind == "sakumon":
        result = _handle_sakumon(req, user_id, message, ctx)
    else:
        kind = None
        if phase == 2 and last and last.get("support_level") == "level1":
            kind = _answer_kind(message, button)
        if kind:
            result = _handle_compare_answer(req, user_id, message, kind, ctx)
        else:
            result = _handle_taiwa(req, user_id, message, ctx)
    return _finish(result, req.session_id, phase)


def _finish(result: dict, session_id: int, phase: int) -> dict:
    show = phase == 2
    history = database.get_history(session_id)
    result["phase"] = phase
    result["show_support"] = show
    result["history"] = history if show else []
    result["ui_level"] = database.get_max_level(session_id, 2) if show else 0
    result["all_reached"] = show and set(history) >= STRUCTURES
    if not show:
        # フェーズ1・3では判定結果を画面に出さない（ログには残っている）
        for k in ("valid", "structure", "unknown", "is_new", "buttons", "target_structure"):
            result[k] = None
        result["highlight_problems"] = False
    return result


def _base_result(message: str, display_type: str, input_type: str, **extra) -> dict:
    return {
        "valid": None, "structure": None, "unknown": None, "is_new": False,
        "display_type": display_type, "message": message, "buttons": None,
        "figure": None, "tape_diagram": None, "target_structure": None,
        "highlight_problems": False,
        "state": None, "input_type": input_type, **extra,
    }


def _handle_compare_answer(req: JudgeRequest, user_id: str, message: str, kind: str, ctx: dict) -> dict:
    """水準1の問い「同じ？ちがう？」への応答。
    同じ → 気づけている：水準は上げず挑戦を促す。 ちがう → 気づけていない：即時に水準2へ。"""
    target = None
    if kind == "same":
        support_level, text = "level1", ai_dialogue.compare_same_reply()
    else:
        support_level = "level2"
        target = ai_dialogue.pick_unreached_structure(ctx["history"])
        text = ai_dialogue.unknown_hint_message(target)
    learner_state = _learner_state(req.session_id, None, set(ctx["history"]), False, ctx["phase"], ctx["last"])
    result = _base_result(text, support_level, "taiwa", state=f"compare_answer_{kind}",
                          target_structure=target)
    database.save_log(
        session_id=req.session_id, user_id=user_id, message=message, response_json=result,
        structure=None, is_new=False, input_type="taiwa",
        phase=ctx["phase"], support_level=support_level, learner_state=learner_state,
        unknown=None, issue=None, button_pressed=ctx["button"],
        stall_count=database.get_stall_count(req.session_id), target_structure=target,
        expression=ctx["expression"],
    )
    return result


def _handle_taiwa(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """対話経路：judge は通さない。フェーズ1・3は「おくったよ」。

    フェーズ2では、困り表明（「わからない」「どうしたら」等）を作問の反復と同じ停滞シグナルとして
    扱い、支援水準を1段上げて構造支援を返す。それ以外のつぶやき・質問は従来どおり talk。
    """
    phase, history = ctx["phase"], ctx["history"]
    learner_state = _learner_state(req.session_id, None, set(history), False, phase, ctx["last"])

    problems = [{"text": p["text"], "structure": p["structure"]}
                for p in database.get_valid_problems(req.session_id)]
    target = None
    if phase != 2:
        support_level, display_type = "none", "ack"
    elif _is_stuck(message):
        support_level = _stuck_support_level(req.session_id, history, learner_state, bool(problems))
        display_type = "normal" if support_level == "talk" else support_level
        if support_level in ("level2", "level3"):
            target = ai_dialogue.pick_unreached_structure(history)
    else:
        support_level, display_type = "talk", "normal"

    dlg = ai_dialogue.dialogue(message, "taiwa", None, history, ctx["recent"], support_level,
                               ctx["expression"], problems=problems, target_structure=target)
    result = _base_result(dlg["message"], display_type, "taiwa", state=dlg.get("state"))
    result.update({"buttons": dlg.get("buttons"), "target_structure": target,
                   "highlight_problems": bool(dlg.get("highlight_problems"))})
    database.save_log(
        session_id=req.session_id, user_id=user_id, message=message, response_json=result,
        structure=None, is_new=False, input_type="taiwa",
        phase=phase, support_level=support_level, learner_state=learner_state,
        unknown=None, issue=None, button_pressed=ctx["button"],
        stall_count=database.get_stall_count(req.session_id), target_structure=target,
        expression=ctx["expression"],
    )
    return result


def _handle_sakumon(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """作問経路：ai_judge で同定 → サーバが学習者状態・支援水準を決定 → ai_dialogue が声かけ。"""
    phase, expression, history, last = ctx["phase"], ctx["expression"], ctx["history"], ctx["last"]
    show = phase == 2
    jr = ai_judge.judge(message, expression)

    # 技術的失敗（リトライ後もパース不可）→ 児童向けフォールバック。児童の責任ではないので S0 判定にも数えない。
    if jr.get("issue") == "error":
        learner_state = _learner_state(req.session_id, None, set(history), False, phase, last)
        result = _base_result(ai_dialogue.FALLBACK_MESSAGE if show else ai_dialogue.ACK_MESSAGE,
                              "normal" if show else "ack", "sakumon", state="judge_error")
        database.save_log(
            session_id=req.session_id, user_id=user_id, message=message, response_json=result,
            structure=None, is_new=False, input_type="sakumon",
            phase=phase, support_level="error", learner_state=learner_state,
            unknown=None, issue="error", button_pressed=ctx["button"],
            stall_count=None, target_structure=None, expression=expression,
        )
        return result

    valid = jr["valid"]
    structure = jr["structure"] if valid else None
    unknown = jr["unknown"]
    issue = jr["issue"]
    is_new = bool(valid and structure in STRUCTURES and structure not in history)
    all_before = set(history) >= STRUCTURES
    completes_all = is_new and (set(history) | {structure}) >= STRUCTURES
    stall_count = (database.get_stall_count(req.session_id) + 1) if (valid and not is_new) else 0
    history_after = set(history) | ({structure} if valid else set())
    learner_state = _learner_state(req.session_id, valid, history_after, is_new, phase, last)

    target = None
    if not show:
        support_level, display_type = "none", "ack"
    elif not valid:
        support_level, display_type = "form", "normal"          # 水準0（S0は構造支援に進まない）
    elif completes_all or all_before:
        support_level, display_type = "goal", "goal"
    elif is_new:
        support_level, display_type = "discover", "new_structure"  # 水準を0にリセット（次の反復が level1）
    else:
        level = min(MAX_LEVEL, database.get_current_level(req.session_id, 2) + 1)  # 1提出につき最大1段階
        support_level = display_type = f"level{level}"
        if level >= 2:   # 水準2・3は「まだ聞いていない求める量」を1つ選んで使う
            target = ai_dialogue.pick_unreached_structure(history)

    problems = [{"text": p["text"], "structure": p["structure"]} for p in database.get_valid_problems(req.session_id)]
    if valid:
        problems.append({"text": message, "structure": structure})

    dlg = ai_dialogue.dialogue(
        message, "sakumon", {**jr, "is_new": is_new, "completes_all": completes_all},
        history, ctx["recent"], support_level, expression,
        problems=problems, target_structure=target, first_goal=completes_all,
    )

    result = _base_result(dlg["message"], display_type, "sakumon", state=dlg.get("state"))
    result.update({
        "valid": valid, "structure": structure, "unknown": unknown, "is_new": is_new,
        "buttons": dlg.get("buttons"), "target_structure": target,
        "highlight_problems": bool(dlg.get("highlight_problems")),
    })
    database.save_log(
        session_id=req.session_id, user_id=user_id, message=message, response_json=result,
        structure=structure, is_new=is_new, input_type="sakumon",
        phase=phase, support_level=support_level, learner_state=learner_state,
        unknown=unknown, issue=issue, button_pressed=ctx["button"],
        stall_count=stall_count, target_structure=target, expression=expression,
    )
    return result


@app.get("/")
def index():
    return _html_page("index.html")


# ===== 管理者（HTTP Basic 認証） =====

_security = HTTPBasic(realm="sakumon-admin")


def require_admin(credentials: HTTPBasicCredentials = Depends(_security)):
    ok = secrets.compare_digest(credentials.password.encode("utf-8"), config.ADMIN_PASSWORD.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="認証に失敗しました",
                            headers={"WWW-Authenticate": 'Basic realm="sakumon-admin"'})
    return True


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@admin.get("")
def admin_page():
    return _html_page("admin.html")


@admin.get("/api/config")
def admin_config():
    cfg = database.get_config()
    return {**cfg, "public": _cfg_public(cfg)}


@admin.post("/api/phase")
def admin_set_phase(req: PhaseRequest):
    if req.phase not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="phase は 1 / 2 / 3")
    cfg = database.set_phase(req.phase)
    return {**cfg, "public": _cfg_public(cfg)}


@admin.post("/api/expressions")
def admin_set_expressions(req: ExpressionsRequest):
    try:
        a = config.normalize_expression(req.expression_a)
        b = config.normalize_expression(req.expression_b)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cfg = database.set_expressions(a, b)
    return {**cfg, "public": _cfg_public(cfg)}


@admin.post("/api/new_run")
def admin_new_run():
    cfg = database.start_new_run()
    _last_seen.clear()
    return {**cfg, "public": _cfg_public(cfg)}


@admin.get("/api/live")
def admin_live():
    cfg = database.get_config()
    rows = database.admin_live_status(cfg["run_id"], cfg["current_phase"])
    now = time.time()
    for r in rows:
        seen = _last_seen.get(r["user_id"])
        r["online"] = bool(seen and now - seen["ts"] <= config.ONLINE_WINDOW_SECONDS)
        r["last_seen_seconds"] = int(now - seen["ts"]) if seen else None
    return {"config": _cfg_public(cfg), "students": rows}


@admin.get("/api/students")
def admin_students():
    return database.admin_get_all_students()


@admin.get("/api/students/{user_id}")
def admin_student_detail(user_id: str):
    return {"user_id": user_id, "sessions": database.admin_get_student_sessions(user_id)}


@admin.get("/api/sessions/{session_id}")
def admin_session_logs(session_id: int):
    return database.admin_get_session_logs(session_id)


@admin.delete("/api/sessions/{session_id}")
def admin_delete_session(session_id: int):
    database.admin_delete_session(session_id)
    return {"ok": True}


@admin.delete("/api/logs/{log_id}")
def admin_delete_log(log_id: int):
    database.admin_delete_log(log_id)
    return {"ok": True}


@admin.get("/api/export/csv")
def admin_export_csv():
    rows = database.admin_get_all_logs_csv()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=database.CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]),  # BOM 付き（Excel で開いたときの文字化け防止）
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sakumon_export.csv"},
    )


app.include_router(admin)
