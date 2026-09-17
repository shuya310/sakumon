"""作問支援システム API。

- フェーズは app_config（database.get_config）から取得する。ハードコードしない。
- 式は児童（出席番号の奇偶）とフェーズで決まる（EXPRESSION_ASSIGNMENT。仕様 v2 4章）。
  セッション開始時に決めて sessions.expression に保存し、途中で変えない。管理画面から個別に上書きできる。
- フェーズ1・3：classify は同期で動かし、作問なら本文を judge_status='pending' で保存した時点で「おくったよ」を返す。
  判定（ai_judge）は応答後に judge_queue が実行して同じ行を埋める（9/17。判定を待つ約 3 秒のラグをなくす）。
  response_type / ai_message は記録しない（表示していないものは記録しない）。判定が失敗した行は judge_status='failed'
  （issue='error'）で、管理画面の「再判定」から再投入できる。
- フェーズ2：応答の種類（response_type: form / praise / prompt / done / talk / error）を状態機械
  （docs/sakumon_spec_v3.md 2章）で決定論的に決め、ai_dialogue に文言を組み立てさせる。AIには判定させない。
  状態（sessions に保持・フェーズスコープ）：produced（到達構造の集合）、declared / declared_by（予告）、
  stuck（新構造に到達しなかった成立作問の連続回数）、miss（予告不一致の累積回数）、
  help（taiwa が支援要求に分類された回数）、strength（現在の強度 0〜3。decide_strength で遷移）。
  不成立・再送ではカウンタを動かさない。新構造到達で3カウンタと強度を全て 0 に戻す。
- 弱（強度1）は「役割の宣言」2ターン。ターン1「{n}ばんの お話で、{divisor}は 何を あらわして いるかな？」
  → 答えを classify_role で分類（role_answer）。判定の役割（expected_divisor_role）と食い違えば1回だけ、
  児童の問題文の除数の句を引用して問い返す（role_corrected。2回目以降は正誤にかかわらず進む）。
  ターン2「じゃあ 次は、{divisor}を 何の 数に して みたい？」→ classify_declaration で予告（declared_by=child）。
  どちらも児童の画面では作問と同じ入力欄から送る（入力欄は1つ）。対話待ちの状態で届いた入力は、
  classify が作問なら通常の作問処理（対話は打ち切り・ブロックしない）、そうでなければ待っているターンの答え
  として扱う（/api/judge の declaring。どのターンを待っているかはサーバが直前のログ行から決める）。
- 中（強度2）・強（強度3）はシステムが未到達構造を目標に立てて文言で伝える（3択の自己ラベルは廃止）。
- フェーズ1・3「作った お話を 見る」（3つえらぶ）：児童はいつでも選択画面を開け（/api/selection/open。開くたびに記録）、
  そのフェーズで送った作問（不成立・未判定も含む。対話は除く）から最大 min(3, 作問数) を選んで送る（/api/selection/submit。
  送るたびに記録。分析では最後の選択を採用）。判定結果・構造名は返さない。教師の「声がけした」は teacher_calls に時刻だけ記録。
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
import judge_queue

if not config.ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD が未設定です。ローカルは .env に、本番は Render の Environment に設定してください。"
    )

STRUCTURES = {"tobun", "hougan", "bai"}
USER_ID_PATTERN = re.compile(r"^[0-9]{2}$")
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# 式の設定は config.EXPRESSION_ASSIGNMENT（ここ1箇所）。main からは別名で参照する（テスト・旧記述との互換）。
EXPRESSION_ASSIGNMENT = config.EXPRESSION_ASSIGNMENT
EXPRESSION_CHOICES = config.EXPRESSION_CHOICES

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


class PhaseRequest(BaseModel):
    phase: int


class SessionExpressionRequest(BaseModel):
    expression: str


class SelectionSubmitRequest(BaseModel):
    session_id: int
    user_id: str
    log_ids: list[int]


class TeacherCallRequest(BaseModel):
    phase: int


# ===== 共通ヘルパ =====

def _normalize_user_id(user_id: str) -> str:
    uid = (user_id or "").strip().lower()
    if not USER_ID_PATTERN.match(uid):
        raise HTTPException(status_code=400, detail="出席番号は数字2桁で入力してください")
    return uid


def _touch(user_id: str, session_id: int | None):
    _last_seen[user_id] = {"session_id": session_id, "ts": time.time()}


def assigned_expression(user_id: str, phase: int) -> str:
    """出席番号の奇偶とフェーズから式を決める（'21 ÷ 3' の形）。"""
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
        "expression_choices": EXPRESSION_CHOICES,
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
    """いま待っている対話のターン（直前のログ行から決める。再入場時の復元にも使う）。

    role        … 弱・ターン1（除数の役割）の答え待ち。訂正を出した直後もここ
    declaration … 弱・ターン2（予告）の答え待ち
    None        … 待っていない（通常の作問入力）
    """
    if not last:
        return None
    if last["input_type"] == "sakumon" and last.get("response_type") == "prompt" and last.get("prompt_strength") == 1:
        return "role"
    if last["input_type"] == "role":
        return "role" if last.get("role_corrected") else "declaration"
    if last["input_type"] in ("taiwa", "resend") and last.get("awaiting") == "role":
        return "role"     # talk が役割を問うた（強度1以上）／help で 0→1 に上がり ROLE_ASK を連結した直後
    return None


def _awaiting_answer(last: dict | None) -> bool:
    """直前の AI の発話が児童への問いで、その答えを待っているか（9/17 修正①）。

    弱のターン1・2（_pending_dialog）に加えて、talk が問い返した直後（chat_logs.awaiting='answer'）も含む。
    この状態の入力は LLM 分類に頼らず、完全な問題文（ai_classify.looks_like_problem）だけを作問にする。"""
    if not last:
        return False
    return _pending_dialog(last) is not None or last.get("awaiting") in ("answer", "role", "declaration")


def _awaiting_of(response_type: str | None, dialog: str | None, ai_message: str | None,
                 expression: str | None = None) -> str | None:
    """このターンのあと、サーバが児童の何を待つか（ログ列 awaiting）。role / declaration / answer / None。
    talk が除数の役割を問うていれば role（次の入力は _handle_role で評価する）。"""
    if dialog in ("role", "declaration"):
        return dialog
    if response_type == "talk":
        if expression and ai_dialogue.asks_role(ai_message, config.parse_expression(expression)[1]):
            return "role"
        if ai_dialogue.asks_child(ai_message):
            return "answer"
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
        **_support_state(session, show),
        "poll_seconds": config.CONFIG_POLL_SECONDS,
    }


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
      - 強度 0 → 1               → stuck が 2 に達したとき（同じ構造を1回くり返しただけでは介入しない）、
                                    または help が増えたとき（明示的な援助要求は随伴的指導の原則における支援増強の契機。
                                    Wood & Middleton 1975。9/17 修正④B）。miss だけでは上がらない
      - 強度 1 以上              → stuck / miss / help のいずれかが増えたターンごとに +1（上限 3）
    同じターンで stuck と miss が両方増えても +1 は1回（1回の失敗＝1段）。trigger は miss > stuck > help の
    優先で1つだけ記録する。強度が上がらなかったターン（上限3で据え置きを含む）の trigger は "none"。
    """
    if is_new:
        return 0, "none"
    trigger = "miss" if miss_up else ("stuck" if stuck_up else ("help" if help_up else "none"))
    if trigger == "none":
        return prev, "none"
    if prev == 0:
        if stuck_up and stuck_after >= 2:
            return 1, "stuck"
        if help_up:
            return 1, "help"
        return 0, "none"
    if prev >= MAX_STRENGTH:
        return MAX_STRENGTH, "none"      # 上限で据え置き（カウンタは動くが強度は上がらない）
    return prev + 1, trigger


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

    if phase == 2 and _awaiting_answer(last):
        # AI が問いを出した直後：原則は対話。数量が2つ以上あり問いの文で終わる完全な問題文だけ作問（LLM 分類は使わない）
        input_kind = "sakumon" if ai_classify.looks_like_problem(message) else "taiwa"
    elif phase == 2 and last and last.get("response_type") == "form" and ai_classify.looks_like_fragment(message):
        # 形式支援（「どこか さがして みよう」等）への短い返事（「一人4こ」）は作問ではない。判定に回さず対話にする
        input_kind = "taiwa"
    else:
        # 分類に失敗したら、フェーズ2は対話（judge に対話文を流さない）、フェーズ1・3は作問（pending で残し、再判定で拾う）
        input_kind = ai_classify.classify(message, ctx["recent"], expression, user_id=user_id,
                                          fallback="taiwa" if phase == 2 else "sakumon")
    pending = _pending_dialog(last) if (req.declaring and phase == 2) else None
    if input_kind == "sakumon":
        result = _handle_sakumon(req, user_id, message, ctx)
    elif pending == "role":
        # 弱・ターン1の答え（除数の役割）。対話には回さない
        result = _handle_role(session, user_id, message, ctx)
    elif pending == "declaration":
        # 弱・ターン2の答え（予告）→ 構造に分類する
        result = _handle_declaration(session, user_id, message, t_start)
    else:
        result = _handle_taiwa(req, user_id, message, ctx)
    return _finish(result, user_id, ctx)


def _handle_role(session: dict, user_id: str, text: str, ctx: dict) -> dict:
    """弱・ターン1（役割の宣言）：「{divisor}は 何を あらわして いるかな？」への答えを処理する。

    - 答えを classify_role で分類し role_answer に保存する（訂正前の生の値。必ず保存）
    - 直前の成立作問の判定（structure / unknown）から見た除数の役割と突き合わせ、
      不一致（役割が確定でき、答えも people / per_one / base のどれかに確定したときだけ）なら
      1回だけ訂正：児童の問題文の除数の句を引用して問い返す（正解の役割名は言わない）→ もう一度ターン1を待つ
    - 一致・わからない・判別不能、または訂正後の2回目の答え → 正誤にかかわらずターン2へ
    カウンタ・強度は動かさない。input_type='role'。"""
    sid, expression, last = session["session_id"], session["expression"], ctx["last"]
    problems = database.get_valid_problems(sid)
    if not problems:
        return _handle_taiwa(JudgeRequest(session_id=sid, user_id=user_id, message=text), user_id, text, ctx)
    latest = problems[-1]
    _dividend, divisor = config.parse_expression(expression)
    role = ai_classify.classify_role(text, divisor, user_id=user_id)
    expected = ai_dialogue.expected_divisor_role(latest["structure"], latest["unknown"])
    already_corrected = bool(last and last.get("input_type") == "role" and last.get("role_corrected"))
    correct = (not already_corrected and expected is not None
               and role in ("people", "per_one", "base") and role != expected)
    if correct:
        phrase = ai_dialogue.extract_divisor_phrase(latest["text"], divisor, user_id=user_id)
        ai_message = ai_dialogue.role_correction_message(phrase, expression)
        dialog = "role"
    else:
        ai_message = ai_dialogue.role_next_message(expression)
        dialog = "declaration"
    database.save_log(
        session_id=sid, user_id=user_id, phase=2, expression=expression,
        input_type="role", message=text, ai_message=ai_message,
        response_type="prompt", prompt_strength=max(1, session["strength"]),   # 弱のターンだが、talk が問うた場合は現在の強度
        role_answer=role, role_corrected=correct,
        produced_structures=ctx["produced"], stuck_count=session["stuck_count"], miss_count=session["miss_count"],
        help_count=session["help_count"], strength=session["strength"], strength_trigger="none",
        target_structure=session["declared"], awaiting=dialog,
        latency_ms=_latency(ctx),
    )
    return _base_result(ai_message, "prompt", "role", prompt_strength=max(1, session["strength"]),
                        role_answer=role, role_corrected=correct, dialog=dialog, accepted=False)


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
        help_count=session["help_count"], strength=session["strength"], strength_trigger="none",
        target_structure=declared or session["declared"],
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
        help_count=counts["help"] if phase == 2 else None, strength=counts["strength"] if phase == 2 else None,
        strength_trigger="none" if phase == 2 else None, target_structure=ctx["session"]["declared"],
        awaiting=last.get("awaiting"),   # 同じ問いを返しているので待ち状態も同じ
        latency_ms=None,
    )
    return result


def _talk_context(session: dict, expression: str) -> dict:
    """taiwa の LLM に渡す状況：現在の強度・目標・成立作問の一覧（本文と、わる数が指していたもの）。
    わる数の役割は ai_judge の判定（structure / unknown）から引く（ai_dialogue.divisor_role_label）。"""
    _dividend, divisor = config.parse_expression(expression)
    problems = database.get_valid_problems(session["session_id"])
    listed = [{"text": p["text"], "divisor_role": ai_dialogue.divisor_role_label(p["structure"], p["unknown"], divisor)}
              for p in problems]
    last = None
    if problems:
        p = problems[-1]
        last = {"text": p["text"], "structure": p["structure"], "unknown": p["unknown"],
                "divisor_role": listed[-1]["divisor_role"], "item": p["item"], "unit": p["unit"]}
    return {"strength": session["strength"], "target": session["declared"], "problems": listed, "last_problem": last}


def _last_turn_context(last: dict | None) -> dict | None:
    """taiwa の LLM に渡す「直前のやりとり」：児童の最新入力（作問なら判定と issue）と、それへの AI の返事（9/17 修正③）。
    再送行は本文が同じなので、そのまま渡す。"""
    if not last:
        return None
    return {"child": last.get("message"), "ai": last.get("ai_message"), "input_type": last.get("input_type"),
            "response_type": last.get("response_type"), "issue": last.get("issue"), "valid": last.get("valid")}


def _handle_taiwa(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """対話経路：judge は通さない。フェーズ1・3は「おくったよ」。カウンタは動かさない。
    フェーズ2では現在の強度・目標・到達構造・成立作問を LLM に渡す（話してよいことは強度で決まる）。"""
    phase, produced, counts = ctx["phase"], ctx["produced"], ctx["counts"]
    session = ctx["session"]
    show = phase == 2

    ai_message = None
    is_help = None
    strength, trigger = (session["strength"] if show else None), "none"
    help_count, declared, declared_by = counts["help"], session["declared"], session["declared_by"]
    awaiting = dialog = None
    if show:
        dlg = ai_dialogue.dialogue(message, "taiwa", None, produced, ctx["recent"], "talk",
                                   ctx["expression"], user_id=user_id,
                                   context={**_talk_context(session, ctx["expression"]),
                                            "last_turn": _last_turn_context(ctx["last"])})
        ai_message = dlg["message"]
        is_help = dlg.get("is_help_request")
        # 支援要求（フェーズE）：help += 1 → 強度規則で段階を更新（文言は更新前の強度で生成済み。反映は次ターンから）。
        # 3つそろった後は支援なし（カウンタも動かさない）
        if is_help and not (set(produced) >= STRUCTURES):
            help_count += 1
            strength, trigger = decide_strength(session["strength"], help_up=True)
            if strength >= 2 and not declared:
                # 中・強に上がったのに目標が無ければ、ここでシステムが未到達構造を立てる
                declared = ai_dialogue.pick_unreached_structure(produced)
                declared_by = "system" if declared else None
            database.set_state(req.session_id, declared=declared, declared_by=declared_by,
                               stuck_count=counts["stuck"], miss_count=counts["miss"],
                               help_count=help_count, strength=strength)
            if session["strength"] == 0 and strength == 1:
                # 0→1（help）：弱＝役割の宣言をここから始める。talk の文言（強度0）のあとに ROLE_ASK を連結し、
                # ターン1の答えを待つ（→ _handle_role → ターン2 → 予告）。成立作問が無ければ問う対象が無いので連結しない
                problems = database.get_valid_problems(req.session_id)
                if problems:
                    ai_message = f"{ai_message}\n{ai_dialogue.weak_message(len(problems), ctx['expression'])}"
                    dialog = "role"
        awaiting = _awaiting_of("talk", dialog, ai_message, ctx["expression"])
        if awaiting == "role":
            dialog = "role"     # talk が役割を問うた → 次の入力は役割の答えとして評価する（修正④A）

    result = _base_result(ai_message, "talk" if show else None, "taiwa", dialog=dialog)
    result.update({"prompt_strength": session["strength"] if show else None,
                   "is_help_request": is_help, "strength_trigger": trigger if show else None})
    database.save_log(
        session_id=req.session_id, user_id=user_id, phase=phase, expression=ctx["expression"],
        input_type="taiwa", message=message, ai_message=ai_message,
        response_type="talk" if show else None, prompt_strength=session["strength"] if show else None,
        is_help_request=is_help,
        produced_structures=produced, stuck_count=counts["stuck"], miss_count=counts["miss"],
        help_count=help_count if show else None, strength=strength, strength_trigger=trigger if show else None,
        target_structure=session["declared"] if show else None,   # 発話（talk）が参照した目標＝更新前
        awaiting=awaiting,
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
    中・強ではシステムが未到達構造を declared_by="system" で立てて文言で伝える。弱は役割の宣言（ターン1）を開く。
    """
    phase, expression, produced, counts = ctx["phase"], ctx["expression"], ctx["produced"], ctx["counts"]
    session = ctx["session"]
    show = phase == 2
    if not show:
        return _handle_sakumon_deferred(req, user_id, message, ctx)
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
            help_count=counts["help"] if show else None, strength=counts["strength"] if show else None,
            strength_trigger="none" if show else None, target_structure=session["declared"] if show else None,
            latency_ms=_latency(ctx), judge_status="failed",
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
                if strength == 1:
                    dialog = "role"      # 弱：役割の宣言・ターン1の答えを待つ
                else:
                    # 中・強：produced に含まれない構造を固定順で1つ指定
                    declared = ai_dialogue.pick_unreached_structure(produced_after)
                    declared_by = "system" if declared else None
        database.set_state(req.session_id, declared=declared, declared_by=declared_by,
                           stuck_count=stuck, miss_count=miss, help_count=help_count, strength=strength)
    elif show:
        response_type, strength = "form", 0
    # ログ用：このターン後の強度（不成立は据え置き）と、AI の発話が指した目標
    strength_after = (strength if valid else prev_strength) if show else None

    ai_message = None
    if show:
        dlg = ai_dialogue.dialogue(
            message, "sakumon", {**jr, "is_new": is_new, "completes_all": completes_all},
            produced, ctx["recent"], response_type, expression,
            prompt_strength=strength, target=declared, ref_no=ref_no,
            item=jr.get("item"), unit=jr.get("unit"), session_id=req.session_id, user_id=user_id,
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
        item=jr.get("item"), unit=jr.get("unit"),
        response_type=response_type, prompt_strength=strength,
        declared_structure=declared_used, declared_by=declared_by_used, declaration_met=met,
        produced_structures=produced_after, stuck_count=stuck, miss_count=miss,
        help_count=help_count if show else None, strength=strength_after, strength_trigger=trigger if show else None,
        target_structure=declared if show else None, awaiting=dialog,
        latency_ms=_latency(ctx), judge_status="done",
    )
    return result


def _handle_sakumon_deferred(req: JudgeRequest, user_id: str, message: str, ctx: dict) -> dict:
    """フェーズ1・3の作問：本文を judge_status='pending' で保存して即応答し、判定は judge_queue に任せる。

    判定に関わる列（valid / structure / unknown / issue / is_new / item / unit / produced_structures）は判定完了時に
    judge_queue が埋める（is_new は送信順で決まる）。カウンタ・支援の列はフェーズ1・3では従来どおり動かさない。"""
    phase, expression, counts = ctx["phase"], ctx["expression"], ctx["counts"]
    log_id = database.save_log(
        session_id=req.session_id, user_id=user_id, phase=phase, expression=expression,
        input_type="sakumon", message=message, ai_message=None,
        valid=None, structure=None, unknown=None, issue=None, is_new=None,
        response_type=None, prompt_strength=None,
        produced_structures=None, stuck_count=counts["stuck"], miss_count=counts["miss"],
        latency_ms=_latency(ctx), judge_status="pending",
    )
    judge_queue.enqueue(user_id, log_id)
    return _base_result(None, None, "sakumon", accepted=False, strength_trigger=None, declaration_met=None)


# ===== 「作った お話を 見る」（フェーズ1・3の選択） =====

SELECTION_MAX = 3


def _selection_session(session_id: int, user_id: str) -> dict:
    session = _owned_session(session_id, user_id)
    if session["phase"] == 2:
        raise HTTPException(status_code=400, detail="この画面はフェーズ1・3でだけ使えます")
    return session


def _selection_payload(session: dict) -> dict:
    problems = database.get_sakumon_rows(session["session_id"])
    state = database.get_selection_state(session["session_id"])
    return {
        "problems": problems,                       # [{no, log_id, text}] 送信順。判定結果は含めない
        "max_select": min(SELECTION_MAX, len(problems)),
        "last_log_ids": state["last_log_ids"],      # 直前の選択（選び直しの起点として画面でチェック済みにする）
        "last_at": state["last_at"],
    }


@app.post("/api/selection/open")
def selection_open(req: SessionRequest):
    """選択画面を開いた（開くたびに記録）。そのフェーズの作問一覧と直前の選択を返す。"""
    user_id = _normalize_user_id(req.user_id)
    session = _selection_session(req.session_id, user_id)
    _touch(user_id, req.session_id)
    database.add_selection_event(req.session_id, user_id, session["phase"], "open")
    return _selection_payload(session)


@app.post("/api/selection/submit")
def selection_submit(req: SelectionSubmitRequest):
    """選択の送信（何度でも可。すべて時刻付きで記録）。自分の作問行だけ・重複なし・1〜min(3, 作問数) 個。"""
    user_id = _normalize_user_id(req.user_id)
    session = _selection_session(req.session_id, user_id)
    _touch(user_id, req.session_id)
    problems = database.get_sakumon_rows(req.session_id)
    valid_ids = {p["log_id"] for p in problems}
    ids = list(dict.fromkeys(req.log_ids))     # 順序を保って重複を除く
    if not ids or len(ids) != len(req.log_ids):
        raise HTTPException(status_code=400, detail="選ぶ お話を 1つ以上、重複なしで送ってください")
    if not set(ids) <= valid_ids:
        raise HTTPException(status_code=400, detail="自分の作問ではない行が含まれています")
    max_select = min(SELECTION_MAX, len(problems))
    if len(ids) > max_select:
        raise HTTPException(status_code=400, detail=f"選べるのは {max_select} つまでです")
    database.add_selection_event(req.session_id, user_id, session["phase"], "submit", ids)
    return {"ok": True, **_selection_payload(session)}


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
    submitted = database.count_selection_submitted()
    calls = database.get_teacher_calls()
    return {"config": _cfg_public(cfg), "students": rows,
            "judge": {**database.count_judge_status(), "queued": judge_queue.pending_in_queue()},
            "selection": {str(ph): {"submitted": submitted.get(ph, 0), "teacher_call": calls.get(ph)} for ph in (1, 3)}}


@admin.post("/api/teacher_call")
def admin_teacher_call(req: TeacherCallRequest):
    """「声がけした」：フェーズと時刻だけを記録する（児童画面には何も反映しない。同じフェーズで複数回可）。"""
    if req.phase not in (1, 3):
        raise HTTPException(status_code=400, detail="声がけはフェーズ1・3でだけ記録します")
    return {"ok": True, **database.add_teacher_call(req.phase), "calls": database.get_teacher_calls()}


@admin.post("/api/rejudge")
def admin_rejudge():
    """未判定（pending）・失敗（failed）の作問行を判定キューに入れ直す（サーバ再起動・API エラーの復旧用）。"""
    return {"ok": True, "requeued": judge_queue.requeue_unjudged(), **database.count_judge_status()}


@admin.get("/api/students")
def admin_students():
    return database.admin_get_all_students()


@admin.get("/api/students/{user_id}")
def admin_student_detail(user_id: str):
    return {"user_id": user_id, "sessions": database.admin_get_student_sessions(user_id)}


@admin.get("/api/sessions/{session_id}")
def admin_session_logs(session_id: int):
    return database.admin_get_session_logs(session_id)


@admin.get("/api/sessions/{session_id}/selection")
def admin_session_selection(session_id: int):
    """そのセッションの選択イベント（open / submit の全履歴）と最終選択の本文。"""
    if database.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    problems = {p["log_id"]: p for p in database.get_sakumon_rows(session_id)}
    state = database.get_selection_state(session_id)
    last = [problems[i] for i in (state["last_log_ids"] or []) if i in problems]
    return {**state, "last_problems": last, "events": database.get_selection_events(session_id)}


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
