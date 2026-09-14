"""作問支援システム API。

- 式・フェーズは app_config（database.get_config）から取得する。ハードコードしない。
- フェーズ1・3：classify / judge は動かしログに全記録するが、児童には「おくったよ」だけ返す
  （response_type / ai_message は記録しない＝表示していないものは記録しない）。
- フェーズ2：応答の種類（response_type: form / praise / prompt / done / talk / error）を状態機械
  （仕様 v2 2章）で決定論的に決め、ai_dialogue に声かけを組み立てさせる。AIには判定させない。
  状態（sessions に保持・フェーズスコープ）：produced（到達構造の集合）、declared / declared_by（予告）、
  stuck（新構造に到達しなかった成立作問の連続回数）、miss（予告不一致の累積回数）。
  不成立・対話・再送ではカウンタを動かさない。新構造到達で stuck / miss を両方 0 に戻す。
- 管理画面（/admin, /admin/api/*）は HTTP Basic 認証（ADMIN_PASSWORD）。未設定なら起動しない。
- API 不通時の扱い：judge が規定回数リトライしても応答しなければ、その作問を「判定保留」として受理し
  （一覧に載せる・構造は空のまま）、児童には「おくれたよ！先生があとで読むね」を返す。再送要求は出さない。
  同じ本文の連続再送は API を呼ばず直前の結果を返す（input_type='resend'）。
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
import llm_call

if not config.ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD が未設定です。ローカルは .env に、本番は Render の Environment に設定してください。"
    )

STRUCTURES = {"tobun", "hougan", "bai"}
# judge が API 不通で判定できなかったときの児童向け表示（受理はする。再送は求めない）
JUDGE_PENDING_MESSAGE = "おくれたよ！ 先生があとで読むね。つぎのお話も作ってみよう。"
USER_ID_PATTERN = re.compile(r"^[0-9]{2}$")
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


class SessionRequest(BaseModel):
    session_id: int
    user_id: str


class JudgeRequest(BaseModel):
    session_id: int
    user_id: str
    message: str


class DeclareRequest(BaseModel):
    session_id: int
    user_id: str
    text: str


class PhaseRequest(BaseModel):
    phase: int


class ExpressionsRequest(BaseModel):
    expression_a: str
    expression_b: str


# ===== 共通ヘルパ =====

def _normalize_user_id(user_id: str) -> str:
    uid = (user_id or "").strip().lower()
    if not USER_ID_PATTERN.match(uid):
        raise HTTPException(status_code=400, detail="出席番号は数字2桁で入力してください")
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


def _enter_payload(session: dict, cfg: dict) -> dict:
    """ログイン／再入場時にクライアントへ返す一式。フェーズ1・3では支援に関わる情報を伏せる。

    フェーズ・式はセッションのもの（＝児童の画面に出ているもの）。"""
    session_id, phase, user_id = session["session_id"], session["phase"], session["user_id"]
    show = phase == 2
    history = database.get_produced(user_id, phase)
    return {
        "user_id": user_id,
        "session_id": session_id,
        "phase": phase,
        "expression": config.normalize_expression(database.expression_for_phase(cfg, phase)),
        "show_support": show,
        "history": history if show else [],
        "problems": ([{"text": p["text"], "structure": p["structure"]} for p in database.get_valid_problems(session_id)]
                     if show else []),
        "conversation": database.get_conversation(session_id),
        "ui_strength": database.get_max_prompt_strength(session_id) if show else 0,
        "all_reached": show and set(history) >= STRUCTURES,
        "declared": session["declared"] if show else None,
        "declared_by": session["declared_by"] if show else None,
        "poll_seconds": config.CONFIG_POLL_SECONDS,
    }


def _enter_current(user_id: str) -> dict:
    """現在のフェーズに対応するセッションを探し（無ければ作り）、入場情報を返す。"""
    cfg = database.get_config()
    phase = cfg["current_phase"]
    expression = database.expression_for_phase(cfg, phase)
    session_id, _created = database.find_or_create_session(user_id, phase, expression)
    _touch(user_id, session_id)
    return _enter_payload(database.get_session(session_id), cfg)


# ===== 状態機械（仕様 v2 2章） =====

def decide_strength(stuck: int, miss: int) -> int:
    """予告支援の強さ（0=なし／1=弱／2=中／3=強）。仕様 v2 2-4 のとおり。"""
    from_stuck = 0 if stuck <= 1 else (1 if stuck == 2 else (2 if stuck == 3 else 3))
    from_miss = 0 if miss == 0 else (2 if miss == 1 else 3)
    return max(from_stuck, from_miss)


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
def resume_session(req: SessionRequest):
    user_id = _normalize_user_id(req.user_id)
    session = _owned_session(req.session_id, user_id)
    _touch(user_id, req.session_id)
    return _enter_payload(session, database.get_config())


@app.post("/api/session/end")
def end_session(req: SessionRequest):
    """ログアウト。セッションの終了時刻を打つ（入り直せば再開＝終了時刻は消える）。"""
    user_id = _normalize_user_id(req.user_id)
    _owned_session(req.session_id, user_id)
    database.end_session(req.session_id)
    return {"ok": True}


@app.post("/api/judge")
def judge(req: JudgeRequest):
    t_start = time.perf_counter()
    user_id = _normalize_user_id(req.user_id)
    session = _owned_session(req.session_id, user_id)
    _touch(user_id, req.session_id)

    # フェーズはセッションのもの（＝児童の画面に出ているもの）。切替直後に旧画面から届いた送信を
    # 新フェーズとして記録しない（chat_logs.phase と sessions.phase を常に一致させる）。
    phase = session["phase"]
    cfg = database.get_config()
    expression = config.normalize_expression(database.expression_for_phase(cfg, phase))
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    produced = database.get_produced(user_id, phase)
    last = database.get_last_turn(req.session_id)
    ctx = dict(session=session, phase=phase, expression=expression, produced=produced,
               recent=database.get_recent_turns(req.session_id), last=last,
               counts={"stuck": session["stuck_count"], "miss": session["miss_count"]},
               t_start=t_start)

    # 同じ本文の連続再送（API 不通時に児童が送り直すのが典型）は API を呼ばず直前の結果を返す
    if _is_resend(message, last):
        return _finish(_handle_resend(req, user_id, message, ctx), user_id, ctx)

    input_kind = ai_classify.classify(message, ctx["recent"], expression, user_id=user_id)
    if input_kind == "sakumon":
        result = _handle_sakumon(req, user_id, message, ctx)
    else:
        result = _handle_taiwa(req, user_id, message, ctx)
    return _finish(result, user_id, ctx)


@app.post("/api/declare")
def declare(req: DeclareRequest):
    """予告（弱）：「つぎは何を求める問題にするか」の自由記述を受け取り、構造に分類して declared に立てる。

    unknown なら declared は立てない（再質問もしない）。フェーズ2以外は受け付けない。
    input_type='declaration' で記録する。カウンタは動かさない。"""
    t_start = time.perf_counter()
    user_id = _normalize_user_id(req.user_id)
    session = _owned_session(req.session_id, user_id)
    _touch(user_id, req.session_id)
    if session["phase"] != 2:
        raise HTTPException(status_code=400, detail="予告はフェーズ2でだけ受け付けます")
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    cfg = database.get_config()
    expression = config.normalize_expression(database.expression_for_phase(cfg, 2))

    kind = ai_classify.classify_declaration(text, user_id=user_id)
    declared = kind if kind in STRUCTURES else None
    declared_by = "child" if declared else None
    if declared:
        database.set_state(req.session_id, declared=declared, declared_by="child",
                           stuck_count=session["stuck_count"], miss_count=session["miss_count"])
    produced = database.get_produced(user_id, 2)
    database.save_log(
        session_id=req.session_id, user_id=user_id, phase=2, expression=expression,
        input_type="declaration", message=text, ai_message=None,
        declared_structure=declared, declared_by=declared_by,
        produced_structures=produced, stuck_count=session["stuck_count"], miss_count=session["miss_count"],
        latency_ms=int((time.perf_counter() - t_start) * 1000),
    )
    return {"declared": declared, "declared_by": declared_by, "classified": kind,
            "phase": 2, "show_support": True, "history": produced}


@app.get("/api/judge/status")
def judge_status(user_id: str):
    """送信中の児童が1秒間隔で見る。同時実行上限で待たされていれば "waiting"。"""
    if not USER_ID_PATTERN.match(user_id or ""):
        raise HTTPException(status_code=400, detail="bad user_id")
    return {"state": llm_call.wait_state(user_id)}


def _is_resend(message: str, last: dict | None) -> bool:
    """直前のターンと同じ本文 → 再送とみなす。"""
    return last is not None and (last.get("message") or "") == message


def _latency(ctx: dict) -> int:
    return int((time.perf_counter() - ctx["t_start"]) * 1000)


def _base_result(message: str | None, response_type: str | None, input_type: str, **extra) -> dict:
    return {
        "message": message, "response_type": response_type, "prompt_strength": None,
        "valid": None, "structure": None, "unknown": None, "is_new": False,
        "input_type": input_type, **extra,
    }


def _finish(result: dict, user_id: str, ctx: dict) -> dict:
    phase = ctx["phase"]
    show = phase == 2
    history = database.get_produced(user_id, phase)
    result["phase"] = phase
    result["show_support"] = show
    result["history"] = history if show else []
    result["ui_strength"] = database.get_max_prompt_strength(ctx["session"]["session_id"]) if show else 0
    result["all_reached"] = show and set(history) >= STRUCTURES
    state = database.get_session(ctx["session"]["session_id"])
    result["stuck_count"] = state["stuck_count"] if show else None
    result["miss_count"] = state["miss_count"] if show else None
    result.setdefault("declared", state["declared"] if show else None)
    result.setdefault("declared_by", state["declared_by"] if show else None)
    # accepted：フロントが「作った お話」一覧に追加するか。成立した作問と、判定保留で受理した作問が対象。
    # 再送（resend）は直前の応答を返すだけなので追加しない。
    result.setdefault("accepted", bool(result.get("valid")) and result.get("input_type") == "sakumon")
    if not show:
        # フェーズ1・3では判定結果を画面に出さない（ログには残っている）
        result["message"] = ai_dialogue.ACK_MESSAGE
        for k in ("valid", "structure", "unknown", "response_type", "prompt_strength",
                  "declared", "declared_by", "declaration_met"):
            result[k] = None
        result["is_new"] = False
        result["accepted"] = False
    return result


def _handle_resend(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """同一本文の再送：API を呼ばず直前の応答をそのまま返す（多重呼び出し防止）。

    ログは残す（再送の回数は計測したい）が、input_type="resend" として提出・成立・回数のどれにも
    数えない。response_type / ai_message は直前の値を写す。"""
    last, phase, counts = ctx["last"], ctx["phase"], ctx["counts"]
    result = _base_result(last.get("ai_message") or ai_dialogue.ACK_MESSAGE, last.get("response_type"), "resend",
                          accepted=False)
    result.update({"valid": last.get("valid"), "structure": last.get("structure"),
                   "unknown": last.get("unknown"), "prompt_strength": last.get("prompt_strength")})
    database.save_log(
        session_id=req.session_id, user_id=user_id, phase=phase, expression=ctx["expression"],
        input_type="resend", message=message, ai_message=last.get("ai_message"),
        response_type=last.get("response_type"), prompt_strength=last.get("prompt_strength"),
        produced_structures=ctx["produced"], stuck_count=counts["stuck"], miss_count=counts["miss"],
        latency_ms=None,
    )
    return result


def _handle_taiwa(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """対話経路：judge は通さない。フェーズ1・3は「おくったよ」。

    困り表明（「わからない」「どうしたら」等）は stuck_count に数える。応答は talk
    （LLM が困りに対しても具体的な手がかりを1つ出して作問にもどす）。
    """
    phase, produced, counts = ctx["phase"], ctx["produced"], ctx["counts"]
    show = phase == 2

    ai_message = None
    if show:
        dlg = ai_dialogue.dialogue(message, "taiwa", None, produced, ctx["recent"], "talk",
                                   ctx["expression"], user_id=user_id)
        ai_message = dlg["message"]

    result = _base_result(ai_message, "talk" if show else None, "taiwa")
    result["prompt_strength"] = 0 if show else None
    database.save_log(
        session_id=req.session_id, user_id=user_id, phase=phase, expression=ctx["expression"],
        input_type="taiwa", message=message, ai_message=ai_message,
        response_type="talk" if show else None, prompt_strength=0 if show else None,
        produced_structures=produced, stuck_count=counts["stuck"], miss_count=counts["miss"],
        latency_ms=_latency(ctx),
    )
    return result


def _handle_sakumon(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """作問経路：ai_judge で同定 → 状態機械（仕様 v2 2-5）で応答の種類と強さを決定 → ai_dialogue が声かけ。

    カウンタの規則（2-3）：
      不成立            → stuck / miss は変化しない
      成立・新構造      → produced に追加、stuck = 0、miss = 0
      成立・既出構造    → stuck += 1
      予告あり・一致    → declaration_met = 1、miss = 0
      予告あり・不一致  → declaration_met = 0、miss += 1
    状態機械はフェーズ2でだけ動く。フェーズ1・3は判定だけ記録する（カウンタも動かさない）。
    """
    phase, expression, produced, counts = ctx["phase"], ctx["expression"], ctx["produced"], ctx["counts"]
    session = ctx["session"]
    show = phase == 2
    jr = ai_judge.judge(message, expression, user_id=user_id)

    # 技術的失敗（規定回数リトライしても API が応答しない）→ 判定保留として「受理」する。
    # 児童の活動を止めないことを最優先にし、一覧には載せる・構造は空のまま・再送は求めない。
    # 児童の責任ではないので miss_count にも数えない。
    if jr.get("issue") == "error":
        ai_message = JUDGE_PENDING_MESSAGE if show else None
        result = _base_result(ai_message, "error" if show else None, "sakumon", accepted=True,
                              prompt_strength=0 if show else None)
        database.save_log(
            session_id=req.session_id, user_id=user_id, phase=phase, expression=expression,
            input_type="sakumon", message=message, ai_message=ai_message,
            valid=None, issue="pending", is_new=False,
            response_type="error" if show else None, prompt_strength=0 if show else None,
            produced_structures=produced, stuck_count=counts["stuck"], miss_count=counts["miss"],
            latency_ms=_latency(ctx),
        )
        return result

    valid = bool(jr["valid"])
    structure = jr["structure"] if valid else None
    unknown = jr["unknown"]
    issue = jr["issue"]
    is_new = bool(valid and structure in STRUCTURES and structure not in produced)
    produced_after = list(produced) + ([structure] if is_new else [])
    all_before = set(produced) >= STRUCTURES
    completes_all = is_new and set(produced_after) >= STRUCTURES

    # ---- 状態機械（フェーズ2のみ。不成立は何も動かさない） ----
    stuck, miss = counts["stuck"], counts["miss"]
    declared, declared_by = session["declared"], session["declared_by"]
    declared_used, declared_by_used, met = None, None, None
    strength = None
    response_type = None
    if show and valid:
        if declared:
            declared_used, declared_by_used = declared, declared_by
            met = structure == declared
            miss = 0 if met else miss + 1
        if is_new:
            stuck, miss = 0, 0
        else:
            stuck += 1
        # 予告はこの作問で消費される（2-5）。中・強はこのあとシステムが新しい目標を立てる
        declared, declared_by = None, None
        if completes_all or all_before:
            response_type, strength = "done", 0
        else:
            strength = decide_strength(stuck, miss)
            if strength == 0:
                response_type = "praise"
            else:
                response_type = "prompt"
                if strength >= 2:
                    # 中・強：produced に含まれない構造を固定順で1つ指定（自己ラベルは使わない）
                    declared = ai_dialogue.pick_unreached_structure(produced_after)
                    declared_by = "system" if declared else None
        database.set_state(req.session_id, declared=declared, declared_by=declared_by,
                           stuck_count=stuck, miss_count=miss)
    elif show:
        response_type, strength = "form", 0

    ai_message = None
    if show:
        dlg = ai_dialogue.dialogue(
            message, "sakumon", {**jr, "is_new": is_new, "completes_all": completes_all},
            produced, ctx["recent"], response_type, expression,
            first_done=completes_all, prompt_strength=strength, user_id=user_id,
        )
        ai_message = dlg["message"]

    result = _base_result(ai_message, response_type, "sakumon")
    result.update({"valid": valid, "structure": structure, "unknown": unknown, "is_new": is_new,
                   "prompt_strength": strength, "declaration_met": met,
                   "declared": declared, "declared_by": declared_by})
    database.save_log(
        session_id=req.session_id, user_id=user_id, phase=phase, expression=expression,
        input_type="sakumon", message=message, ai_message=ai_message,
        valid=valid, structure=structure, unknown=unknown, issue=issue, is_new=is_new,
        response_type=response_type, prompt_strength=strength,
        declared_structure=declared_used, declared_by=declared_by_used, declaration_met=met,
        produced_structures=produced_after, stuck_count=stuck, miss_count=miss,
        latency_ms=_latency(ctx),
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


@admin.get("/api/live")
def admin_live():
    cfg = database.get_config()
    rows = database.admin_live_status(cfg["current_phase"])
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
