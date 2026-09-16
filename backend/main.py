"""作問支援システム API。

- フェーズは app_config（database.get_config）から取得する。ハードコードしない。
- 式は児童（出席番号の奇偶）とフェーズで決まる（EXPRESSION_ASSIGNMENT。仕様 v2 4章）。
  セッション開始時に決めて sessions.expression に保存し、途中で変えない。管理画面から個別に上書きできる。
- フェーズ1・3：classify / judge は動かしログに全記録するが、児童には「おくったよ」だけ返す
  （response_type / ai_message は記録しない＝表示していないものは記録しない）。
- フェーズ2：応答の種類（response_type: form / praise / prompt / done / talk / error）を状態機械
  （仕様 v2 2章）で決定論的に決め、ai_dialogue に文言を組み立てさせる。AIには判定させない。
  状態（sessions に保持・フェーズスコープ）：produced（到達構造の集合）、declared / declared_by（予告）、
  stuck（新構造に到達しなかった成立作問の連続回数）、miss（予告不一致の累積回数）、
  help（taiwa が支援要求に分類された回数）、strength（現在の強度 0〜3。decide_strength で遷移）。
  不成立・再送ではカウンタを動かさない。新構造到達で3カウンタと強度を全て 0 に戻す。
- 弱（強度1）の予告は、児童の画面では作問と同じ入力欄から送る（入力欄は1つ）。予告待ちの状態で届いた
  入力は、classify が作問なら通常の作問処理、そうでなければ予告として分類する（/api/judge の declaring）。
- 中（強度2）は引用＋3択の自己ラベル（/api/self_label）。児童が答えずに作問を送ってきたら
  対話は打ち切り、通常の作問処理に進む（予告は立ったまま）。自己ラベルが判定と違っても訂正しない。
- 弱・中の引用文（直前の成立作問の問いの文）は ai_dialogue.extract_question（LLM）。取れなければ定型文。
- 管理画面（/admin, /admin/api/*）は HTTP Basic 認証（ADMIN_PASSWORD）。未設定なら起動しない。
- judge が API 不通：response_type='error'・issue='error'。児童には「もう一度 おくって みてね」（3-2）。
  一覧には載せない。同じ本文を送り直したら判定し直す（すでに判定済みの本文の再送だけ input_type='resend' で
  API を呼ばず直前の結果を返す。カウンタは動かさない）。
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
USER_ID_PATTERN = re.compile(r"^[0-9]{2}$")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# 式のカウンターバランス（仕様 v2 4章）。出席番号の奇偶 × フェーズ。後から1箇所で変更できるようここに置く。
# 24÷4 を使わない理由：9/1 の紙の調査が全員 24÷4 で、練習効果が交絡するため。
EXPRESSION_ASSIGNMENT = {
    "odd":  {1: "24÷6", 2: "24÷8", 3: "24÷3"},
    "even": {1: "24÷3", 2: "24÷8", 3: "24÷6"},
}

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
    """frontend/ の js・css の最終更新時刻。HTMLの `?v=__V__` に埋め込む。"""
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
    declaring: bool = False    # 予告待ち（弱の直後）の入力。作問でなければ予告として扱う


class DeclareRequest(BaseModel):
    session_id: int
    user_id: str
    text: str


class SelfLabelRequest(BaseModel):
    session_id: int
    user_id: str
    choice: str                  # tobun / hougan / bai


class PhaseRequest(BaseModel):
    phase: int


class SessionExpressionRequest(BaseModel):
    expression: str


# ===== 共通ヘルパ =====

def _normalize_user_id(user_id: str) -> str:
    uid = (user_id or "").strip().lower()
    if not USER_ID_PATTERN.match(uid):
        raise HTTPException(status_code=400, detail="出席番号は数字2桁で入力してください")
    return uid


def _touch(user_id: str, session_id: int | None):
    _last_seen[user_id] = {"session_id": session_id, "ts": time.time()}


def assigned_expression(user_id: str, phase: int) -> str:
    """出席番号の奇偶とフェーズから式を決める（'24 ÷ 6' の形）。"""
    group = database.parity_group_of(user_id)
    return config.normalize_expression(EXPRESSION_ASSIGNMENT[group][phase])


def _cfg_public(cfg: dict, session: dict | None = None) -> dict:
    """現在のフェーズ。式は児童ごとなので、セッションが分かるときだけ返す。"""
    out = {
        "phase": cfg["current_phase"],
        "expression": session["expression"] if session else None,
        "updated_at": cfg["updated_at"],
        "poll_seconds": config.CONFIG_POLL_SECONDS,
        "expression_assignment": {g: {str(p): config.normalize_expression(e) for p, e in d.items()}
                                  for g, d in EXPRESSION_ASSIGNMENT.items()},
    }
    return out


def _owned_session(session_id: int, user_id: str) -> dict:
    """セッションの存在と所有権（user_id 一致）を検証する。"""
    session = database.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="このセッションはあなたのものではありません")
    return session


def _pending_dialog(last: dict | None) -> str | None:
    """再入場時に、途中だった対話（予告入力／自己ラベル）を復元するための状態。

    declaration       … 弱の直後（入力欄を予告モードにする）
    self_label_choice … 中の直後（3択を待つ）
    """
    if not last:
        return None
    if last["input_type"] == "sakumon" and last.get("response_type") == "prompt":
        return {1: "declaration", 2: "self_label_choice"}.get(last.get("prompt_strength"))
    return None


def _support_state(session: dict, show: bool) -> dict:
    declared = session["declared"] if show else None
    return {
        "declared": declared,
        "declared_by": session["declared_by"] if show else None,
        "target_label": ai_dialogue.STRUCTURE_LABEL.get(declared) if declared else None,
        "stuck_count": session["stuck_count"] if show else None,
        "miss_count": session["miss_count"] if show else None,
        "help_count": session["help_count"] if show else None,
        "strength": session["strength"] if show else None,
    }


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
        "expression": session["expression"],
        "show_support": show,
        "history": history if show else [],
        "problems": ([{"text": p["text"], "structure": p["structure"]} for p in database.get_valid_problems(session_id)]
                     if show else []),
        "conversation": database.get_conversation(session_id),
        "all_reached": show and set(history) >= STRUCTURES,
        "dialog": _pending_dialog(database.get_last_turn(session_id)) if show else None,
        "choices": _choices(),
        **_support_state(session, show),
        "poll_seconds": config.CONFIG_POLL_SECONDS,
    }


def _choices() -> list[dict]:
    """中・ステップ2の3択（常に3つとも出す。到達済みかどうかで出し分けない）。"""
    return [{"value": s, "label": ai_dialogue.STRUCTURE_LABEL[s]} for s in ai_dialogue.STRUCTURE_ORDER]


def _enter_current(user_id: str) -> dict:
    """現在のフェーズに対応するセッションを探し（無ければ作り）、入場情報を返す。"""
    cfg = database.get_config()
    phase = cfg["current_phase"]
    session_id, _created = database.find_or_create_session(user_id, phase, assigned_expression(user_id, phase))
    _touch(user_id, session_id)
    return _enter_payload(database.get_session(session_id), cfg)


# ===== 状態機械 =====

MAX_STRENGTH = 3
TRIGGERS = ("stuck", "miss", "help", "none")


def decide_strength(prev: int, *, is_new: bool = False, stuck_after: int = 0,
                    stuck_up: bool = False, miss_up: bool = False, help_up: bool = False) -> tuple[int, str]:
    """支援の強度（0=促し／1=弱／2=中／3=強）の遷移。(新しい強度, strength_trigger) を返す。

    強度は sessions.strength に状態として保持し、カウンタから毎回計算し直さない（随伴的指導の原則：
    失敗で1段強め、成功で1段弱める。Wood & Middleton）。
      - 新構造到達（is_new）      → 0 に戻す（カウンタも呼び出し側で全て 0）
      - 強度 0 → 1               → stuck が 2 に達したときだけ（同じ構造を1回くり返しただけでは介入しない）
      - 強度 1 以上              → stuck / miss / help のいずれかが増えたターンごとに +1（上限 3）
    同じターンで stuck と miss が両方増えても +1 は1回（1回の失敗＝1段）。trigger は miss > stuck > help の
    優先で1つだけ記録する。強度が上がらなかったターンの trigger は "none"。
    """
    if is_new:
        return 0, "none"
    trigger = "miss" if miss_up else ("stuck" if stuck_up else ("help" if help_up else "none"))
    if trigger == "none":
        return prev, "none"
    if prev == 0:
        if stuck_up and stuck_after >= 2:
            return 1, "stuck"
        return 0, "none"
    return min(prev + 1, MAX_STRENGTH), trigger


# ===== Routes（児童） =====

@app.get("/api/config")
def get_public_config(user_id: str | None = None, session_id: int | None = None):
    """現在のフェーズ（認証不要）。児童側はこれを5秒間隔でポーリングする（心拍にもなる）。
    user_id と session_id が自分のものなら、そのセッションの式も返す（管理画面の上書きを画面に反映するため）。"""
    session = None
    if user_id and USER_ID_PATTERN.match(user_id):
        _touch(user_id, session_id)
        if session_id is not None:
            s = database.get_session(session_id)
            if s and s["user_id"] == user_id:
                session = s
    return _cfg_public(database.get_config(), session)


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

    # フェーズ・式はセッションのもの（＝児童の画面に出ているもの）。切替直後に旧画面から届いた送信を
    # 新フェーズとして記録しない（chat_logs.phase と sessions.phase を常に一致させる）。
    phase = session["phase"]
    expression = session["expression"]
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    produced = database.get_produced(user_id, phase)
    last = database.get_last_turn(req.session_id)
    ctx = dict(session=session, phase=phase, expression=expression, produced=produced,
               recent=database.get_recent_turns(req.session_id), last=last,
               counts={"stuck": session["stuck_count"], "miss": session["miss_count"],
                       "help": session["help_count"], "strength": session["strength"]},
               t_start=t_start)

    # すでに判定済みの本文の連続再送は API を呼ばず直前の結果を返す
    if _is_resend(message, last):
        return _finish(_handle_resend(req, user_id, message, ctx), user_id, ctx)

    input_kind = ai_classify.classify(message, ctx["recent"], expression, user_id=user_id)
    if input_kind == "sakumon":
        result = _handle_sakumon(req, user_id, message, ctx)
    elif req.declaring and phase == 2:
        # 予告待ちの入力欄から届いた作問以外の文 → 予告として分類する（対話には回さない）
        result = _handle_declaration(session, user_id, message, t_start)
    else:
        result = _handle_taiwa(req, user_id, message, ctx)
    return _finish(result, user_id, ctx)


def _handle_declaration(session: dict, user_id: str, text: str, t_start: float) -> dict:
    """予告（弱）：自由記述を構造に分類して declared に立てる。unknown なら立てない（再質問もしない）。
    input_type='declaration' で記録する。カウンタは動かさない。"""
    kind = ai_classify.classify_declaration(text, user_id=user_id)
    declared = kind if kind in STRUCTURES else None
    declared_by = "child" if declared else None
    if declared:
        database.set_state(session["session_id"], declared=declared, declared_by="child",
                           stuck_count=session["stuck_count"], miss_count=session["miss_count"],
                           help_count=session["help_count"], strength=session["strength"])
    produced = database.get_produced(user_id, 2)
    database.save_log(
        session_id=session["session_id"], user_id=user_id, phase=2, expression=session["expression"],
        input_type="declaration", message=text, ai_message=None,
        declared_structure=declared, declared_by=declared_by,
        produced_structures=produced, stuck_count=session["stuck_count"], miss_count=session["miss_count"],
        latency_ms=int((time.perf_counter() - t_start) * 1000),
    )
    return _base_result(None, None, "declaration", classified=kind, accepted=False)


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
    result = _handle_declaration(session, user_id, text, t_start)
    ctx = dict(session=session, phase=2)
    return _finish(result, user_id, ctx)


@app.post("/api/self_label")
def self_label(req: SelfLabelRequest):
    """中（強度2）の自己ラベル（仕様 v2 3-5）：3択のタップだけ。自由記述はない（self_label_text は使わない）。

    選択を self_label に、ai_judge 判定との一致を self_label_match に保存し、目標の指定の文言を返す。
    目標は prompt 発行時にシステムが立てた declared（未到達構造）。
    ★児童の選択が判定と食い違っても訂正しない（記録のみ）。カウンタは動かさない。フェーズ2以外は受け付けない。"""
    t_start = time.perf_counter()
    user_id = _normalize_user_id(req.user_id)
    session = _owned_session(req.session_id, user_id)
    _touch(user_id, req.session_id)
    if session["phase"] != 2:
        raise HTTPException(status_code=400, detail="フェーズ2でだけ受け付けます")
    produced = database.get_produced(user_id, 2)
    problems = database.get_valid_problems(req.session_id)
    if not problems:
        raise HTTPException(status_code=400, detail="成立した作問がありません")
    latest = problems[-1]          # 「いま 作って くれた 問題」＝直近の成立作問
    common = dict(session_id=req.session_id, user_id=user_id, phase=2, expression=session["expression"],
                  input_type="self_label", produced_structures=produced,
                  stuck_count=session["stuck_count"], miss_count=session["miss_count"])

    choice = (req.choice or "").strip()
    if choice not in STRUCTURES:
        raise HTTPException(status_code=400, detail="choice は tobun / hougan / bai")
    match = choice == latest["structure"]
    # 目標：prompt 発行時に立てた declared（システム）。無ければここで未到達構造から立てる
    target = session["declared"] if session["declared_by"] == "system" and session["declared"] else None
    if not target:
        target = ai_dialogue.pick_unreached_structure(produced)
        if target:
            database.set_state(req.session_id, declared=target, declared_by="system",
                               stuck_count=session["stuck_count"], miss_count=session["miss_count"],
                               help_count=session["help_count"], strength=session["strength"])
    ai_message = ai_dialogue.target_message(target, session["expression"]) if target else ai_dialogue.DONE_MESSAGE
    database.save_log(**common, message=ai_dialogue.STRUCTURE_LABEL[choice], ai_message=ai_message,
                      self_label=choice, self_label_match=match,
                      latency_ms=int((time.perf_counter() - t_start) * 1000))
    session = database.get_session(req.session_id)
    return {"message": ai_message, "dialog": None,
            "phase": 2, "show_support": True, "history": produced, **_support_state(session, True)}


@app.get("/api/judge/status")
def judge_status(user_id: str):
    """送信中の児童が1秒間隔で見る。同時実行上限で待たされていれば "waiting"。"""
    if not USER_ID_PATTERN.match(user_id or ""):
        raise HTTPException(status_code=400, detail="bad user_id")
    return {"state": llm_call.wait_state(user_id)}


def _is_resend(message: str, last: dict | None) -> bool:
    """直前のターンと同じ本文 → 再送とみなす。ただし直前が判定エラーなら判定し直す（3-2「もう一度 おくって みてね」）。"""
    if last is None or (last.get("message") or "") != message:
        return False
    if last.get("input_type") == "sakumon" and last.get("issue") == "error":
        return False
    return True


def _latency(ctx: dict) -> int:
    return int((time.perf_counter() - ctx["t_start"]) * 1000)


def _base_result(message: str | None, response_type: str | None, input_type: str, **extra) -> dict:
    return {
        "message": message, "response_type": response_type, "prompt_strength": None,
        "valid": None, "structure": None, "unknown": None, "is_new": False,
        "input_type": input_type, "dialog": None, **extra,
    }


def _finish(result: dict, user_id: str, ctx: dict) -> dict:
    phase = ctx["phase"]
    show = phase == 2
    history = database.get_produced(user_id, phase)
    session = database.get_session(ctx["session"]["session_id"])
    result.setdefault("dialog", None)
    result["phase"] = phase
    result["show_support"] = show
    result["history"] = history if show else []
    result["all_reached"] = show and set(history) >= STRUCTURES
    result["choices"] = _choices()
    result.update(_support_state(session, show))
    # accepted：フロントが「つくった お話」一覧に追加するか（成立した作問だけ。再送・判定エラーは追加しない）
    result.setdefault("accepted", bool(result.get("valid")) and result.get("input_type") == "sakumon")
    if not show:
        # フェーズ1・3では判定結果を画面に出さない（ログには残っている）
        result["message"] = ai_dialogue.ACK_MESSAGE
        for k in ("valid", "structure", "unknown", "response_type", "prompt_strength", "declaration_met", "dialog"):
            result[k] = None
        result["is_new"] = False
        result["accepted"] = False
    return result


def _handle_resend(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """判定済みの本文の再送：API を呼ばず直前の応答をそのまま返す（多重呼び出し防止）。

    ログは残す（再送の回数は計測したい）が、input_type="resend" として提出・成立・カウンタのどれにも
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
    """対話経路：judge は通さない。フェーズ1・3は「おくったよ」。カウンタは動かさない。"""
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
    """作問経路：ai_judge で同定 → 状態機械（仕様 v2 2-5）で応答の種類と強さを決定 → ai_dialogue が文言。

    カウンタの規則（2-3）：
      不成立            → stuck / miss は変化しない
      成立・新構造      → produced に追加、stuck = 0、miss = 0
      成立・既出構造    → stuck += 1
      予告あり・一致    → declaration_met = 1、miss = 0
      予告あり・不一致  → declaration_met = 0、miss += 1
    状態機械はフェーズ2でだけ動く。フェーズ1・3は判定だけ記録する（カウンタも動かさない）。
    中・強ではシステムが未到達構造を declared_by="system" で立てる（中はステップ3でそれを文言で伝える）。
    """
    phase, expression, produced, counts = ctx["phase"], ctx["expression"], ctx["produced"], ctx["counts"]
    session = ctx["session"]
    show = phase == 2
    jr = ai_judge.judge(message, expression, user_id=user_id)

    # 技術的失敗（規定回数リトライしても API が応答しない）→ 3-2 error。一覧には載せず、送り直してもらう。
    # 児童の責任ではないのでカウンタは動かさない。
    if jr.get("issue") == "error":
        ai_message = ai_dialogue.FORM_MESSAGES["error"] if show else None
        result = _base_result(ai_message, "error" if show else None, "sakumon", accepted=False,
                              prompt_strength=0 if show else None)
        database.save_log(
            session_id=req.session_id, user_id=user_id, phase=phase, expression=expression,
            input_type="sakumon", message=message, ai_message=ai_message,
            valid=None, issue="error", is_new=False,
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
    ref_no = len(database.get_valid_problems(req.session_id)) + (1 if valid else 0)  # 最新の表示番号
    quoted = None

    # ---- 状態機械（フェーズ2のみ。不成立は何も動かさない） ----
    stuck, miss, help_count = counts["stuck"], counts["miss"], counts["help"]
    prev_strength = counts["strength"]
    declared, declared_by = session["declared"], session["declared_by"]
    declared_used, declared_by_used, met = None, None, None
    strength = None
    trigger = "none"
    response_type = None
    dialog = None
    if show and valid:
        stuck_up = miss_up = False
        if declared:
            declared_used, declared_by_used = declared, declared_by
            met = structure == declared
            miss_up = not met
            miss = 0 if met else miss + 1
        if is_new:
            stuck, miss, help_count = 0, 0, 0
        else:
            stuck += 1
            stuck_up = True
        # 予告はこの作問で消費される。中・強はこのあとシステムが新しい目標を立てる
        declared, declared_by = None, None
        if completes_all or all_before:
            response_type, strength = "done", 0
        else:
            strength, trigger = decide_strength(prev_strength, is_new=is_new, stuck_after=stuck,
                                                stuck_up=stuck_up, miss_up=miss_up)
            if strength == 0:
                response_type = "praise"
            else:
                response_type = "prompt"
                dialog = {1: "declaration", 2: "self_label_choice"}.get(strength)
                if strength in (1, 2):
                    # 弱・中：いま成立した作問の問いの文を引用する（取れなければ定型文）
                    quoted = ai_dialogue.extract_question(message, user_id=user_id)
                if strength >= 2:
                    # 中・強：produced に含まれない構造を固定順で1つ指定（自己ラベルは使わない）
                    declared = ai_dialogue.pick_unreached_structure(produced_after)
                    declared_by = "system" if declared else None
        database.set_state(req.session_id, declared=declared, declared_by=declared_by,
                           stuck_count=stuck, miss_count=miss, help_count=help_count, strength=strength)
    elif show:
        response_type, strength = "form", 0

    ai_message = None
    if show:
        dlg = ai_dialogue.dialogue(
            message, "sakumon", {**jr, "is_new": is_new, "completes_all": completes_all},
            produced, ctx["recent"], response_type, expression,
            prompt_strength=strength, target=declared, ref_no=ref_no, quoted=quoted, user_id=user_id,
        )
        ai_message = dlg["message"]

    result = _base_result(ai_message, response_type, "sakumon")
    result.update({"valid": valid, "structure": structure, "unknown": unknown, "is_new": is_new,
                   "prompt_strength": strength, "strength_trigger": trigger if show else None,
                   "declaration_met": met, "dialog": dialog})
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


@admin.post("/api/sessions/{session_id}/expression")
def admin_set_session_expression(session_id: int, req: SessionExpressionRequest):
    """個別の式の上書き（当日のトラブル対応用）。児童の画面には次のポーリングで反映される。"""
    if database.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        expression = config.normalize_expression(req.expression)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    database.set_session_expression(session_id, expression)
    return {"ok": True, "session_id": session_id, "expression": expression}


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
