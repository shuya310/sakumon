"""フェーズ管理・状態機械（仕様 v2 2章）・文言（3章）・式（4章）・認証・CSV の決定論的な動作確認（LLM はモック）。

実行: cd backend && ./venv/bin/python tests/test_flow.py
一時DBを使うので data/sakumon.db には触れない。
"""
import base64
import csv
import io
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = str(Path(_tmp) / "test.db")
os.environ.setdefault("ADMIN_PASSWORD", "test-pass")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
config.JUDGE_BG_RETRY_WAITS = ()      # 応答後の判定の外側リトライは待たない（テストを速く・決定論に）

import main  # noqa: E402
import judge_queue  # noqa: E402
import database  # noqa: E402
import ai_judge  # noqa: E402
import ai_classify  # noqa: E402
import ai_dialogue as d  # noqa: E402

# ---- モック：メッセージ先頭の記号で判定を決める ----
_JUDGE = {
    "T": {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None},
    "H": {"valid": True, "structure": "hougan", "unknown": "num_units", "issue": None},
    "B": {"valid": True, "structure": "bai", "unknown": "ratio", "issue": None},
    "X": {"valid": False, "structure": "invalid", "unknown": None, "issue": "scene_contradiction"},
    "N": {"valid": False, "structure": "invalid", "unknown": None, "issue": "no_question"},
    "W": {"valid": False, "structure": "invalid", "unknown": None, "issue": "wrong_number"},
    "P": {"valid": False, "structure": "invalid", "unknown": None, "issue": "not_problem"},
    "E": {"valid": False, "structure": "invalid", "unknown": None, "issue": "error", "error": "boom",
          "meta": {"retry_count": 3, "latency_ms": 65000, "status": "failed"}},
}

CALLS = {"judge": 0, "classify": 0, "llm": 0, "declare": 0}


LAST_JUDGE = {}


def fake_judge(message, expression, user_id=None):
    """モック：先頭の記号で判定。本文末尾の「@物/助数詞」で item / unit を返す（無ければ None）。"""
    CALLS["judge"] += 1
    LAST_JUDGE.update(message=message, expression=expression)
    jr = dict(_JUDGE[message[0]], item=None, unit=None)
    if "@" in message:
        item, _, unit = message.rsplit("@", 1)[1].partition("/")
        jr["item"], jr["unit"] = item or None, unit or None
    return jr


LAST_CLASSIFY = {}


def fake_classify(message, recent=None, expression=None, user_id=None, fallback="taiwa"):
    CALLS["classify"] += 1
    LAST_CLASSIFY.update(fallback=fallback)
    if message.startswith("?:"):
        return fallback      # 分類失敗（LLM 不通）を模す
    return "sakumon" if len(message) > 1 and message[1] == ":" and message[0] in _JUDGE else "taiwa"


LAST_TALK = {}


def fake_llm(child_message, input_kind, judge_result, history, recent_turns, response_type, expression,
             user_id=None, context=None):
    CALLS["llm"] += 1
    LAST_TALK.clear()
    LAST_TALK.update(history=list(history), context=context, expression=expression)
    is_help = any(k in child_message for k in ("ヒント", "わからない", "どうすれば", "思いつかない"))
    # 「どういうこと」を含む発話には問い返し（文末「かな？」）を返す＝次の入力は awaiting（修正①）
    message = "[talk] どこが むずかしかったかな？" if "どういうこと" in child_message else f"[{response_type}] llm"
    if "4ってなに" in child_message:
        message = "[talk] お話の 中の 4は 何の 数かな？"    # 役割の問い返し（強度1以上で許される）
    return {"message": message, "state": response_type, "is_help_request": is_help}


def fake_declaration(text, user_id=None, expression="24 ÷ 4"):
    """モック：弱の問い「4を どんな ふうに 使った お話に する？」への答えを構造に分類する。"""
    CALLS["declare"] += 1
    LAST_DECLARE.update(text=text, expression=expression)
    for key, kind in (("1つ分", "tobun"), ("人数", "tobun"), ("人で", "tobun"),
                      ("いくつ分", "hougan"), ("ずつ", "hougan"),
                      ("何倍", "bai"), ("くらべ", "bai")):
        if key in text:
            return kind
    return "unknown"


LAST_DECLARE = {}


PHRASE = {"on": True}


def fake_extract_divisor_phrase(problem_text, divisor, user_id=None):
    """モック：本文の【…】を除数の句とみなす。PHRASE["on"] が False なら LLM 抽出失敗＝実装の予備（除数を含む文）に落ちる。"""
    CALLS["phrase"] = CALLS.get("phrase", 0) + 1
    if PHRASE["on"] and "【" in problem_text:
        return d.format_quote(problem_text.split("【", 1)[1].split("】", 1)[0])
    return d._sentence_with_number(problem_text, divisor)


ai_classify.classify_declaration = fake_declaration
main.ai_classify.classify_declaration = fake_declaration
assert not hasattr(ai_classify, "classify_role"), "役割の宣言（classify_role）は v3.1 で廃止"
d.extract_divisor_phrase = fake_extract_divisor_phrase
ai_judge.judge = fake_judge
main.ai_judge.judge = fake_judge
judge_queue.ai_judge.judge = fake_judge
ai_classify.classify = fake_classify
main.ai_classify.classify = fake_classify
REAL_LLM_MESSAGE = d._llm_message
d._llm_message = fake_llm

client = TestClient(main.app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"x:test-pass").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()}
BANNED_CHILD_WORDS = ("種類", "たずね", "聞いていること", "ちがうことを聞く", "等分除", "包含除")


def post(path, **body):
    return client.post(path, json=body)


def judge(sid, uid, msg):
    r = post("/api/judge", session_id=sid, user_id=uid, message=msg)
    assert r.status_code == 200, r.text
    return r.json()


def admin_post(path, **body):
    r = client.post(path, json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def logs_of(sid):
    return client.get(f"/admin/api/sessions/{sid}", headers=AUTH).json()


def raw(sql, *args):
    con = sqlite3.connect(os.environ["DATABASE_PATH"])
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def no_banned(text):
    for w in BANNED_CHILD_WORDS:
        assert w not in (text or ""), (w, text)


with client:
    # ===== スキーマ =====
    tables = {r[0] for r in raw("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "chat_logs", "app_config", "phase_changes", "selection_events", "teacher_calls"} <= tables
    cols = [r[1] for r in raw("PRAGMA table_info(chat_logs)")]
    assert cols == [
        "log_id", "session_id", "user_id", "phase", "expression", "created_at",
        "input_type", "message", "ai_message",
        "valid", "structure", "unknown", "issue", "is_new", "item", "unit", "divisor_phrase", "question_phrase",
        "response_type", "prompt_strength",
        "declared_structure", "declared_by", "declaration_met",
        "self_label", "self_label_text", "self_label_match",
        "role_answer", "role_corrected", "is_help_request",
        "produced_structures", "stuck_count", "miss_count", "help_count",
        "strength", "strength_trigger", "target_structure", "awaiting", "latency_ms",
        "judge_status", "judged_at",
    ], cols
    scols = [r[1] for r in raw("PRAGMA table_info(sessions)")]
    assert scols == ["session_id", "user_id", "phase", "expression", "parity_group", "session_start", "session_end",
                     "declared", "declared_by", "declared_text", "stuck_count", "miss_count", "help_count", "strength"], scols
    idx = {r[1]: r[2] for r in raw("PRAGMA index_list(sessions)")}
    assert idx.get("idx_sessions_user_phase") == 1, "UNIQUE(user_id, phase)"
    print("OK スキーマ: chat_logs 40列（question_phrase 追加）・sessions 14列（declared_text 追加）・UNIQUE(user_id, phase)")

    # ===== 強度の遷移規則（decide_strength は状態遷移。カウンタから毎回計算し直さない） =====
    ds = main.decide_strength
    # 0→1 は stuck が 2 に達したときだけ
    assert ds(0, stuck_up=True, stuck_after=1) == (0, "none"), "同構造1回目では介入しない"
    assert ds(0, stuck_up=True, stuck_after=2) == (1, "stuck")
    assert ds(0, miss_up=True) == (0, "none"), "強度0では miss だけでは上がらない"
    assert ds(0, help_up=True) == (1, "help"), "強度0でも help（明示的な援助要求）で 1 に上がる（9/17 修正④B）"
    assert ds(0, stuck_up=True, stuck_after=2, miss_up=True) == (1, "stuck"), "0→1 は必ず1段"
    # 強度1以降は stuck / miss / help のどれが増えても +1（上限3）
    assert ds(1, stuck_up=True, stuck_after=3) == (2, "stuck")
    assert ds(1, miss_up=True) == (2, "miss")
    assert ds(1, help_up=True) == (2, "help")
    assert ds(2, help_up=True) == (3, "help")
    assert ds(3, stuck_up=True, stuck_after=9) == (3, "none"), "上限3で据え置き（trigger は none）"
    assert ds(3, miss_up=True) == (3, "none") and ds(3, help_up=True) == (3, "none")
    # 同じターンで stuck と miss が両方増えても +1 は1回。trigger は miss を優先
    assert ds(1, stuck_up=True, stuck_after=3, miss_up=True) == (2, "miss")
    # 何も増えなければ据え置き
    assert ds(2) == (2, "none") and ds(1) == (1, "none")
    # 新構造到達で 0
    assert ds(3, is_new=True) == (0, "none") and ds(1, is_new=True, miss_up=True) == (0, "none")
    # v3.1：強度0で対話だけが続く（talk_up）→ 1。1以上では talk では上げない。help と同時なら help を優先
    assert ds(0, talk_up=True) == (1, "talk") and ds(1, talk_up=True) == (1, "none") and ds(2, talk_up=True) == (2, "none")
    assert ds(0, help_up=True, talk_up=True) == (1, "help")
    assert main.TALK_STALL_TURNS == 3
    print("OK 強度の遷移: 0→1 は stuck=2 / help / talk（対話3ターン）、1以降はどのカウンタが増えても +1（上限3）、新構造で 0")

    # ===== 認証 =====
    assert client.get("/admin/api/config").status_code == 401
    assert client.get("/admin").status_code == 401
    assert client.get("/admin/api/config", headers=BAD).status_code == 401
    assert client.get("/admin/api/config", headers=AUTH).status_code == 200
    assert client.get("/api/config").status_code == 200  # 認証不要
    print("OK 認証: /admin* は401、正しいパスワードで200、/api/config は認証不要")

    # ===== 式のカウンターバランス（4章） =====
    cfg = client.get("/api/config").json()
    assert cfg["phase"] == 1 and cfg["expression"] is None, "式は児童ごとなのでログイン前は返さない"
    assert cfg["expression_assignment"] == {g: {str(ph): main.config.normalize_expression(e) for ph, e in dd.items()}
                                            for g, dd in main.EXPRESSION_ASSIGNMENT.items()}
    assert cfg["expression_assignment"] == {"odd": {"1": "21 ÷ 3", "2": "24 ÷ 4", "3": "30 ÷ 5"},
                                            "even": {"1": "30 ÷ 5", "2": "24 ÷ 4", "3": "21 ÷ 3"}}
    assert cfg["expression_choices"] == ["21 ÷ 3", "24 ÷ 4", "30 ÷ 5"], "「式を変更」の選択肢は設定表に現れる式の集合"
    a = post("/api/login", user_id="01").json()
    b = post("/api/login", user_id="02").json()
    assert a["expression"] == "21 ÷ 3" and b["expression"] == "30 ÷ 5"
    assert raw("SELECT expression FROM sessions WHERE session_id=?", a["session_id"])[0][0] == "21 ÷ 3"
    q = client.get(f"/api/config?user_id=01&session_id={a['session_id']}").json()
    assert q["expression"] == "21 ÷ 3"
    assert client.get(f"/api/config?user_id=02&session_id={a['session_id']}").json()["expression"] is None, "他人のセッションの式は返さない"
    print("OK 式: 01→21÷3 / 02→30÷5（フェーズ1）。/api/config は本人のセッションの式だけ返す")

    # ===== フェーズ1：ログイン＝探して無ければ作る（1児童1フェーズ1セッション） =====
    a2 = post("/api/login", user_id="01").json()
    assert a["session_id"] == a2["session_id"], "同じフェーズで再ログインしても同一セッション"
    assert a["phase"] == 1 and a["show_support"] is False
    c = post("/api/login", user_id="03").json()
    assert len({a["session_id"], b["session_id"], c["session_id"]}) == 3
    sidA, sidB, sidC = a["session_id"], b["session_id"], c["session_id"]
    try:
        raw("INSERT INTO sessions (user_id, phase, expression, parity_group, session_start) VALUES ('01', 1, '21 ÷ 3', 'odd', '2026-09-18 10:00:00')")
        raise AssertionError("UNIQUE が効いていない")
    except sqlite3.IntegrityError:
        pass
    sess = raw("SELECT parity_group, session_start, session_end FROM sessions WHERE session_id=?", sidA)[0]
    assert sess[0] == "odd" and sess[2] is None
    assert raw("SELECT parity_group FROM sessions WHERE session_id=?", sidB)[0][0] == "even"
    now_jst = datetime.now(database.JST).replace(tzinfo=None)
    started = datetime.strptime(sess[1], "%Y-%m-%d %H:%M:%S")
    assert abs((now_jst - started).total_seconds()) < 120, (sess[1], now_jst)
    print("OK セッション: UNIQUE(user_id, phase)・parity_group（奇偶）・session_start は JST")

    # 所有権チェック
    assert post("/api/judge", session_id=sidA, user_id="02", message="T: x").status_code == 403
    assert post("/api/session/resume", session_id=sidA, user_id="02").status_code == 403
    assert post("/api/session/resume", session_id=sidA, user_id="01").status_code == 200
    print("OK 所有権: 他人の session_id への judge/resume は403")

    # フェーズ1の提出：作問は判定を待たずに「おくったよ」（judge_status=pending）。判定は応答後（judge_queue）。
    # response_type / ai_message は記録しない
    j_before = CALLS["judge"]
    r1 = judge(sidA, "01", "T: おりがみ21まいを3人で")
    assert r1["message"] == "おくったよ" and r1["response_type"] is None and r1["dialog"] is None
    assert r1["valid"] is None and r1["structure"] is None and r1["history"] == [] and r1["accepted"] is False
    judge(sidA, "01", "T: クッキー21こを3人で")
    judge(sidA, "01", "X: 場面矛盾")
    rt = judge(sidA, "01", "わからない")
    assert rt["message"] == "おくったよ"
    assert judge_queue.wait_idle(10), "応答後の判定が終わらない"
    assert CALLS["judge"] == j_before + 3, "作問3件が応答後に判定される（対話は判定しない）"
    logs = logs_of(sidA)
    assert [l["judge_status"] for l in logs] == ["done", "done", "done", None]
    assert all(l["judged_at"] for l in logs[:3]) and logs[3]["judged_at"] is None
    assert [l["response_type"] for l in logs] == [None] * 4
    assert [l["ai_message"] for l in logs] == [None] * 4
    assert [l["prompt_strength"] for l in logs] == [None] * 4
    assert [l["phase"] for l in logs] == [1, 1, 1, 1]
    assert logs[0]["structure"] == "tobun" and logs[0]["is_new"] is True and logs[0]["produced_structures"] == "tobun"
    assert logs[1]["is_new"] is False and logs[2]["issue"] == "scene_contradiction"
    assert [l["stuck_count"] for l in logs] == [0] * 4 and [l["miss_count"] for l in logs] == [0] * 4
    assert all(l["strength"] is None and l["strength_trigger"] is None and l["help_count"] is None and l["target_structure"] is None
               for l in logs), "フェーズ1では支援に関わる列は空"
    assert all(l["expression"] == "21 ÷ 3" for l in logs)
    assert all(l["latency_ms"] is not None for l in logs)
    resumed = post("/api/session/resume", session_id=sidA, user_id="01").json()
    assert [p["text"] for p in resumed["problems"]] == ["T: おりがみ21まいを3人で", "T: クッキー21こを3人で", "X: 場面矛盾"], \
        "フェーズ1・3の右パネルは送った作問の全部（不成立も。対話は除く）"
    assert all(p["structure"] is None for p in resumed["problems"]) and resumed["history"] == []
    print("OK フェーズ1: 表示は『おくったよ』のみ。判定・produced は記録、カウンタは動かない、response_type は空。右パネルに作問の一覧")

    # ===== 応答後の判定（フェーズ1・3）：pending → done、送信順の is_new、失敗 → failed → 再判定 =====
    pl = post("/api/login", user_id="05").json()
    sidQ = pl["session_id"]
    # 判定をブロックして「応答時は pending・valid NULL」を確かめる
    import threading
    gate = threading.Event()
    real_fake_judge = judge_queue.ai_judge.judge
    def blocking_judge(message, expression, user_id=None):
        gate.wait(10)
        return real_fake_judge(message, expression, user_id)
    judge_queue.ai_judge.judge = blocking_judge
    try:
        t0 = __import__("time").perf_counter()
        judge(sidQ, "05", "T: a")
        judge(sidQ, "05", "T: b")
        judge(sidQ, "05", "H: c")
        assert __import__("time").perf_counter() - t0 < 2, "応答が判定を待っている"
        lq = logs_of(sidQ)
        assert [l["judge_status"] for l in lq] == ["pending"] * 3
        assert all(l["valid"] is None and l["structure"] is None and l["is_new"] is None and l["produced_structures"] is None for l in lq)
        assert all(l["input_type"] == "sakumon" and l["latency_ms"] is not None for l in lq)
        st = client.get("/admin/api/live", headers=AUTH).json()["judge"]
        assert st["pending"] == 3 and st["queued"] == 3
        # pending 中の同じ本文の再送は API を呼ばない（resend）
        assert judge(sidQ, "05", "H: c")["input_type"] == "resend"
    finally:
        gate.set()
    assert judge_queue.wait_idle(10)
    judge_queue.ai_judge.judge = real_fake_judge
    lq = [l for l in logs_of(sidQ) if l["input_type"] == "sakumon"]
    assert [l["judge_status"] for l in lq] == ["done"] * 3
    assert [l["valid"] for l in lq] == [True, True, True]
    assert [l["is_new"] for l in lq] == [True, False, True], "is_new は送信順（同じ児童は直列に判定）"
    assert [l["produced_structures"] for l in lq] == ["tobun", "tobun", "tobun,hougan"]
    assert [l["structure"] for l in lq] == ["tobun", "tobun", "hougan"]
    assert all(l["response_type"] is None and l["ai_message"] is None and l["strength"] is None for l in lq)
    assert database.get_produced("05", 1) == ["tobun", "hougan"]
    # 分類の失敗：フェーズ1・3は作問（pending）に倒す（フェーズ2は対話に倒す＝後段で検査）
    assert judge(sidQ, "05", "?: 分類できない")["input_type"] == "sakumon" and LAST_CLASSIFY["fallback"] == "sakumon"
    assert judge_queue.wait_idle(10)
    lc = logs_of(sidQ)[-1]
    assert lc["judge_status"] == "failed" and lc["issue"] == "error", "モックの judge は '?' を知らないので失敗扱い"
    client.delete(f"/admin/api/logs/{lc['log_id']}", headers=AUTH)
    # 失敗 → failed（issue=error）→ 同じ本文の再送は判定し直す → 管理画面の再判定で done
    judge(sidQ, "05", "E: だめ")
    assert judge_queue.wait_idle(10)
    lf = logs_of(sidQ)[-1]
    assert lf["judge_status"] == "failed" and lf["issue"] == "error" and lf["valid"] is None
    st = client.get("/admin/api/live", headers=AUTH).json()["judge"]
    assert st["failed"] == 1 and st["pending"] == 0
    assert judge(sidQ, "05", "E: だめ")["input_type"] == "sakumon", "直前が判定失敗なら再送ではなく判定し直す"
    assert judge_queue.wait_idle(10)
    assert client.get("/admin/api/live", headers=AUTH).json()["judge"]["failed"] == 2
    # 再判定：モックを成立に差し替えて再投入（実運用では API 復旧後に押す）
    _JUDGE["E"] = dict(_JUDGE["B"])
    r = admin_post("/admin/api/rejudge")
    assert r["requeued"] == 2
    assert judge_queue.wait_idle(10)
    _JUDGE["E"] = {"valid": False, "structure": "invalid", "unknown": None, "issue": "error", "error": "boom",
                   "meta": {"retry_count": 3, "latency_ms": 65000, "status": "failed"}}
    lq = [l for l in logs_of(sidQ) if l["input_type"] == "sakumon"]
    assert [l["judge_status"] for l in lq] == ["done"] * 5 and lq[3]["structure"] == "bai" and lq[3]["is_new"] is True
    assert lq[4]["is_new"] is False, "再判定でも is_new は送信順"
    assert client.get("/admin/api/live", headers=AUTH).json()["judge"]["failed"] == 0
    assert admin_post("/admin/api/rejudge")["requeued"] == 0
    # 32人の同時送信：全員分が判定され、各自の is_new が送信順に正しい
    admin_post("/admin/api/phase", phase=1)
    sids = {}
    for i in range(40, 72):
        uid = f"{i:02d}"
        sids[uid] = post("/api/login", user_id=uid).json()["session_id"]
        for m in ("T: x", "T: y", "B: z"):
            judge(sids[uid], uid, m)
    assert judge_queue.wait_idle(30)
    for uid, sid in sids.items():
        lq = [l for l in logs_of(sid) if l["input_type"] == "sakumon"]
        assert [l["judge_status"] for l in lq] == ["done"] * 3 and [l["is_new"] for l in lq] == [True, False, True], uid
    for uid, sid in sids.items():
        client.delete(f"/admin/api/sessions/{sid}", headers=AUTH)
    print("OK 応答後の判定: 応答時は pending、完了で done（is_new は送信順）、失敗は failed→再判定、32人同時でも全件 done")

    # ===== 「作った お話を 見る」（フェーズ1・3の選択） =====
    def sel_open(sid, uid):
        return post("/api/selection/open", session_id=sid, user_id=uid)
    def sel_submit(sid, uid, ids):
        return post("/api/selection/submit", session_id=sid, user_id=uid, log_ids=ids)
    # 0問：一覧なし・max 0。開いた記録は残る
    s0 = post("/api/login", user_id="60").json()["session_id"]
    r = sel_open(s0, "60"); assert r.status_code == 200, r.text
    assert r.json() == {"problems": [], "max_select": 0, "last_log_ids": None, "last_at": None}
    assert sel_submit(s0, "60", []).status_code == 400
    assert raw("SELECT kind FROM selection_events WHERE session_id=?", s0) == [("open",)]
    # 2問（うち1問は不成立・1問は未判定でも並ぶ）：max 2。対話・再送は並ばない。判定結果は返さない
    s2 = post("/api/login", user_id="61").json()["session_id"]
    judge(s2, "61", "T: a"); judge(s2, "61", "X: b")
    assert judge(s2, "61", "X: b")["input_type"] == "resend"
    judge(s2, "61", "わからない")
    r = sel_open(s2, "61").json()
    assert [p["no"] for p in r["problems"]] == [1, 2] and [p["text"] for p in r["problems"]] == ["T: a", "X: b"]
    assert r["max_select"] == 2 and set(r["problems"][0].keys()) == {"no", "log_id", "text"}, "判定結果・構造は返さない"
    ids2 = [p["log_id"] for p in r["problems"]]
    assert sel_submit(s2, "61", ids2 + [ids2[0]]).status_code == 400, "重複"
    assert sel_submit(s2, "61", [ids2[0], 999999]).status_code == 400, "他人・存在しない行"
    assert sel_submit(s2, "61", [ids2[0]]).status_code == 200, "1つでも送れる"
    assert judge_queue.wait_idle(10)
    # 5問：max 3。4つは 400。3つは 200。選び直し → 作問を追加 → 追加分が一覧に載り、選び直せる。記録はすべて残る
    s5 = post("/api/login", user_id="62").json()["session_id"]
    for m in ("T: 1", "T: 2", "H: 3", "B: 4", "N: 5"):
        judge(s5, "62", m)
    r = sel_open(s5, "62").json()
    ids5 = [p["log_id"] for p in r["problems"]]
    assert len(ids5) == 5 and r["max_select"] == 3 and r["last_log_ids"] is None
    assert sel_submit(s5, "62", ids5[:4]).status_code == 400
    assert sel_submit(s5, "62", [ids5[0], ids5[2], ids5[3]]).status_code == 200
    judge(s5, "62", "B: 6")
    r = sel_open(s5, "62").json()
    assert len(r["problems"]) == 6 and r["problems"][5]["no"] == 6 and r["problems"][5]["text"] == "B: 6"
    assert r["last_log_ids"] == [ids5[0], ids5[2], ids5[3]], "直前の選択を返す（画面でチェック済みにする）"
    id6 = r["problems"][5]["log_id"]
    assert sel_submit(s5, "62", [ids5[0], ids5[2], id6]).status_code == 200
    ev = raw("SELECT kind, log_ids FROM selection_events WHERE session_id=? ORDER BY id", s5)
    assert [e[0] for e in ev] == ["open", "submit", "open", "submit"]
    assert ev[3][1] == f"[{ids5[0]}, {ids5[2]}, {id6}]"
    assert raw("SELECT COUNT(*) FROM selection_events WHERE session_id=? AND created_at LIKE ?", s5, f"{now_jst.year}-%")[0][0] == 4
    assert judge_queue.wait_idle(10)
    # 管理画面：送信済み人数・声がけ・児童詳細・セッションの選択情報
    live = client.get("/admin/api/live", headers=AUTH).json()
    assert live["selection"]["1"]["submitted"] == 2 and live["selection"]["1"]["teacher_call"] is None
    assert admin_post("/admin/api/teacher_call", phase=1)["phase"] == 1
    admin_post("/admin/api/teacher_call", phase=1)
    assert client.post("/admin/api/teacher_call", json={"phase": 2}, headers=AUTH).status_code == 400
    tc = client.get("/admin/api/live", headers=AUTH).json()["selection"]["1"]["teacher_call"]
    assert tc["count"] == 2 and tc["first_at"] <= tc["last_at"]
    det = client.get("/admin/api/students/62", headers=AUTH).json()["sessions"][0]
    assert det["selection"]["last_log_ids"] == [ids5[0], ids5[2], id6] and det["selection"]["submit_count"] == 2
    assert det["selection"]["first_open_at"] is not None
    si = client.get(f"/admin/api/sessions/{s5}/selection", headers=AUTH).json()
    assert [p["no"] for p in si["last_problems"]] == [1, 3, 6] and len(si["events"]) == 4
    # フェーズ2では使えない（ボタンも画面も無い）
    admin_post("/admin/api/phase", phase=2)
    sP2 = post("/api/login", user_id="62").json()["session_id"]
    assert sel_open(sP2, "62").status_code == 400 and sel_submit(sP2, "62", [ids5[0]]).status_code == 400
    admin_post("/admin/api/phase", phase=1)
    # CSV：selected（最終選択の行=1、作問行のみ）・初回 open・最終選択・声がけ
    rows = list(csv.DictReader(io.StringIO(client.get("/admin/api/export/csv", headers=AUTH).content.decode("utf-8-sig"))))
    r62 = [x for x in rows if x["user_id"] == "62" and x["phase"] == "1"]
    assert [x["selected"] for x in r62] == ["1", "0", "1", "0", "0", "1"]
    assert all(x["selection_last_log_ids"] == f"[{ids5[0]}, {ids5[2]}, {id6}]" and x["selection_last_at"] and x["selection_first_open_at"]
               and x["teacher_call_first_at"] == tc["first_at"] for x in r62)
    r61 = [x for x in rows if x["user_id"] == "61"]
    assert [x["selected"] for x in r61] == ["1", "0", "", ""], "再送・対話の行は空"
    r60 = [x for x in rows if x["user_id"] == "60"]
    assert r60 == [], "0問の児童はログ行が無い（選択イベントは DB に残る）"
    assert all(x["selected"] == "" and x["selection_first_open_at"] == "" for x in rows if x["user_id"] == "01" and x["phase"] == "1")
    for sid_ in (s0, s2, s5):
        client.delete(f"/admin/api/sessions/{sid_}", headers=AUTH)
    assert raw("SELECT COUNT(*) FROM selection_events")[0][0] == 0, "セッション削除で選択イベントも消える"
    print("OK 3つえらぶ: 0問／2問／5問の一覧と上限、選び直し（追加した作問を含む・記録は全件）、声がけ、児童詳細、CSV、フェーズ2は不可")

    # ===== フェーズ2へ切替（別セッション：フェーズ1は引き継がない） =====
    admin_post("/admin/api/phase", phase=2)
    assert client.get("/api/config").json()["phase"] == 2
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sidA)[0][0] is not None
    a3 = post("/api/session/new", user_id="01").json()
    assert a3["session_id"] != sidA and a3["expression"] == "24 ÷ 4"
    assert post("/api/session/new", user_id="01").json()["session_id"] == a3["session_id"]
    assert post("/api/session/new", user_id="02").json()["expression"] == "24 ÷ 4"
    sid2A = a3["session_id"]
    assert a3["show_support"] is True and a3["declared"] is None and a3["dialog"] is None
    assert a3["history"] == [] and a3["problems"] == [] and a3["conversation"] == []
    assert "choices" not in a3, "3択は廃止"
    post("/api/session/new", user_id="03")
    print("OK フェーズ2: 新セッション（式 24÷4）で開始し、フェーズ1の産出・到達構造・会話を引き継がない")

    # ===== 3章の文言（praise / form / done / talk） =====
    pb = CALLS.get("phrase", 0)
    r = judge(sid2A, "01", "T: おりがみが24まいあります。【4人で 分けます】。1人何まいですか。")
    assert r["response_type"] == "praise" and r["is_new"] is True and r["prompt_strength"] == 0
    assert r["message"] == ("新しい 問題が できたね！ この お話は、『1人何まいですか』を 求める お話だね。\n"
                            "今度は、**求める ものが ちがう** お話は 作れるかな？"), r["message"]
    assert CALLS["phrase"] == pb + 1 and logs_of(sid2A)[-1]["divisor_phrase"] == "4人で 分けます", "除数の句を判定と並行して取り、ログに残す"
    assert logs_of(sid2A)[-1]["question_phrase"] == "1人何まいですか", "問いの文（正規表現）をログに残す"
    assert r["accepted"] is True and r["history"] == ["tobun"]
    llm_before = CALLS["llm"]
    assert LAST_CLASSIFY["fallback"] == "taiwa", "フェーズ2の分類失敗は対話に倒す"
    r = judge(sid2A, "01", "T: ジュース24Lを4人で")
    assert r["response_type"] == "praise" and r["is_new"] is False and r["prompt_strength"] == 0
    assert r["message"] == ("いいね、また 一つ できたね。\n"
                            "今度は、**求める ものが ちがう** お話も 作れそうかな？"), "問いの文が無ければ引用の文を落とす"
    assert logs_of(sid2A)[-1]["divisor_phrase"] == "T: ジュース24Lを4人で" and logs_of(sid2A)[-1]["question_phrase"] is None
    assert CALLS["llm"] == llm_before, "praise は定型（LLM を呼ばない）"
    r = judge(sid2A, "01", "X: だめ")
    assert r["response_type"] == "form" and r["valid"] is False
    assert r["message"] == "お話の 中の 数が、何の 数か たしかめて みよう。求める ことと 合って いるかな？"
    assert CALLS["llm"] == llm_before, "form も定型"
    r = judge(sid2A, "01", "N: あめが24こあります。4人にくばります。")
    assert r["message"] == "求める ことを きく 文が ないみたいだよ。さいごに「〜は いくつですか」を 書いて みよう。"
    r = judge(sid2A, "01", "W: あめが20こあります。4人でわけると1人何こですか。")
    assert r["message"] == "24と 4を つかう 問題に しよう。いまの お話だと しきが ちがって しまうよ。"
    r = judge(sid2A, "01", "P: わりざんは たのしいと おもいます。")
    assert r["message"] == "まだ お話に なって いないみたいだよ。「〜が あります」から はじめて みよう。"
    assert logs_of(sid2A)[-1]["divisor_phrase"] is None, "不成立では句を残さない"
    r = judge(sid2A, "01", "むずかしい")
    assert r["response_type"] == "talk" and r["message"] == "[talk] llm" and r["stuck_count"] == 1
    r = judge(sid2A, "01", "H: あめ24こを4こずつ")
    assert r["response_type"] == "praise" and r["is_new"] is True and sorted(r["history"]) == ["hougan", "tobun"]
    r = judge(sid2A, "01", "B: 24本は4本の何倍")
    assert r["response_type"] == "done" and r["all_reached"] is True
    assert r["message"] == ("3つ とも できたね！ 1ばん・3ばん・4ばんは、同じ 24÷4 なのに 求める ものが ぜんぶ ちがう お話だね。\n"
                            "時間まで、もっと 作って みよう。求める ものが 同じでも、ちがう お話なら いいよ。"), \
        "疑問語が3つそろわなければ番号だけ（3ばんに問いが無い）"
    pb = CALLS.get("phrase", 0)
    r = judge(sid2A, "01", "B: 24人は4人の何倍")
    assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["declared"] is None
    assert r["message"] == "5ばんも できたね。この お話は『B: 24人は4人の何倍』を 求める お話だね。", "完了後は完了文をくり返さない"
    assert r["accepted"] is True
    assert CALLS["phrase"] == pb, "3つそろった後は句を取らない"
    # 成立作問と同じ本文（直前でなくても）→ 判定せず「もう ◯ばんに あるよ」（v3.1）
    cb = dict(CALLS)
    r = judge(sid2A, "01", "H: あめ24こを 4こずつ")
    assert r["input_type"] == "resend" and r["accepted"] is False and CALLS["judge"] == cb["judge"] and CALLS["classify"] == cb["classify"]
    assert r["message"] == "その お話は もう 3ばんに あるよ。ちがう お話を 作って みよう。", r["message"]
    assert logs_of(sid2A)[-1]["input_type"] == "resend" and logs_of(sid2A)[-1]["stuck_count"] == 1
    p2 = logs_of(sid2A)
    assert [l["response_type"] for l in p2] == ["praise", "praise", "form", "form", "form", "form", "talk", "praise", "done", "done", "talk"]
    assert [l["stuck_count"] for l in p2] == [0, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1]
    assert [l["miss_count"] for l in p2] == [0] * 11
    assert [l["strength"] for l in p2] == [0] * 11 and [l["strength_trigger"] for l in p2] == ["none"] * 11
    assert [l["help_count"] for l in p2] == [0] * 11 and all(l["target_structure"] is None for l in p2)
    assert all(l["ai_message"] for l in p2), "フェーズ2は全ターン ai_message を記録"
    for l in p2:
        no_banned(l["ai_message"])
    resumed = post("/api/session/resume", session_id=sid2A, user_id="01").json()
    assert len(resumed["conversation"]) == 11 and resumed["conversation"][0]["prompt_strength"] == 0
    assert [p["structure"] for p in resumed["problems"]] == ["tobun", "tobun", "hougan", "bai", "bai"], "同一本文の再送は一覧に載らない"
    print("OK 文言: praise(新/反復・除数の句を引用)・form(4種)・done は仕様の表どおり。不成立・対話でカウンタ不動、新構造で0。同一本文は再送")

    # ===== 判定エラー（3-2 error）・再送 =====
    sidB2 = post("/api/session/new", user_id="02").json()["session_id"]
    r = judge(sidB2, "02", "T: 成立")
    assert r["response_type"] == "praise" and r["is_new"] is True
    r = judge(sidB2, "02", "E: err")
    assert r["response_type"] == "error" and r["message"] == "うまく よみとれなかったよ。もう一度 おくって みてね。"
    assert r["accepted"] is False and r["valid"] is None and r["structure"] is None
    row = logs_of(sidB2)[-1]
    assert row["issue"] == "error" and row["response_type"] == "error" and row["stuck_count"] == 0 and row["miss_count"] == 0
    resumed = post("/api/session/resume", session_id=sidB2, user_id="02").json()
    assert [p["text"] for p in resumed["problems"]] == ["T: 成立"], "判定エラーは一覧に載せない"
    # 判定エラーの直後に同じ本文を送り直す → 判定し直す（resend にしない）
    jb = CALLS["judge"]
    r = judge(sidB2, "02", "E: err")
    assert CALLS["judge"] == jb + 1 and logs_of(sidB2)[-1]["input_type"] == "sakumon"
    # 判定済みの本文の連続再送 → API を呼ばず直前の結果を返す（カウンタは動かない）
    r = judge(sidB2, "02", "T: べつの")
    assert r["stuck_count"] == 1
    calls_before = dict(CALLS)
    r = judge(sidB2, "02", "T: べつの")
    assert CALLS == calls_before, "再送で classify / judge / LLM を呼ばない"
    assert r["accepted"] is False and r["message"] == d._fill(d.PRAISE_REPEAT_NOPHRASE, "24 ÷ 4") and r["stuck_count"] == 1
    row = logs_of(sidB2)[-1]
    assert row["input_type"] == "resend" and row["latency_ms"] is None and row["stuck_count"] == 1
    resumed = post("/api/session/resume", session_id=sidB2, user_id="02").json()
    assert sum(1 for p in resumed["problems"] if p["text"] == "T: べつの") == 1, "再送で一覧に重複しない"
    print("OK 判定エラー: 3-2 の文言・一覧に載せない・送り直しは判定し直す。判定済み本文の再送は API を呼ばずカウンタ不動")

    # 待ち状態の問い合わせ（送信中でなければ null）
    assert client.get("/api/judge/status?user_id=02").json() == {"state": None}
    assert client.get("/api/judge/status?user_id=abc").status_code == 400

    # ===== is_new はフェーズスコープ =====
    database.save_log(session_id=99999, user_id="02", phase=2, expression="24 ÷ 4", input_type="sakumon",
                      message="別セッションの倍", ai_message=None, valid=True, structure="bai", unknown="ratio",
                      is_new=True, produced_structures=["tobun", "bai"], stuck_count=0, miss_count=0)
    assert database.get_produced("02", 2) == ["tobun", "bai"]
    r = judge(sidB2, "02", "B: 24本は4本の何倍")
    assert r["is_new"] is False, "別セッションで既出の構造は is_new=0"
    assert database.get_produced("02", 1) == []
    raw("DELETE FROM chat_logs WHERE session_id=99999")
    print("OK is_new: フェーズスコープ（別セッションでも既出なら 0、別フェーズは数えない）")

    # ===== 状態機械（仕様 v2 7章の9項目） =====
    sidS = post("/api/login", user_id="11").json()["session_id"]

    # (1) 不成立を3回連続 → stuck_count は 0 のまま（form のみ）
    for i in range(3):
        r = judge(sidS, "11", f"X: 24このあめを4こずつくばります。1人何こですか{i}")
        assert r["response_type"] == "form" and r["stuck_count"] == 0 and r["miss_count"] == 0
    assert [l["stuck_count"] for l in logs_of(sidS)] == [0, 0, 0]
    print("OK 状態機械(1): 不成立3連続で stuck_count は 0 のまま")

    # (2) 等分除→等分除→等分除 → stuck 0→1→2、3問目で弱（現在地の対比＋宣言の問い。1ターン）
    r = judge(sidS, "11", "T: おりがみ24まいを4人で")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 0 and r["is_new"] is True
    r = judge(sidS, "11", "T: あめが24こあります。【4人に 分けます】。1人何こですか。")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 1
    assert r["strength"] == 0 and r["strength_trigger"] == "none", "同構造1回目では強度は 0 のまま"
    r = judge(sidS, "11", "T: みかんが24こあります。【4人で 分けます】。1人分は何こになりますか。")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 1 and r["stuck_count"] == 2, r
    assert r["strength"] == 1 and r["strength_trigger"] == "stuck" and r["help_count"] == 0
    assert database.get_session(sidS)["strength"] == 1, "強度は sessions に状態として保持"
    assert r["message"] == ("2ばんの『4人に 分けます』も 3ばんの『4人で 分けます』も、4の 使い方は 同じで、どちらも **何こか**を 求める お話だね。\n"
                            "じゃあ 次は、何を 求める お話に する？"), r["message"]
    for label in d.STRUCTURE_LABEL.values():
        assert label not in r["message"], ("弱で構造のラベルを出さない", label)
    for w in ("人数", "1人分", "分ける お話", "くらべる"):
        assert w not in r["message"], ("弱で行き先・役割名を言わない", w)
    assert r["dialog"] == "declaration" and r["declared"] is None, "弱：宣言の答え待ち。まだ予告は立たない"
    assert logs_of(sidS)[-1]["awaiting"] == "declaration"
    resumed = post("/api/session/resume", session_id=sidS, user_id="11").json()
    assert resumed["dialog"] == "declaration", "再入場でも宣言待ちを復元"
    print("OK 状態機械(2): 同構造 0→1→2、3問目で弱（同じ構造の直近2問の除数の句と問いの疑問語を引用して対比 → 宣言の問い）")

    # (3) 宣言「4こずつ配る」→ declared=hougan, declared_by=child。返事は1回で閉じ、児童の言葉が目標に固定表示される
    db = CALLS["declare"]
    r = post("/api/judge", session_id=sidS, user_id="11", message="4こずつ配る", declaring=True).json()
    assert r["input_type"] == "declaration" and r["classified"] == "hougan" and r["dialog"] is None, r
    assert r["message"] == "じゃあ、その お話を 作って みよう。" and r["response_type"] == "prompt" and r["prompt_strength"] == 1
    assert CALLS["declare"] == db + 1 and LAST_DECLARE["expression"] == "24 ÷ 4", "分類には式を渡す"
    assert r["declared"] == "hougan" and r["declared_by"] == "child" and r["declared_text"] == "4こずつ配る"
    assert r["target_label"] == "4こずつ配る", "児童が宣言したときは児童の言葉をそのまま出す（構造のラベルは出さない）"
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "child" and st["declared_text"] == "4こずつ配る"
    assert st["stuck_count"] == 2 and st["miss_count"] == 0 and st["strength"] == 1, "宣言ではカウンタ・強度を動かさない"
    dd = logs_of(sidS)[-1]
    assert dd["input_type"] == "declaration" and dd["declared_structure"] == "hougan" and dd["declared_by"] == "child"
    assert dd["target_structure"] == "hougan" and dd["strength"] == 1 and dd["awaiting"] is None
    assert dd["message"] == "4こずつ配る" and dd["ai_message"] == "じゃあ、その お話を 作って みよう。" and dd["latency_ms"] is not None
    resumed = post("/api/session/resume", session_id=sidS, user_id="11").json()
    assert resumed["declared"] == "hougan" and resumed["target_label"] == "4こずつ配る" and resumed["dialog"] is None, "目標の固定表示"
    # 宣言が済んだあとに declaring で届いた作問以外の文は対話（待っているターンが無い）
    r = post("/api/judge", session_id=sidS, user_id="11", message="りんごの問題", declaring=True).json()
    assert r["input_type"] == "taiwa" and r["declared"] == "hougan", "待っているターンが無ければ対話。予告は消さない"
    # 対話モードでも作問を書けば通常の作問処理（予告として扱わない）
    r = post("/api/judge", session_id=sidS, user_id="11", message="X: 予告モードで作問", declaring=True).json()
    assert r["input_type"] == "sakumon" and r["response_type"] == "form" and r["declared"] == "hougan"
    # declaring なし（通常）の対話は talk のまま。/api/declare も直接使える（宣言の言葉を上書き）
    r = judge(sidS, "11", "むずかしい")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk"
    r = post("/api/declare", session_id=sidS, user_id="11", text="1つ分がいくつか").json()
    assert r["declared"] == "tobun" and r["declared_by"] == "child" and r["target_label"] == "1つ分がいくつか"
    r = post("/api/declare", session_id=sidS, user_id="11", text="いくつ分をきく").json()
    assert r["declared"] == "hougan" and r["target_label"] == "いくつ分をきく"
    assert post("/api/declare", session_id=sidA, user_id="01", text="いくつ分").status_code == 400
    print("OK 状態機械(3): 宣言「4こずつ配る」→ declared=hougan / child、返事は1回で閉じる。作問なら通常処理、unknown は立てない")

    # (4) その次に等分除 → declaration_met=0, miss=1 → 中（行き先の指定＋題材固定。3択は無い）
    r = judge(sidS, "11", "T: えんぴつが24本あります。4人で分けます。1人分は何本ですか。@えんぴつ/本")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 2, r
    assert r["declaration_met"] is False and r["miss_count"] == 1 and r["stuck_count"] == 3
    assert r["strength"] == 2 and r["strength_trigger"] == "miss", "stuck と miss が同時に増えても +1 は1回。trigger は miss"
    assert r["message"] == ("何人に 分けられるかを 求める お話に して みよう。4を、1人が もらう 数に するよ。"
                            "えんぴつの お話は そのままで いいよ。"), r["message"]
    row = logs_of(sidS)[-1]
    assert row["item"] == "えんぴつ" and row["unit"] == "本", "判定が読み取った物・助数詞をログに残す"
    assert row["question_phrase"] == "1人分は何本ですか"
    assert row["strength"] == 2 and row["strength_trigger"] == "miss" and row["target_structure"] == "hougan" and row["help_count"] == 0
    assert r["dialog"] is None and "choices" not in r, "中：3択は出さない。入力欄は作問モードのまま"
    row = logs_of(sidS)[-1]
    assert row["declared_structure"] == "hougan" and row["declared_by"] == "child" and row["declaration_met"] is False
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "system" and st["declared_text"] is None, "中：prompt 発行と同時にシステムが目標を立てる"
    assert r["declared"] == "hougan" and r["declared_by"] == "system" and r["declared_text"] is None
    assert r["target_label"] == "何人に 分けられるかを 求める お話", "システム指定は行き先（求めるもの）の言葉（構造のラベル単独は出さない）"
    assert post("/api/session/resume", session_id=sidS, user_id="11").json()["dialog"] is None
    assert client.post("/api/self_label", json={"session_id": sidS, "user_id": "11", "choice": "bai"}).status_code == 404, "/api/self_label は廃止"
    print("OK 状態機械(4): 予告と違う構造 → declaration_met=0, miss=1 → 中（行き先の指定。3択・/api/self_label は廃止）")

    # ---- taiwa に渡す状況（フェーズD）：強度・目標・到達構造名・成立作問の一覧と直前の問題（本文＋わる数の役割＋物） ----
    r = judge(sidS, "11", "8ってなんの数？")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk" and r["prompt_strength"] == 2
    c = LAST_TALK["context"]
    assert LAST_TALK["history"] == ["tobun"], "到達構造は構造名で渡す"
    assert c["strength"] == 2 and c["target"] == "hougan", c
    assert c["last_problem"]["text"] == "T: えんぴつが24本あります。4人で分けます。1人分は何本ですか。@えんぴつ/本"
    assert c["last_problem"]["structure"] == "tobun" and c["last_problem"]["divisor_role"] == "分ける相手の数（人数など）"
    assert c["last_problem"]["item"] == "えんぴつ" and c["last_problem"]["unit"] == "本"
    assert len(c["problems"]) == 4 and all(p["divisor_role"] == "分ける相手の数（人数など）" for p in c["problems"])
    assert logs_of(sidS)[-1]["prompt_strength"] == 2 and logs_of(sidS)[-1]["input_type"] == "taiwa"
    assert database.get_session(sidS)["stuck_count"] == 3 and database.get_session(sidS)["miss_count"] == 1, "taiwa でカウンタ不動"
    # システムプロンプト：強度2では役割の指示・3語・題材固定が許可、場面文は不可。強度0・1では3語を出さない
    sys2 = d._build_system("24 ÷ 4", ["tobun"], c)
    assert "支援の強さ: 2" in sys2 and "1人が もらう 数に するよ" in sys2 and "場面文（お話の文そのもの）は渡さない" in sys2
    assert "えんぴつの お話は そのままで いいよ" in sys2 and "4 が表しているもの: 分ける相手の数" in sys2 and "答え（数値 6）" in sys2
    assert "問いの文（求めるもの）: 『1人分は何本ですか』" in sys2, "成立作問の問いの文を渡す"
    sys0 = d._build_system("24 ÷ 4", [], {"strength": 0})
    assert "先生から答え（役割）も言わない" in sys0 and "「1つ分の 大きさ」「いくつ分」「何倍」「1人分」という言葉は使わない" in sys0
    assert "分類して言わない" in sys0 and "現在地を示してよい" not in sys0, "強度0は現在地も示さない"
    sys1 = d._build_system("24 ÷ 4", [], {"strength": 1})
    assert "役割を言わず" in sys1 and "行き先は言わない" in sys1 and "現在地を示してよい" in sys1
    assert "何を 求める お話に する？" in sys1 and "求めるもの」の意味を聞かれたら" in sys1 and "求めるもの」の意味を聞かれたら" in sys0
    assert "行き先を言ってよい" in sys2 and "行き先を言ってよい" in d._build_system("24 ÷ 4", ["tobun"], {**c, "strength": 3})
    sys3 = d._build_system("24 ÷ 4", ["tobun"], {**c, "strength": 3})
    assert "場面文を渡してよい" in sys3 and "えんぴつが 24本 あります" in sys3
    for text in (sys0, sys2, sys3):
        assert "数量関係（だれが何をどう分けるか" not in text, "旧の全面禁止は撤廃"
        assert "「同じ」「ちがう」という主張が判定と食い違っていたら、共感のために肯定しない" in text
        assert "内容を確かめずに褒めない" in text
    print("OK taiwa の状況: 強度・目標・到達構造名・成立作問（本文＋役割＋物）を渡し、話してよいことは強度で切り替わる")

    # (5) 中の直後にまた予告と違う構造 → miss=2 → 強（場面固定の文言・ref_no は最新の表示番号）
    r = judge(sidS, "11", "T: ジュース24Lを4人で@ジュース/L")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 3, r
    assert r["declaration_met"] is False and r["miss_count"] == 2 and r["stuck_count"] == 4
    assert r["strength"] == 3 and r["strength_trigger"] == "miss"
    assert r["message"] == "「ジュースが 24L あります。1人に 4Lずつ 分けます。」\nつづきの、求める 文を 書いて みよう。", r["message"]
    assert r["target_label"] == "何人に 分けられるかを 求める お話"
    assert r["dialog"] is None and r["declared"] == "hougan" and r["declared_by"] == "system"
    row = logs_of(sidS)[-1]
    assert row["declared_by"] == "system" and row["declaration_met"] is False
    assert row["strength"] == 3 and row["strength_trigger"] == "miss" and row["target_structure"] == "hougan"
    print("OK 状態機械(5): 中の直後にまた不一致 → miss=2 → 強（場面文提示：いくつ分）")

    # 中・強の対話に答えず作問を送っても止まらない（対話は打ち切り、予告は立ったまま）
    r = judge(sidS, "11", "X: 途中で作問")
    assert r["response_type"] == "form" and r["declared"] == "hougan" and r["miss_count"] == 2
    assert r["strength"] == 3 and r["strength_trigger"] == "none", "不成立では強度も動かない"
    row = logs_of(sidS)[-1]
    assert row["strength"] == 3 and row["strength_trigger"] == "none" and row["target_structure"] == "hougan", "form 行も強度・目標を記録"
    print("OK 対話を無視した作問: ブロックしない・予告は立ったまま")

    # (6) 包含除に到達 → stuck / miss が両方 0 に戻り、強度0の称賛
    r = judge(sidS, "11", "H: あめ24こを4こずつ")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["is_new"] is True, r
    assert r["message"] == d.praise_message(True, "24 ÷ 4", None) == "新しい 問題が できたね！\n今度は、**求める ものが ちがう** お話は 作れるかな？"
    assert r["target_label"] is None
    assert r["stuck_count"] == 0 and r["miss_count"] == 0 and r["declaration_met"] is True and r["declared"] is None
    assert r["strength"] == 0 and r["help_count"] == 0 and r["strength_trigger"] == "none"
    st = database.get_session(sidS)
    assert st["stuck_count"] == 0 and st["miss_count"] == 0 and st["declared"] is None
    assert st["strength"] == 0 and st["help_count"] == 0, "新構造到達で3カウンタと強度を全て 0"
    row = logs_of(sidS)[-1]
    assert row["declared_structure"] == "hougan" and row["declared_by"] == "system" and row["declaration_met"] is True
    assert row["produced_structures"] == "tobun,hougan"
    print("OK 状態機械(6): 新構造到達で stuck / miss が両方 0、強度0の称賛、予告は消費")

    # (7) 3構造そろう → done。以降支援なし
    r = judge(sidS, "11", "B: 24本は4本の何倍")
    assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["all_reached"] is True
    assert r["message"] == ("3つ とも できたね！ 1ばん・6ばん・7ばんは、同じ 24÷4 なのに 求める ものが ぜんぶ ちがう お話だね。\n"
                            "時間まで、もっと 作って みよう。求める ものが 同じでも、ちがう お話なら いいよ。"), r["message"]
    for i in range(4):
        r = judge(sidS, "11", f"T: 3つそろった後の反復{i}")
        assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["declared"] is None and r["dialog"] is None, r
        assert r["strength"] == 0 and r["message"] == f"{8 + i}ばんも できたね。", r["message"]
    # 完了後の対話：支援は出ない（help を送っても強度は 0 のまま）
    r = judge(sidS, "11", "ヒント")
    assert r["response_type"] == "talk" and r["strength"] == 0 and r["strength_trigger"] == "none"
    print("OK 状態機械(7): 3構造で done（児童の問いの疑問語で3つを並べる）、以降は「◯ばんも できたね」で予告支援は出ない")

    # ---- 弱の変形（v3.1）：到達構造の集合で現在地の言い方が変わる。宣言 unknown は立てず定型で作問に戻す ----
    # 分ける系を両方到達（残りは倍）→「どれも 分ける お話」
    sidQ = post("/api/login", user_id="14").json()["session_id"]
    judge(sidQ, "14", "T: a"); judge(sidQ, "14", "T: b")
    r = judge(sidQ, "14", "H: あめが24こあります。4こずつふくろに入れます。ふくろは何まいいりますか。")
    assert r["prompt_strength"] == 0 and r["is_new"] is True and r["dialog"] is None
    judge(sidQ, "14", "H: b2")
    r = judge(sidQ, "14", "H: クッキーが24まいあります。1人に4まいずつ配ります。何人に配れますか。@クッキー/まい")
    assert r["prompt_strength"] == 1 and r["dialog"] == "declaration"
    assert r["message"] == ("これまでの お話は、どれも 24まいを 分ける お話だね。\n"
                            "じゃあ 次は、24まいを 分けない お話に するなら、何を 求める？"), ("1ばんに問いが無いので番号なし", r["message"])
    assert d.weak_variant(database.get_valid_problems(sidQ))[0] == "wakeru"
    assert d.weak_message([{"structure": "hougan", "unit": "こ", "question_phrase": "何人にくばれますか"},
                           {"structure": "tobun", "unit": "こ", "question_phrase": "1人分は何こですか"},
                           {"structure": "tobun", "unit": "まい", "question_phrase": "1人何まいですか"}], "24 ÷ 4") == (
        "1ばんは 何人か、2ばんは 何こかを 求めたね。どちらも 24まいを 分ける お話だね。\n"
        "じゃあ 次は、24まいを 分けない お話に するなら、何を 求める？"), "wakeru：各構造の最初の問題の疑問語（助数詞は最新の問題）"
    # 宣言 unknown だが中身のある言葉（題材だけ）→ 構造は立てないが児童の言葉として引き取る（『…』だね）。
    # 上部「つぎは」にその言葉を出す（9/18：「折り紙の数」に「わからなくても だいじょうぶ」と返さない）
    r = post("/api/judge", session_id=sidQ, user_id="14", message="りんごの問題", declaring=True).json()
    assert r["input_type"] == "declaration" and r["classified"] == "unknown" and r["declared"] is None and r["dialog"] is None
    assert r["message"] == "『りんごの問題』だね。じゃあ、その お話を 作って みよう。", r["message"]
    assert r["declared_by"] == "child" and r["declared_text"] == "りんごの問題" and r["target_label"] == "りんごの問題"
    assert logs_of(sidQ)[-1]["declared_structure"] is None and logs_of(sidQ)[-1]["awaiting"] is None
    assert logs_of(sidQ)[-1]["ai_message"] == r["message"], "unknown でも返事を記録する（無応答にしない）"
    # 同じ「りんごの問題」をもう一度 → 再送。直前の返事をそのまま返す（「おくったよ」を出さない）
    r = judge(sidQ, "14", "りんごの問題")
    assert r["input_type"] == "resend" and r["message"] == "『りんごの問題』だね。じゃあ、その お話を 作って みよう。"
    # 構造が無いので次の作問で miss は動かない（declaration_met なし）。作問で児童の言葉は消え、システムの目標に置き換わる
    r = judge(sidQ, "14", "H: ドーナツが24こあります。4こずつ箱に入れます。箱は何箱いりますか。")
    assert r["prompt_strength"] == 2 and r["strength"] == 2 and r["strength_trigger"] == "stuck" and r["declaration_met"] is None
    assert r["declared"] == "bai" and r["declared_by"] == "system" and r["declared_text"] is None, "未到達は bai"
    assert r["message"] == ("24こは 4この 何倍かを 求める お話に して みよう。4を、くらべる 相手の 数に するよ。"
                            "6ばんの お話は そのままで いいよ。"), ("item が取れないときの中", r["message"])
    assert r["target_label"] == "24こは 4この 何倍かを 求める お話"
    # 倍だけを2問以上 →「どれも くらべる お話」。1問だけ（help で上がる）→ one
    sidR = post("/api/login", user_id="16").json()["session_id"]
    judge(sidR, "16", "B: 【24本は 4本の 何倍】ですか@リボン/本")
    r = judge(sidR, "16", "わからない")
    assert r["strength"] == 1 and r["strength_trigger"] == "help" and r["dialog"] == "declaration"
    assert r["message"] == ("[talk] llm\n1ばんは、4を『24本は 4本の 何倍』と 使って、**何倍か**を 求める お話だったね。\n"
                            "じゃあ 次は、何を 求める お話に する？"), r["message"]
    # 宣言「わからない」（直前の「わからない」と同じ本文だが、答えを待っているので再送にしない）→ 立てない。help も動かない
    cb = dict(CALLS)
    r = post("/api/judge", session_id=sidR, user_id="16", message="わからない", declaring=True).json()
    assert r["input_type"] == "declaration" and r["declared"] is None and r["dialog"] is None and r.get("is_help_request") is None
    assert r["message"] == "わからなくても だいじょうぶ。じゃあ、求める ものが ちがう お話を 作って みよう。", r["message"]
    assert r["declared_by"] is None and r["target_label"] is None, "「わからない」は引き取らない"
    assert CALLS["declare"] == cb["declare"] + 1 and CALLS["llm"] == cb["llm"], "宣言は classify_declaration だけ（talk は呼ばない）"
    assert database.get_session(sidR)["help_count"] == 1 and database.get_session(sidR)["strength"] == 1
    # 宣言が立たないまま同構造 → stuck 1 → 1→2（stuck）。目標は未到達の先頭（tobun）
    r = judge(sidR, "16", "B: 【24こは 4この 何倍】ですか@あめ/こ")
    assert r["prompt_strength"] == 2 and r["strength"] == 2 and r["strength_trigger"] == "stuck" and r["declaration_met"] is None
    assert r["message"] == "1人分が 何こかを 求める お話に して みよう。4を、分ける 人の 数に するよ。あめの お話は そのままで いいよ。", r["message"]
    assert r["target_label"] == "1人分が 何こかを 求める お話"
    assert d.weak_variant(database.get_valid_problems(sidR))[0] == "kuraberu"
    # 中（目標 tobun）で help → 2→3。talk の文言（強度2で生成）は捨て、定型の前置き＋強の場面文（上部ラベルだけ変えて黙らない・矛盾させない）
    r = judge(sidR, "16", "ヒント")
    assert r["is_help_request"] is True and r["prompt_strength"] == 2 and r["strength"] == 3 and r["strength_trigger"] == "help"
    assert r["declared"] == "tobun" and r["dialog"] is None
    assert r["message"] == ("そっか。じゃあ、こうして みよう。\n「あめが 24こ あります。4人で 同じ 数ずつ 分けます。」\n"
                            "つづきの、求める 文を 書いて みよう。"), r["message"]
    r = judge(sidR, "16", "もっと ヒント")
    assert r["strength"] == 3 and r["strength_trigger"] == "none" and r["message"] == "[talk] llm", "上限で据え置きなら連結しない"
    assert database.get_session(sidR)["help_count"] == 3, "help_count は数える"
    assert d.weak_message(database.get_valid_problems(sidR), "24 ÷ 4") == (
        "1ばんも 2ばんも、24こと 4こを くらべて、**何倍か**を 求める お話だね。\n"
        "じゃあ 次は、くらべない お話に するなら、何を 求める お話に する？")
    # 句が取れない同構造2問 → 番号だけで対比。1問で句なし → 番号で言う
    sidU = post("/api/login", user_id="18").json()["session_id"]
    judge(sidU, "18", "T: a"); judge(sidU, "18", "T: b")
    r = judge(sidU, "18", "T: c")
    assert r["prompt_strength"] == 1 and r["message"] == ("2ばんも 3ばんも、4の 使い方は 同じで、求める ものも 同じだね。\n"
                                                          "じゃあ 次は、何を 求める お話に する？"), r["message"]
    assert d.weak_message([{"structure": "tobun", "divisor_phrase": None, "unit": None}], "24 ÷ 4") == \
        "1ばんの お話とは 求める ものが ちがう お話に するなら、何を 求める お話に する？"
    assert d.weak_message([{"structure": "tobun", "divisor_phrase": None, "unit": None, "question_phrase": "1人分は何こですか"}], "24 ÷ 4") == \
        "1ばんは、**何こか**を 求める お話だったね。\nじゃあ 次は、何を 求める お話に する？"
    assert d.weak_message([{"structure": "tobun", "divisor_phrase": "4人で 分けます", "unit": None}], "24 ÷ 4") == \
        "1ばんは、4を『4人で 分けます』と 使った お話だったね。\nじゃあ 次は、何を 求める お話に する？"
    assert d.weak_message([{"structure": "hougan", "divisor_phrase": "4こずつ 買います", "question_phrase": "何人でいけばいいですか"},
                           {"structure": "hougan", "divisor_phrase": "4つ くばると", "question_phrase": "何ふくろできますか"}], "24 ÷ 4") == (
        "2ばんの『4こずつ 買います』も 3ばんの『4つ くばると』も、4の 使い方は 同じで、求める ものも 同じだね。\n"
        "じゃあ 次は、何を 求める お話に する？").replace("2ばん", "1ばん").replace("3ばん", "2ばん"), "疑問語が違えば疑問語は出さない"
    # 誤答（到達済み構造の宣言）は訂正しない：そのまま作れば stuck で中へ、一致なので miss は動かない
    r = post("/api/judge", session_id=sidU, user_id="18", message="4人で分ける", declaring=True).json()
    assert r["declared"] == "tobun" and r["declared_by"] == "child" and r["message"] == "じゃあ、その お話を 作って みよう。"
    r = judge(sidU, "18", "T: d")
    assert r["declaration_met"] is True and r["miss_count"] == 0 and r["stuck_count"] == 3
    assert r["prompt_strength"] == 2 and r["strength_trigger"] == "stuck" and r["declared"] == "hougan" and r["declared_by"] == "system"
    assert d._sentence_with_number("みかんが24こあります。8人で同じ数ずつ分けます。1人分は何こですか。", 8) == "8人で同じ数ずつ分けます"
    assert d._sentence_with_number("みかんが24こあって8人で同じ数ずつ分けると1人分は何こになるでしょうかというもんだいです。", 8) is None, "40字超は引用しない"
    assert d._sentence_with_number("18こを3人で", 8) is None, "18 の 8 は除数ではない"
    assert d.expected_divisor_role("tobun", "one_unit") == "people" and d.expected_divisor_role("hougan", "num_units") == "per_one"
    assert d.expected_divisor_role("bai", "ratio") == "base" and d.expected_divisor_role("bai", "base") is None
    assert d.expected_divisor_role("invalid", None) is None
    assert d.format_quote("　1人分は 何こ  ですか。") == "1人分は 何こ ですか"
    assert d.format_quote("「何倍ですか？」") == "何倍ですか？" and d.format_quote("。") is None and d.format_quote(None) is None
    print("OK 弱の変形: 分ける両方→「どれも 分ける」、倍だけ→「どれも くらべる」、1問→「〜じゃ ない お話」、句なし→番号。unknown は定型で戻す。誤答は訂正しない")

    # ===== 修正①（9/17）：AI が問いを出した直後の入力は原則対話（awaiting）。完全な問題文だけ作問 =====
    lp = ai_classify.looks_like_problem
    assert lp("24台の車があります。1台に4人のるとすると何台ひつようですか。") is True
    assert lp("24まいのおりがみがあります。ぜんぶで何人におりがみをくばれますか。") is False, "数量が1つ"
    assert lp("24本のえんぴつを分けるときのハコの数") is False, "問いの文で終わらない"
    assert lp("あめ24こを4人でわけると1人なんこ？") is True and lp("２４こを４こずつ。何人？") is True
    assert lp("24こを4人で分けます。") is False and lp("4は人数") is False
    assert d.asks_child("[talk] どこが むずかしかったかな？") is True and d.asks_child("[talk] llm") is False
    assert d.asks_child("じゃあ 次は、4を どんな ふうに 使った お話に する？") is True and d.asks_child("いいね。") is False
    sidW = post("/api/login", user_id="20").json()["session_id"]
    judge(sidW, "20", "T: えんぴつが24本あります。4人で分けます。1人分は何本ですか。@えんぴつ/本")
    r = judge(sidW, "20", "X: 24台の車があります。1台に4人のるとすると何台ひつようですか。")
    assert r["response_type"] == "form" and logs_of(sidW)[-1]["awaiting"] is None, "form の問いかけは待ち状態にしない"
    # form の直後の断片（指摘への返事）は判定に回さない（9/17 画面：「一人4こ」→ not_problem になっていた）
    lf = ai_classify.looks_like_fragment
    assert lf("一人4こ") and lf("ハコの数") and lf("4人で") and lf("24こ")
    assert not lf("あめが24こあります。") and not lf("24このあめを4人にくばります。") and not lf("1人分は何こですか？")
    cb = dict(CALLS)
    r = judge(sidW, "20", "一人4こ")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk" and CALLS["judge"] == cb["judge"] and CALLS["classify"] == cb["classify"]
    r = judge(sidW, "20", "X: 24台の車があります。1台に4人のるとすると何台ひつようですか。")
    assert r["response_type"] == "form"
    r = judge(sidW, "20", "N: あめが24こあります。4人にくばります。")
    assert r["input_type"] == "sakumon" and r["response_type"] == "form", "form の直後でも問い忘れの作り直しは判定する"
    r = judge(sidW, "20", "どういうことですか？")
    assert r["input_type"] == "taiwa" and r["message"].endswith("かな？") and r["dialog"] is None
    assert logs_of(sidW)[-1]["awaiting"] == "answer", "talk の問い返し → awaiting=answer"
    # 回帰3：問い返しへの答え（数量1つ・問いの文でない）は対話。classify も judge も呼ばない
    cb = dict(CALLS)
    r = judge(sidW, "20", "24本のえんぴつを分けるときのハコの数")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk", r
    assert CALLS["judge"] == cb["judge"] and CALLS["classify"] == cb["classify"] and CALLS["llm"] == cb["llm"] + 1
    assert logs_of(sidW)[-1]["awaiting"] is None, "問いで終わらない talk は待たない"
    # v3.1：強度0で作問のあと対話が3ターン続いた（この行で3つ目）→ 0→1（trigger=talk）。talk の後ろに弱の文言を連結して宣言を待つ
    r = judge(sidW, "20", "どういうこと？")
    assert r["strength"] == 1 and r["strength_trigger"] == "talk" and r["help_count"] == 0 and r["prompt_strength"] == 0, r
    assert r["message"] == ("[talk] どこが むずかしかったかな？\n"
                            "1ばんは、4を『4人で分けます』と 使って、**何本か**を 求める お話だったね。\n"
                            "じゃあ 次は、何を 求める お話に する？"), r["message"]
    assert r["dialog"] == "declaration" and logs_of(sidW)[-1]["awaiting"] == "declaration" and logs_of(sidW)[-1]["strength_trigger"] == "talk"
    assert database.get_session(sidW)["strength"] == 1 and database.get_session(sidW)["help_count"] == 0
    # 回帰4：awaiting 中でも完全な問題文は作問として判定（宣言待ちは打ち切り）。新構造で 0 に戻る
    cb = dict(CALLS)
    r = judge(sidW, "20", "H: あめが24こあります。4こずつふくろに入れます。ふくろは何まいいりますか。")
    assert r["input_type"] == "sakumon" and r["is_new"] is True and CALLS["judge"] == cb["judge"] + 1
    assert CALLS["classify"] == cb["classify"], "awaiting 中は classify を呼ばない"
    assert logs_of(sidW)[-1]["awaiting"] is None and r["strength"] == 0
    # 待ち状態の中で同じ本文が届いたら「答え」（再送にしない）。待っていなければ再送（待ち状態を写す）
    judge(sidW, "20", "どういうこと？")
    cb = dict(CALLS)
    judge(sidW, "20", "どういうこと？")
    assert CALLS["llm"] == cb["llm"] + 1 and [l["input_type"] for l in logs_of(sidW)[-2:]] == ["taiwa", "taiwa"]
    assert [l["awaiting"] for l in logs_of(sidW)[-2:]] == ["answer", "answer"]
    r = judge(sidW, "20", "H: b")
    assert r["input_type"] == "taiwa", "awaiting 中の断片は対話"
    assert r["strength"] == 1 and r["strength_trigger"] == "talk", "対話3ターン目（この行）で 0→1"
    r = judge(sidW, "20", "H: 24このクッキーを4こずつ配ります。何人に配れますか。")
    assert r["prompt_strength"] == 2 and r["strength_trigger"] == "stuck" and r["dialog"] is None, "強度1からの同構造は中"
    assert r["declared"] == "bai" and r["target_label"] == "24こは 4この 何倍かを 求める お話"
    r = judge(sidW, "20", "H: 24このあめを4こずつ配ります。何人に配れますか。")
    assert r["prompt_strength"] == 3 and r["declaration_met"] is False and r["strength_trigger"] == "miss"
    assert r["message"].startswith("「たろうさんは ものを 24こ、お友だちは 4こ もって います。」") or r["message"].startswith("「はなこさんは")
    print("OK 修正①: AI の問いの直後は対話（judge / classify を呼ばない）。完全な問題文だけ作問。対話3ターンで 0→1（talk）")

    # (8) taiwa / resend でカウンタが動かない。強度0の「わからない」は help=1 → 強度1（回帰5）＋弱（対比＋宣言の問い）を連結
    sidT = post("/api/login", user_id="12").json()["session_id"]
    judge(sidT, "12", "T: a"); judge(sidT, "12", "T: b")
    assert database.get_session(sidT)["stuck_count"] == 1
    r = judge(sidT, "12", "わからない")
    assert r["is_help_request"] is True and r["prompt_strength"] == 0, "文言は更新前の強度（0）で生成"
    assert r["strength"] == 1 and r["strength_trigger"] == "help" and r["help_count"] == 1, "回帰5：強度0の help で 1 に上がる"
    assert r["message"] == ("[talk] llm\n1ばんも 2ばんも、4の 使い方は 同じで、求める ものも 同じだね。\n"
                            "じゃあ 次は、何を 求める お話に する？"), "0→1 では talk の後ろに弱の文言を連結"
    assert r["dialog"] == "declaration" and r["declared"] is None
    row = logs_of(sidT)[-1]
    assert row["awaiting"] == "declaration" and row["strength"] == 1 and row["strength_trigger"] == "help" and row["prompt_strength"] == 0
    assert post("/api/session/resume", session_id=sidT, user_id="12").json()["dialog"] == "declaration", "再入場でも宣言待ちを復元"
    # 宣言「人数」→ tobun（到達済みでも訂正しない）。児童の言葉が目標に出る
    r = post("/api/judge", session_id=sidT, user_id="12", message="人数", declaring=True).json()
    assert r["input_type"] == "declaration" and r["declared"] == "tobun" and r["declared_by"] == "child" and r["dialog"] is None
    assert r["target_label"] == "人数" and r.get("is_help_request") is None
    # 目標が立った後の「人数」（同じ本文・待ち状態なし）→ 再送。直前の返事を写す
    r = judge(sidT, "12", "人数")
    assert r["input_type"] == "resend" and r["message"] == "じゃあ、その お話を 作って みよう。" and r["declared"] == "tobun"
    lt = logs_of(sidT)
    assert [l["input_type"] for l in lt] == ["sakumon", "sakumon", "taiwa", "declaration", "resend"]
    assert [l["stuck_count"] for l in lt] == [0, 1, 1, 1, 1]
    assert [l["miss_count"] for l in lt] == [0] * 5
    st1 = database.get_session(sidT)
    assert st1["stuck_count"] == 1 and st1["miss_count"] == 0 and st1["strength"] == 1 and st1["help_count"] == 1
    assert [l["is_help_request"] for l in lt] == [None, None, True, None, None], "taiwa だけ判定。resend・declaration は未判定"
    print("OK 状態機械(8): taiwa / resend では stuck / miss が動かない。強度0の help で 1 に上がり、弱 → 宣言と進む")

    # ---- 支援要求（フェーズE）：強度1以上では help が増えるたびに +1。中に上がれば目標を立てる。反映は次ターン ----
    r = judge(sidT, "12", "ヒント ちょうだい")
    assert r["is_help_request"] is True and r["prompt_strength"] == 1, "文言は更新前の強度（1）で生成"
    assert r["strength"] == 2 and r["strength_trigger"] == "help" and r["help_count"] == 2
    assert r["declared"] == "tobun" and r["declared_by"] == "child" and r["target_label"] == "人数", "児童の宣言が立っていればそのまま"
    assert r["dialog"] is None and r["message"] == ("そっか。じゃあ、こうして みよう。\n1人分が 何こかを 求める お話に して みよう。"
                                                    "4を、分ける 人の 数に するよ。2ばんの お話は そのままで いいよ。"), \
        ("1→2（help）では talk の文言を捨てて前置き＋中の文言（行き先の指定）。弱は連結しない", r["message"])
    st = database.get_session(sidT)
    assert st["strength"] == 2 and st["help_count"] == 2 and st["stuck_count"] == 1 and st["miss_count"] == 0
    r = judge(sidT, "12", "これって足し算？")
    assert r["is_help_request"] is False and r["strength"] == 2 and r["strength_trigger"] == "none" and r["help_count"] == 2
    assert LAST_TALK["context"]["strength"] == 2 and LAST_TALK["context"]["target"] == "tobun", "次ターンから新しい強度・目標で talk"
    # 宣言（tobun・到達済み）どおりに作る → 一致（miss 0）だが stuck → 2→3。システムが未到達（hougan）を立てる
    r = judge(sidT, "12", "T: c")
    assert r["declaration_met"] is True and r["miss_count"] == 0 and r["stuck_count"] == 2
    assert r["strength"] == 3 and r["strength_trigger"] == "stuck" and r["declared"] == "hougan" and r["declared_by"] == "system"
    assert r["target_label"] == "何人に 分けられるかを 求める お話"
    r = judge(sidT, "12", "どうすればいいの")
    assert r["strength"] == 3 and r["strength_trigger"] == "none" and r["help_count"] == 3 and r["declared"] == "hougan", "上限3で据え置き"
    r = judge(sidT, "12", "ヒント")
    assert r["strength"] == 3 and r["help_count"] == 4 and r["strength_trigger"] == "none"
    # 目標（hougan）と一致する新構造 → 全カウンタと強度が 0、予告は消費
    r = judge(sidT, "12", "H: あめ24こを8こずつ")
    assert r["is_new"] is True and r["declaration_met"] is True and r["response_type"] == "praise"
    assert r["strength"] == 0 and r["help_count"] == 0 and r["stuck_count"] == 0 and r["miss_count"] == 0 and r["declared"] is None
    lt = logs_of(sidT)
    assert lt[-2]["is_help_request"] is True and lt[-2]["prompt_strength"] == 3 and lt[-1]["is_help_request"] is None
    assert lt[-1]["strength"] == 0 and lt[-1]["help_count"] == 0 and lt[-1]["target_structure"] is None
    helps = [l for l in lt if l["input_type"] == "taiwa" and l["strength_trigger"] == "help"]
    got = [(l["prompt_strength"], l["strength"], l["help_count"], l["target_structure"]) for l in helps]
    assert got == [(0, 1, 1, None), (1, 2, 2, "tobun")], ("taiwa 行：prompt_strength=更新前、strength=更新後、target=発話が参照した目標", got)
    # 3つそろった後は支援要求でも何も動かない
    judge(sidT, "12", "B: 24本は8本の何倍")
    r = judge(sidT, "12", "ヒント")
    assert r["is_help_request"] is True and r["strength"] == 0 and r["help_count"] == 0 and r["declared"] is None and r["dialog"] is None
    print("OK 支援要求(E): help で 0→1（弱を連結）→2→3（上限）。文言は更新前の強度、反映は次ターン。新構造で全部 0")

    # ===== 修正③④A（9/17）：talk の文脈（直前のやりとり）・強度0の役割ガード・対話中の役割回答の評価 =====
    sidV = post("/api/login", user_id="22").json()["session_id"]
    judge(sidV, "22", "H: えんぴつが24本あります。4本ずつハコに入れます。ハコは何こいりますか。@えんぴつ/本")
    r = judge(sidV, "22", "X: 24台の車があります。1台に4人のるとすると何台ひつようですか。")
    r = judge(sidV, "22", "どういうことですか？")
    lt_ctx = LAST_TALK["context"]["last_turn"]
    assert lt_ctx["child"] == "X: 24台の車があります。1台に4人のるとすると何台ひつようですか。" and lt_ctx["issue"] == "scene_contradiction"
    assert lt_ctx["input_type"] == "sakumon" and lt_ctx["valid"] is False and lt_ctx["response_type"] == "form"
    assert lt_ctx["ai"] == d.form_message("scene_contradiction", "24 ÷ 4")
    sysx = d._build_system("24 ÷ 4", ["hougan"], LAST_TALK["context"])
    assert "直前のやりとり" in sysx and "理由: 場面と問いがかみ合っていない" in sysx and "24台の車" in sysx, "直前の児童入力＋判定理由＋AI応答を渡す"
    assert "直前の子どもの問題文に即して" in sysx and "答え（数値）や、直した問題文は言わない" in sysx, "最優先規則"
    sys0 = d._build_system("24 ÷ 4", ["hougan"], {**LAST_TALK["context"], "strength": 0})
    sys1 = d._build_system("24 ÷ 4", ["hougan"], {**LAST_TALK["context"], "strength": 1})
    assert "子どもに問わない" in sys0 and "問い返してよい" not in sys0, "強度0は促しのみ"
    assert "問い返してよい" in sys1 and "子どもに問わない" not in sys1, "強度1は問い返し"
    assert "直前のやりとり: まだない" in d._build_system("24 ÷ 4", [], {"strength": 0})
    # 回帰7：役割の問いの検出とガード（強度0だけ違反）
    assert d.asks_role("お話の 中の 4は 何の 数かな？", 4) and d.asks_role("4は 何を あらわして いる？", 4) and d.asks_role("4って なんの 数だった？", 4)
    assert d.asks_role("2ばんの お話で、4は 何を あらわして いるかな？", 4)
    assert not d.asks_role("4ばんの お話は 何を 求めて いるかな？", 4), "番号の 4 は除数ではない"
    assert not d.asks_role("お話の 中で 4と 書いた ところを もう一度 読んで みよう", 4), "目を向けさせるだけなら問いではない"
    assert not d.asks_role("何こ あるものは 何かな？", 4) and not d.asks_role("24本の えんぴつ だね", 4)
    assert d.violates_boundary("お話の 中の 4は 何の 数かな？", "talk", "24 ÷ 4", strength=0) == "role_question"
    assert d.violates_boundary("お話の 中の 4は 何の 数かな？", "talk", "24 ÷ 4", strength=1) is None
    assert d.violates_boundary("4ばんの お話は 何を 求めて いるかな？", "talk", "24 ÷ 4", strength=0) is None
    # 実物の _llm_message：強度0で役割を問う出力 → 理由を添えて再生成 → なお違反なら定型文に差し替え
    class _Resp:
        def __init__(self, text): self.content = [type("B", (), {"type": "text", "text": text})()]; self.stop_reason = "end_turn"
    outputs, calls = [], []
    def fake_call(user_id, parse, **kw):
        calls.append(kw["messages"][0]["content"])
        return parse(_Resp(outputs.pop(0))), {"retry_count": 0, "status": "ok"}
    import json as _json
    role_q = _json.dumps({"check": "", "message": "お話の 中の 4は 何の 数かな？", "state": "x", "is_help_request": False})
    ok_msg = _json.dumps({"check": "", "message": "お話の 中で 4と 書いた ところを もう一度 読んで みよう。", "state": "x", "is_help_request": False})
    orig_call = d.llm_call.call
    d.llm_call.call = fake_call
    try:
        outputs[:] = [role_q, ok_msg]
        out = REAL_LLM_MESSAGE("どういうこと？", "taiwa", None, ["hougan"], [], "talk", "24 ÷ 4", context={"strength": 0})
        assert out["message"] == "お話の 中で 4と 書いた ところを もう一度 読んで みよう。" and len(calls) == 2
        assert "範囲を超えていた" in calls[1] and "わる数が何を表しているかを子どもに問わない" in calls[1], "再生成には違反の理由を添える"
        assert "範囲を超えていた" not in calls[0]
        outputs[:] = [role_q, role_q]
        out = REAL_LLM_MESSAGE("どういうこと？", "taiwa", None, ["hougan"], [], "talk", "24 ÷ 4", context={"strength": 0})
        assert out["message"] == "そっか。じゃあ、いま作った 問題とは、求める ものが ちがう お話に して みようか。" and out["state"] == "talk_fallback"
        outputs[:] = [role_q]
        out = REAL_LLM_MESSAGE("4ってなに", "taiwa", None, ["hougan"], [], "talk", "24 ÷ 4", context={"strength": 1})
        assert out["message"] == "お話の 中の 4は 何の 数かな？", "強度1では役割の問い返しは通る"
    finally:
        d.llm_call.call = orig_call
    # 回帰6（v3.1）：help で 0→1 → 弱を連結（1問なので「〜じゃ ない お話」）。talk が役割を問うても awaiting は answer
    #（旧 v3 の役割の評価は廃止。問い返しへの答えは対話として LLM に渡す）
    r = judge(sidV, "22", "わからない")
    assert r["strength"] == 1 and r["strength_trigger"] == "help" and r["dialog"] == "declaration"
    assert r["message"] == ("[talk] llm\n1ばんは、4を『4本ずつハコに入れます』と 使って、**何こか**を 求める お話だったね。\n"
                            "じゃあ 次は、何を 求める お話に する？"), r["message"]
    r = post("/api/judge", session_id=sidV, user_id="22", message="1人に4本ずつ", declaring=True).json()
    assert r["input_type"] == "declaration" and r["declared"] == "hougan" and r["declared_by"] == "child" and r["target_label"] == "1人に4本ずつ"
    r = judge(sidV, "22", "4ってなに？")
    assert r["input_type"] == "taiwa" and r["message"] == "[talk] お話の 中の 4は 何の 数かな？" and r["dialog"] is None
    row = logs_of(sidV)[-1]
    assert row["awaiting"] == "answer" and row["prompt_strength"] == 1 and row["input_type"] == "taiwa"
    assert post("/api/session/resume", session_id=sidV, user_id="22").json()["dialog"] is None
    cb = dict(CALLS)
    r = post("/api/judge", session_id=sidV, user_id="22", message="ハコの数", declaring=True).json()
    assert r["input_type"] == "taiwa" and r["declared"] == "hougan" and CALLS["llm"] == cb["llm"] + 1, \
        "問い返しへの答えは対話（役割の評価はしない）。予告は消さない"
    assert not any(l["input_type"] == "role" for l in logs_of(sidV)), "role 行は作らない"
    print("OK 修正③④A: talk に直前のやりとり（児童入力＋判定理由＋AI応答）を渡す。強度0は役割を問わない（ガード→理由付き再生成→定型文）。"
          "help で 0→1 は弱を連結。役割の宣言（role）は廃止")

    # 中・強の3構造の文言（v3.2：目標（求めるもの）＋手段（除数の使い方）＋題材固定。{item}{unit}{dividend}{divisor} の埋め込み）
    assert d.mid_message("tobun", "21 ÷ 3", 2, "あめ", "こ") == "1人分が 何こかを 求める お話に して みよう。3を、分ける 人の 数に するよ。あめの お話は そのままで いいよ。"
    assert d.mid_message("hougan", "21 ÷ 3", 2, "あめ", "こ") == "何人に 分けられるかを 求める お話に して みよう。3を、1人が もらう 数に するよ。あめの お話は そのままで いいよ。"
    assert d.mid_message("bai", "21 ÷ 3", 2, "えんぴつ", "本") == "21本は 3本の 何倍かを 求める お話に して みよう。3を、くらべる 相手の 数に するよ。えんぴつの お話は そのままで いいよ。"
    assert d.mid_message("tobun", "21 ÷ 3", 4, None, None) == "1人分が 何こかを 求める お話に して みよう。3を、分ける 人の 数に するよ。4ばんの お話は そのままで いいよ。"
    assert d.strong_message("tobun", "21 ÷ 3", 2, "あめ", "こ") == "「あめが 21こ あります。3人で 同じ 数ずつ 分けます。」\nつづきの、求める 文を 書いて みよう。"
    assert d.strong_message("bai", "21 ÷ 3", 2, "えんぴつ", "本", session_id=10) == (
        "「たろうさんは えんぴつを 21本、お友だちは 3本 もって います。」\nつづきの、何倍かを 求める 文を 書いて みよう。")
    assert d.strong_message("bai", "21 ÷ 3", 2, "えんぴつ", "本", session_id=11).startswith("「はなこさんは"), "人物名はセッションごとに周期的"
    assert d.strong_message("hougan", "30 ÷ 5", 2, None, None) == "「ものが 30こ あります。1人に 5こずつ 分けます。」\nつづきの、求める 文を 書いて みよう。"
    # 目標の表示：児童の宣言はその言葉、システム指定は求めるものの文（中の文言の目標部分と同じ形）。構造のラベル単独は出さない
    assert d.target_label("bai", "system", None, "24 ÷ 4", "本") == "24本は 4本の 何倍かを 求める お話"
    assert d.target_label("tobun", "system", None, "24 ÷ 4", "本") == "1人分が 何本かを 求める お話"
    assert d.target_label("hougan", "system", None, "24 ÷ 4") == "何人に 分けられるかを 求める お話"
    assert d.target_label("tobun", "child", "4人で分ける", "24 ÷ 4") == "4人で分ける"
    assert d.target_label("tobun", "child", None, "24 ÷ 4") == "1人分が 何こかを 求める お話", "児童の言葉が無ければ行き先の言葉"
    assert d.target_label(None, None, None, "24 ÷ 4") is None
    # 完了文：各構造に最初に到達した問題の番号と疑問語（到達順）。疑問語が欠ける・重なるなら番号だけ
    probs = [{"structure": "hougan", "question_phrase": "何人にくばれますか"}, {"structure": "hougan", "question_phrase": "何人ですか"},
             {"structure": "tobun", "question_phrase": "1人分は何こですか"}, {"structure": "bai", "question_phrase": "何倍ですか"}]
    assert d.done_message(probs, "24 ÷ 4") == ("3つ とも できたね！ 1ばんは『何人か』、3ばんは『何こか』、4ばんは『何倍か』。\n"
                                              "同じ 24÷4 なのに、求める ものが ぜんぶ ちがう お話に なったね。\n"
                                              "時間まで、もっと 作って みよう。求める ものが 同じでも、ちがう お話なら いいよ。")
    assert d.done_message([{**probs[0], "question_phrase": "何こにくばれますか"}, *probs[1:]], "24 ÷ 4").startswith(
        "3つ とも できたね！ 1ばん・3ばん・4ばんは、同じ 24÷4 なのに 求める ものが ぜんぶ ちがう お話だね。"), "疑問語が重なれば番号だけ"
    # 問いの文・疑問語の抽出（正規表現）
    assert d.extract_question_phrase("おにぎりが24こあります。1人に4こずつくばります。おにぎりは何人にくばれますか。") == "おにぎりは何人にくばれますか"
    assert d.extract_question_phrase("クッキーが24個あります。4人で分けると、1人分は何個になりますか？") == "4人で分けると、1人分は何個になりますか？"
    assert d.extract_question_phrase("あめが24こあります。4人にくばります。") is None
    assert d.interrogative_of("おにぎりは何人にくばれますか") == "何人か" and d.interrogative_of("1人分は何個ですか") == "何こか"
    assert d.interrogative_of("24本は4本のなんばいですか") == "何倍か" and d.interrogative_of("ふくろはいくついりますか") == "いくつか"
    assert d.interrogative_of("何人分ありますか") == "何人分か" and d.interrogative_of(None) is None and d.interrogative_of("6です") is None
    # 「求めるものって何？」の検出と定型（児童の問いの文を指す。行き先は言わない）
    assert d.asks_what_motomeru("求めるものって何ですか") and d.asks_what_motomeru("求めるものがちがうってどういうこと？")
    assert d.asks_what_motomeru("求めるってなに") and d.asks_what_motomeru("求めるものがわかりません")
    assert d.asks_what_motomeru("求めるものがちがうってどう言うことですか？") and d.asks_what_motomeru("求めるものってどーいう意味")
    assert not d.asks_what_motomeru("何を求めればいいの") and not d.asks_what_motomeru("わからない") and not d.asks_what_motomeru("どういうこと？")
    assert d.motomeru_what_message([{"question_phrase": "何人にくばれますか"}, {"question_phrase": "何人で買いにいけばいいですか"}], "24 ÷ 4") == (
        "2ばんの お話の 最後の 文、『何人で買いにいけばいいですか』の ところが『求める もの』だよ。\n今度は、ここが ちがう お話を 作って みよう。")
    assert d.motomeru_what_message([], "24 ÷ 4") == "『求める もの』は、お話の 最後の 文の ことだよ。「〜は いくつですか」の ところ。"
    assert d.motomeru_what_message([{"question_phrase": None}], "24 ÷ 4") == d.motomeru_what_message([], "24 ÷ 4")
    for label in d.STRUCTURE_LABEL.values():
        for t in d.TARGET_LABEL.values():
            assert t != label and "1つ分の 大きさ" not in t and "いくつ分" not in t, "構造ラベル単独は出さない（「何倍か」は求めるものの文の中でだけ）"
    assert not hasattr(d, "ROLE_ASK") and hasattr(d, "weak_variant"), "役割の宣言の文言は廃止"
    for text in [d.PRAISE_NEW, d.PRAISE_NEW_NOPHRASE, d.PRAISE_REPEAT, d.PRAISE_REPEAT_NOPHRASE,
                 d.WEAK_SAME, d.WEAK_SAME_NOPHRASE, d.WEAK_SAME_NOWHAT, d.WEAK_SAME_BARE, d.WEAK_ONE, d.WEAK_ONE_NOPHRASE,
                 d.WEAK_ONE_NOWHAT, d.WEAK_ONE_BARE, d.WEAK_WAKERU, d.WEAK_WAKERU_NOWHAT, d.WEAK_KURABERU,
                 d.DECLARED_MESSAGE, d.DECLARATION_UNKNOWN_MESSAGE, d.DUPLICATE_MESSAGE, d.DONE_MESSAGE, d.DONE_MESSAGE_NOWHAT,
                 d.POST_DONE, d.POST_DONE_NOPHRASE, d.MOTOMERU_WHAT_MESSAGE, d.MOTOMERU_WHAT_NOPHRASE, d.TALK_LEADIN,
                 d.TALK_FALLBACK, d.TALK_FALLBACK_REWRITE, *d.FORM_MESSAGES.values(), *d.MID_MESSAGES.values(),
                 *d.MID_MESSAGES_NOITEM.values(), *d.STRONG_MESSAGES.values(), *d.STRUCTURE_LABEL.values(),
                 *d.TARGET_LABEL.values()]:
        no_banned(text)
    for text in (d.WEAK_SAME, d.WEAK_ONE, d.WEAK_WAKERU, d.WEAK_KURABERU, d.PRAISE_NEW, d.PRAISE_REPEAT):
        for label in d.STRUCTURE_LABEL.values():
            assert label not in text, ("弱・称賛に構造ラベルを出さない", label)
        for w in ("人数", "1人分", "もらう 数", "分ける 人"):
            assert w not in text, ("弱・称賛は行き先（役割）を言わない", w)
    assert d.violates_boundary("この種類のお話はいいね", "talk", "21 ÷ 3") == "banned_vocab:種類"
    assert d.violates_boundary("何をたずねているかな", "talk", "21 ÷ 3") == "banned_vocab:たずね"
    # 境界は強度依存：求める量の語・両方の数は強度0・1でだけ禁止。構造名・答え・語彙・休けいは全強度で禁止
    m = "3を「1人分の 数」に して みよう。えんぴつの お話は そのままで いいよ。"
    assert d.violates_boundary(m, "talk", "21 ÷ 3", strength=1) == "banned_unknown:1人分"
    assert d.violates_boundary(m, "talk", "21 ÷ 3", strength=2) is None
    assert d.violates_boundary("21こを 3こずつ 分けたら？", "talk", "21 ÷ 3", strength=0) == "both_numbers"
    assert d.violates_boundary("21こを 3こずつ 分けたら？", "talk", "21 ÷ 3", strength=3) is None
    for st in (0, 2, 3):
        assert d.violates_boundary("これは等分除だね", "talk", "21 ÷ 3", strength=st) == "banned:等分除"
        assert d.violates_boundary("答えは 7こ だよ", "talk", "21 ÷ 3", strength=st) == "quotient"
        assert d.violates_boundary("7になるね", "talk", "21 ÷ 3", strength=st) == "quotient"
        assert d.violates_boundary("休けいしよう", "talk", "21 ÷ 3", strength=st) == "banned_talk:休"
    assert d.violates_boundary("7ばんの お話の 3は 何かな？", "talk", "21 ÷ 3", strength=0) is None, "番号の 7 は答えではない"
    assert d.violates_boundary("7つ とも 作れそうだね", "talk", "21 ÷ 3", strength=0) is None
    scene = "「えんぴつが 21本 あります。1人に 3本ずつ 分けます。」 この あとに、求める 文を 書いて みよう。何倍 も いいね。えんぴつは そのままで いいよ。"
    assert d.violates_boundary(scene, "talk", "21 ÷ 3", strength=3) is None, "強度3は場面文の長さを許容"
    assert d.violates_boundary(scene * 3, "talk", "21 ÷ 3", strength=3) == "too_long"
    assert d.violates_boundary(scene * 2, "talk", "21 ÷ 3", strength=2) == "too_long", "強度2は140字まで"
    assert d.divisor_role_label("bai", "base", 8) == "倍率（8倍）" and d.divisor_role_label("invalid", None, 8) is None
    # v3.1 のガード：行き先の指定（役割の指定の句）は強度0・1で禁止。「分ける／くらべる お話」は強度0でだけ禁止
    spec = "24このおにぎりを何人かで分けるお話に書きかえて、何人に分けるかを考えてみよう。"
    assert d.violates_boundary(spec, "talk", "24 ÷ 4", strength=0) == "role_spec"
    assert d.violates_boundary(spec, "talk", "24 ÷ 4", strength=1) == "role_spec"
    assert d.violates_boundary(spec, "talk", "24 ÷ 4", strength=2) is None
    assert d.violates_boundary("4を 分ける 人の 数に して みよう", "talk", "24 ÷ 4", strength=1) == "role_spec"
    assert d.violates_boundary("どれも 24こを 分ける お話だね。", "talk", "24 ÷ 4", strength=0) == "banned_classify:分ける お話"
    assert d.violates_boundary("どれも 24こを 分ける お話だね。", "talk", "24 ÷ 4", strength=1) is None
    assert d.violates_boundary("これは分ける話だね", "talk", "24 ÷ 4", strength=0) == "banned_classify:分ける話"
    assert d.violates_boundary("これは倍の話だね", "talk", "24 ÷ 4", strength=2) == "banned:倍の話"
    # v3.2 のガード：求めるものの指定も強度0・1で禁止。「くらべる ような 使い方」（9/18 模擬のすり抜け）も行き先の指定
    assert d.violates_boundary("ここの 4を、たとえば 何かと 何かを くらべる ような 使い方に できないかな。", "talk", "24 ÷ 4", strength=1) == "role_spec"
    assert d.violates_boundary("1人分が 何こかを 求める お話に して みよう。", "talk", "24 ÷ 4", strength=1) == "banned_unknown:1人分"
    assert d.violates_boundary("何人かを 求める お話に して みよう。", "talk", "24 ÷ 4", strength=1) == "role_spec"
    assert d.violates_boundary("何倍かを 求めて みよう。", "talk", "24 ÷ 4", strength=0) == "banned_unknown:何倍"
    assert d.violates_boundary("じゃあ 次は、何を 求める お話に する？", "talk", "24 ÷ 4", strength=1) is None, "宣言の問いは行き先ではない"
    assert d.violates_boundary("何人かを 求める お話に して みよう。", "talk", "24 ÷ 4", strength=2) is None
    # 問いの文（次の問題の問い）は全強度で禁止。児童自身の問いの文の引用は許す
    own = ["おにぎりが24こあります。1人に4こずつくばります。おにぎりは何人にくばれますか。"]
    q_form = "クッキー24こを、4人で 分けたら 1人分は 何こに なるか、という お話だよ。"
    assert d.violates_boundary(q_form, "talk", "24 ÷ 4", strength=2) == "question_form"
    assert d.violates_boundary(q_form, "talk", "24 ÷ 4", strength=3) == "question_form"
    assert d.violates_boundary("1人分は 何こに なりますか。", "talk", "24 ÷ 4", strength=3) == "question_form"
    quoted = "『おにぎりは何人にくばれますか』の ところが 求める ものだよ。"
    assert d.violates_boundary(quoted, "talk", "24 ÷ 4", strength=0, own_texts=own) is None, "児童の問いの文の引用は通す"
    assert d.violates_boundary(quoted, "talk", "24 ÷ 4", strength=0) == "question_form", "引用元が児童の文でなければ問いの文"
    assert d.violates_boundary("『24本は 4本の 何倍ですか』も 何倍かを 求める お話だね", "talk", "24 ÷ 4", strength=1,
                               own_texts=["24本は4本の何倍ですか"]) == "banned_unknown:何倍", "引用の外の「何倍」は強度1では禁止"
    assert d.violates_boundary("『24本は 4本の 何倍ですか』の ところだよ", "talk", "24 ÷ 4", strength=1,
                               own_texts=["24本は4本の何倍ですか"]) is None
    assert d.violates_boundary("4は 何の 数だった？", "talk", "24 ÷ 4", strength=1) is None, "役割の問い返しは問いの文ではない"
    print("OK 語彙(6章): 児童向け文言に「種類」「たずねる」「聞いていること」「等分除」「包含除」が無い。弱に構造ラベル・行き先なし。LLM 出力もガード")

    # ===== 付随バグ（フェーズF）：理由なしの不成立を not_problem に落とさない／逆向きの倍は reversed =====
    nz = lambda raw, text: ai_judge.normalize(raw, text)["issue"]
    NO_ISSUE = {"valid": False, "structure": "invalid", "unknown": None, "issue": None}
    assert nz(NO_ISSUE, "あめが24こあります。8人にくばります。") == "no_question", "場面はある・問いが無い"
    assert nz(NO_ISSUE, "あめが24こあります。8人にくばります。1人何こですか。") == "wrong_number", "場面も問いもある"
    assert nz(NO_ISSUE, "わりざん たのしい") == "not_problem" and nz(NO_ISSUE, "") == "not_problem", "場面の文が無いときだけ not_problem"
    assert nz({"valid": True, "structure": "invalid", "unknown": None, "issue": None}, "りんごが24こあります。") == "no_question", \
        "valid=true なのに structure=invalid でも not_problem に落とさない"
    assert nz({"valid": False, "structure": "invalid", "unknown": None, "issue": "reversed"}, "x") == "reversed"
    assert nz({"valid": False, "structure": "invalid", "unknown": None, "issue": "bogus"}, "24人を3人ずつ") == "no_question"
    assert d.form_message("reversed", "21 ÷ 3") == "21を 3で わる お話に しよう。いまの お話だと、わる 数と わられる 数が ぎゃくに なって いるよ。"
    assert "reversed" in ai_judge.ISSUES
    print("OK バグ修正(F): 理由なしの不成立は本文から no_question / wrong_number に寄せる。逆向きの倍は issue=reversed（専用文言）")

    # ===== 修正②（9/17）：issue コードと応答文の対応。全 issue に専用文言（alias は wrong_operation だけ） =====
    for code in ai_judge.ISSUES:
        assert code in d.FORM_MESSAGES or code in d._FORM_ALIAS, code
    assert set(d._FORM_ALIAS) == {"wrong_operation"}
    assert d.form_message("missing_condition", "24 ÷ 4") == "何こずつ、何人で、など 分ける もとに なる 数が 書いて あるかな？ もう一度 お話を 読んで みよう。"
    assert d.form_message("incomplete_text", "24 ÷ 4") == "お話が とちゅうで 終わって いるみたいだよ。さいごまで 書いて みよう。"
    assert "求める ことを きく 文が" not in d.form_message("incomplete_text", "24 ÷ 4"), "途中で切れを『問いなし』に丸めない"
    # 回帰1：除数（4まいずつ）が無い → missing_condition。判定が理由を落としても _fallback_issue が式から寄せる
    orig = "24まいのおりがみがあります。ぜんぶで何人におりがみをくばれますか。"
    assert nz(NO_ISSUE, orig) == "wrong_number", "式を渡さなければ従来どおり"
    assert ai_judge.normalize(NO_ISSUE, orig, "24 ÷ 4")["issue"] == "missing_condition"
    assert ai_judge.normalize(NO_ISSUE, "24まいを4まいずつ。何人？", "24 ÷ 4")["issue"] == "wrong_number", "除数があれば missing にしない"
    assert ai_judge.normalize({**NO_ISSUE, "issue": "missing_condition"}, orig, "24 ÷ 4")["issue"] == "missing_condition"
    assert "missing_condition" in ai_judge.build_system_prompt("24 ÷ 4")
    assert d.form_message("missing_condition", "24 ÷ 4") != d.form_message("no_question", "24 ÷ 4")
    # 回帰2：scene_contradiction は専用文（「答えが もう 書いて ある」ではない）
    m = d.form_message("scene_contradiction", "24 ÷ 4")
    assert m == "お話の 中の 数が、何の 数か たしかめて みよう。求める ことと 合って いるかな？" and "もう 書いて" not in m
    for text in d.FORM_MESSAGES.values():
        no_banned(text)
    print("OK 修正②: 全 issue → 応答文が1対1（missing_condition / incomplete_text / reversed を分離、scene_contradiction は専用文）")

    # ===== 教師画面 live =====
    live = client.get("/admin/api/live", headers=AUTH).json()
    users = {s["user_id"]: s for s in live["students"]}
    assert users["01"]["submitted"] == 9 and users["01"]["valid"] == 5, users["01"]
    assert users["01"]["structures"] == ["tobun", "hougan", "bai"]
    assert users["12"]["stuck_count"] == 0 and users["12"]["help_count"] == 0 and users["12"]["strength"] == 0 and users["12"]["declared"] is None
    assert users["03"]["submitted"] == 0 and users["03"]["online"] is True
    print("OK 教師画面: 提出数・成立数・到達・予告・反復/不一致・直近の応答・接続状態")

    # ===== 管理画面からの式の上書き =====
    assert database.get_session(sid2A)["expression"] == "24 ÷ 4", "上書き前はフェーズ2の既定"
    r = client.post(f"/admin/api/sessions/{sid2A}/expression", json={"expression": "30÷5"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["expression"] == "30 ÷ 5"
    assert client.get(f"/api/config?user_id=01&session_id={sid2A}").json()["expression"] == "30 ÷ 5"
    assert post("/api/session/resume", session_id=sid2A, user_id="01").json()["expression"] == "30 ÷ 5"
    assert client.post(f"/admin/api/sessions/{sid2A}/expression", json={"expression": "25÷4"}, headers=AUTH).status_code == 400
    assert client.post("/admin/api/sessions/99999/expression", json={"expression": "24÷4"}, headers=AUTH).status_code == 404
    r = judge(sid2A, "01", "W: 上書き後")
    assert r["message"].startswith("30と 5を つかう") and logs_of(sid2A)[-1]["expression"] == "30 ÷ 5"
    assert LAST_JUDGE["expression"] == "30 ÷ 5", "ai_judge に渡る式も上書き後"
    r = post("/api/judge", session_id=sid2A, user_id="01", message="ヒント").json()
    assert LAST_TALK["expression"] == "30 ÷ 5" and r["message"] == "[talk] llm", "ai_dialogue に渡る式も上書き後"
    print("OK 式の上書き: 管理画面から個別に変更でき、児童の画面・判定・ログに反映")

    # ===== ログアウト → 終了時刻、再入場で消える =====
    assert post("/api/session/end", session_id=sid2A, user_id="01").status_code == 200
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sid2A)[0][0] is not None
    assert post("/api/session/end", session_id=sid2A, user_id="02").status_code == 403
    post("/api/login", user_id="01")
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sid2A)[0][0] is None
    print("OK 終了時刻: ログアウトで記録、入り直すと消える")

    # (9) Phase1 / Phase3 で予告支援が一切出ない（判定だけ記録、カウンタも動かない）
    for ph in (1, 3):
        admin_post("/admin/api/phase", phase=ph)
        pl = post("/api/login", user_id="13").json()
        sidP = pl["session_id"]
        assert pl["expression"] == {1: "21 ÷ 3", 3: "30 ÷ 5"}[ph] and pl["dialog"] is None and pl["declared"] is None
        for msg in ("T: a", "T: b", "T: c", "T: d", "T: e"):
            r = judge(sidP, "13", msg)
            assert r["response_type"] is None and r["prompt_strength"] is None and r["message"] == "おくったよ"
            assert r["declared"] is None and r["stuck_count"] is None and r["dialog"] is None
            assert r["strength"] is None and r["help_count"] is None and r["strength_trigger"] is None
        assert post("/api/declare", session_id=sidP, user_id="13", text="いくつ分").status_code == 400
        assert post("/api/judge", session_id=sidP, user_id="13", message="いくつ分", declaring=True).json()["input_type"] == "taiwa", \
            "フェーズ1・3では declaring を無視して対話として記録"
        assert judge_queue.wait_idle(10)
        lp = logs_of(sidP)
        assert all(l["response_type"] is None and l["prompt_strength"] is None and l["ai_message"] is None for l in lp)
        assert [l["stuck_count"] for l in lp] == [0] * 6 and [l["is_new"] for l in lp[:5]] == [True, False, False, False, False]
        assert lp[-1]["input_type"] == "taiwa"
        assert database.get_session(sidP)["stuck_count"] == 0
    print("OK 状態機械(9): フェーズ1・3では予告支援なし・カウンタも動かない")

    # ===== フェーズ3：別式・新セッション =====
    assert client.get("/api/config").json()["phase"] == 3
    a4 = post("/api/session/new", user_id="01").json()
    assert a4["session_id"] != sidA and a4["phase"] == 3 and a4["expression"] == "30 ÷ 5"
    assert post("/api/session/new", user_id="02").json()["expression"] == "21 ÷ 3"
    r = judge(a4["session_id"], "01", "T: 30このあめを5人で")
    assert r["message"] == "おくったよ" and r["history"] == []
    assert logs_of(a4["session_id"])[0]["phase"] == 3 and logs_of(a4["session_id"])[0]["expression"] == "30 ÷ 5"
    admin_post("/admin/api/phase", phase=2)
    assert post("/api/session/new", user_id="01").json()["session_id"] == sid2A
    admin_post("/admin/api/phase", phase=3)
    r = judge(sid2A, "01", "T: おそく届いた")
    assert r["phase"] == 2 and r["show_support"] is True
    assert raw("SELECT COUNT(*) FROM chat_logs cl JOIN sessions s ON s.session_id=cl.session_id WHERE cl.phase != s.phase")[0][0] == 0
    print("OK フェーズ3: 01→30÷5 / 02→21÷3。chat_logs.phase は常に sessions.phase と一致")

    # ===== CSV =====
    r = client.get("/admin/api/export/csv", headers=AUTH)
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert list(rows[0].keys()) == database.CSV_FIELDS
    for col in ("declared_structure", "declared_by", "declaration_met", "self_label", "self_label_text",
                "self_label_match", "role_answer", "role_corrected", "is_help_request",
                "prompt_strength", "produced_structures", "stuck_count", "miss_count", "help_count",
                "strength", "strength_trigger", "target_structure", "latency_ms"):
        assert col in rows[0], col
    decl = [x for x in rows if x["input_type"] == "declaration"]
    assert decl and decl[0]["declared_structure"] == "hougan" and decl[0]["declared_by"] == "child"
    assert decl[0]["ai_message"] == "じゃあ、その お話を 作って みよう。" and any(x["declared_structure"] == "" for x in decl)
    assert not any(x["input_type"] in ("role", "self_label") for x in rows), "役割の宣言・3択の行は無い"
    assert all(x["role_answer"] == "" for x in rows)
    assert any(x["divisor_phrase"] == "4人で 分けます" for x in rows if x["input_type"] == "sakumon")
    assert any(x["strength_trigger"] == "talk" for x in rows)
    assert all(x["latency_ms"] != "" for x in rows if x["input_type"] != "resend")
    assert any(x["declaration_met"] == "1" for x in rows) and any(x["prompt_strength"] == "3" for x in rows)
    assert any(x["strength_trigger"] == "help" for x in rows) and any(x["strength_trigger"] == "miss" for x in rows)
    assert any(x["target_structure"] == "hougan" and x["strength"] == "3" for x in rows)
    assert all(x["created_at"][:4] == str(now_jst.year) for x in rows)
    assert "judge_status" in rows[0] and "judged_at" in rows[0]
    assert all(x["judge_status"] == "done" and x["judged_at"] != "" for x in rows if x["input_type"] == "sakumon" and x["issue"] != "error"), \
        "作問行はフェーズを問わず judge_status=done（同期判定も保存時に done）"
    assert all(x["judge_status"] == "" for x in rows if x["input_type"] in ("taiwa", "resend", "role", "declaration"))
    print("OK CSV: 新列がすべて出る（declaration / role 行・latency_ms・judge_status）・JST")

    # ===== データの片づけ：退避して全部消す =====
    assert client.post("/admin/api/reset", json={"note": "x", "confirm": "けす"}, headers=AUTH).status_code == 400, "確認語が要る"
    assert client.post("/admin/api/reset", json={"note": "x", "confirm": "消す"}).status_code == 401
    before = {t: raw(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("sessions", "chat_logs", "phase_changes")}
    assert before["sessions"] > 0 and before["chat_logs"] > 0
    cfg_before = raw("SELECT current_phase FROM app_config")[0][0]
    r = admin_post("/admin/api/reset", note="pre 0918!", confirm="消す")
    assert r["deleted"]["sessions"] == before["sessions"] and r["deleted"]["chat_logs"] == before["chat_logs"]
    assert r["backup"].endswith("_pre_0918") and Path(r["backup"]).exists() and Path(r["backup"]).parent == Path(os.environ["DATABASE_PATH"]).parent
    bcon = sqlite3.connect(r["backup"])
    assert bcon.execute("SELECT COUNT(*) FROM chat_logs").fetchone()[0] == before["chat_logs"], "バックアップに全ログが残る"
    bcon.close()
    for t in ("sessions", "chat_logs", "selection_events", "teacher_calls", "phase_changes"):
        assert raw(f"SELECT COUNT(*) FROM {t}")[0][0] == 0, t
    assert raw("SELECT current_phase FROM app_config")[0][0] == cfg_before, "app_config は残す"
    assert client.get("/admin/api/live", headers=AUTH).json()["students"] == []
    assert post("/api/login", user_id="01").json()["session_id"] is not None, "消した後も新しく始められる"
    print("OK データの片づけ: 確認語「消す」・DB を data/ に退避（全ログ入り）・5テーブルを空に・app_config は残す")

    # ===== v3.2：「求めるものって何？」の定型（LLM を呼ばない・help に数えない）／help の連続昇格の抑止／talk 捨て＋前置き =====
    admin_post("/admin/api/phase", phase=2)
    sidM = post("/api/login", user_id="24").json()["session_id"]
    r = judge(sidM, "24", "H: おにぎりが24こあります。【1人に 4こずつ】くばります。おにぎりは何人にくばれますか。@おにぎり/こ")
    assert r["message"] == ("新しい 問題が できたね！ この お話は、『おにぎりは何人にくばれますか』を 求める お話だね。\n"
                            "今度は、**求める ものが ちがう** お話は 作れるかな？"), r["message"]
    cb = dict(CALLS)
    r = judge(sidM, "24", "求めるものがちがうってどういうことですか？")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk" and CALLS["llm"] == cb["llm"], "定型（LLM を呼ばない）"
    assert r["message"] == ("1ばんの お話の 最後の 文、『おにぎりは何人にくばれますか』の ところが『求める もの』だよ。\n"
                            "今度は、ここが ちがう お話を 作って みよう。"), r["message"]
    assert r["is_help_request"] is False and r["strength"] == 0 and r["strength_trigger"] == "none" and r["help_count"] == 0
    assert logs_of(sidM)[-1]["awaiting"] is None
    r = judge(sidM, "24", "H: ビー玉が24こひつようです。1人4こずつ買います。何人でビー玉をかいにいけばいいですか。@ビー玉/こ")
    assert r["prompt_strength"] == 0 and r["stuck_count"] == 1
    r = judge(sidM, "24", "H: クッキーが24個あります。1人に4つくばると何人にくばることができますか。@クッキー/こ")
    assert r["prompt_strength"] == 1 and r["strength_trigger"] == "stuck"
    assert r["message"] == ("2ばんの『1人4こずつ買います』も 3ばんの『1人に4つくばると何人にくばることができますか』も、4の 使い方は 同じで、"
                            "どちらも **何人か**を 求める お話だね。\nじゃあ 次は、何を 求める お話に する？"), r["message"]
    r = post("/api/judge", session_id=sidM, user_id="24", message="わかりません", declaring=True).json()
    assert r["input_type"] == "declaration" and r["message"] == "わからなくても だいじょうぶ。じゃあ、求める ものが ちがう お話を 作って みよう。"
    # 「わかんない」（help）→ 1→2：talk の文言は捨てて前置き＋中（目標 tobun、題材は最新＝クッキー）
    r = judge(sidM, "24", "わからない")
    assert r["is_help_request"] is True and r["strength"] == 2 and r["strength_trigger"] == "help" and r["declared"] == "tobun"
    assert r["message"] == ("そっか。じゃあ、こうして みよう。\n1人分が 何こかを 求める お話に して みよう。4を、分ける 人の 数に するよ。"
                            "クッキーの お話は そのままで いいよ。"), r["message"]
    assert r["target_label"] == "1人分が 何こかを 求める お話"
    # 直後の「どういうことですか？」（help 判定）→ 作問も宣言も無いので help では上げない（2 のまま）。help_count は数える
    r = judge(sidM, "24", "どうすればいいの")
    assert r["is_help_request"] is True and r["strength"] == 2 and r["strength_trigger"] == "none", "help の連続昇格を抑止"
    assert r["help_count"] == 2 and r["message"] == "[talk] llm" and r["declared"] == "tobun"
    assert LAST_TALK["context"]["strength"] == 2 and LAST_TALK["context"]["problems"][-1]["question_phrase"] == "1人に4つくばると何人にくばることができますか"
    # 作問（不成立でも「試した」）のあとの help は上がる
    r = judge(sidM, "24", "X: クッキーが24個あります。4人で分けます。")
    assert r["response_type"] == "form" and r["strength"] == 2
    r = judge(sidM, "24", "ヒント")
    assert r["strength"] == 3 and r["strength_trigger"] == "help" and r["help_count"] == 3
    assert r["message"] == ("そっか。じゃあ、こうして みよう。\n「クッキーが 24こ あります。4人で 同じ 数ずつ 分けます。」\n"
                            "つづきの、求める 文を 書いて みよう。"), r["message"]
    # 強度2以上では「求めるものって何」も LLM（行き先を言ってよい）
    cb = dict(CALLS)
    r = judge(sidM, "24", "求めるものってなに")
    assert CALLS["llm"] == cb["llm"] + 1 and r["message"] == "[talk] llm"
    # 目標どおり（tobun）→ 全部 0・称賛。完了後の対話は支援なし
    r = judge(sidM, "24", "T: クッキーが24個あります。4人で同じ数ずつ分けます。1人分は何個ですか。@クッキー/こ")
    assert r["is_new"] is True and r["strength"] == 0 and r["declaration_met"] is True
    assert r["message"].startswith("新しい 問題が できたね！ この お話は、『1人分は何個ですか』を 求める お話だね。")
    r = judge(sidM, "24", "B: クッキー24こはクッキー4この何倍ですか。@クッキー/こ")
    assert r["response_type"] == "done" and r["message"] == (
        "3つ とも できたね！ 1ばんは『何人か』、4ばんは『何こか』、5ばんは『何倍か』。\n"
        "同じ 24÷4 なのに、求める ものが ぜんぶ ちがう お話に なったね。\n"
        "時間まで、もっと 作って みよう。求める ものが 同じでも、ちがう お話なら いいよ。"), r["message"]
    assert database.help_raised_since_last_sakumon(sidM) is False
    print("OK v3.2: 「求めるものって何」は定型で問いの文を指す。help は作問・宣言まで連続で上げない。中・強の連結は talk を捨てて前置き。完了文は児童の疑問語")

    # ===== 旧スキーマの退避（Render の永続ディスク上の DB を想定） =====
    legacy = Path(_tmp) / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript("""
        CREATE TABLE sessions (session_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            expression TEXT NOT NULL, created_at TIMESTAMP, phase INTEGER NOT NULL DEFAULT 1, run_id INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE chat_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            message TEXT NOT NULL, response_json TEXT NOT NULL, structure TEXT, is_new INTEGER NOT NULL DEFAULT 0,
            input_type TEXT, stumble TEXT, created_at TIMESTAMP, support_level TEXT, learner_state TEXT);
        CREATE TABLE app_config (id INTEGER PRIMARY KEY CHECK (id = 1), current_phase INTEGER NOT NULL DEFAULT 1,
            expression_a TEXT NOT NULL DEFAULT '24 ÷ 4', expression_b TEXT NOT NULL DEFAULT '30 ÷ 5',
            run_id INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP);
        CREATE TABLE phase_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, phase INTEGER NOT NULL,
            expression_a TEXT, expression_b TEXT, note TEXT, changed_at TIMESTAMP);
        INSERT INTO sessions (user_id, expression, phase, run_id) VALUES ('07', '24 ÷ 4', 1, 3);
        INSERT INTO chat_logs (session_id, user_id, message, response_json) VALUES (1, '07', '旧データ', '{}');
    """)
    con.commit(); con.close()
    orig = database.DB_PATH
    database.DB_PATH = legacy
    try:
        database.init_db()
        names = {r[0] for r in sqlite3.connect(legacy).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert any(n.startswith("chat_logs_legacy_") for n in names) and any(n.startswith("sessions_legacy_") for n in names), names
        lcon = sqlite3.connect(legacy)
        legacy_logs = [n for n in names if n.startswith("chat_logs_legacy_")][0]
        assert lcon.execute(f"SELECT message FROM {legacy_logs}").fetchone()[0] == "旧データ", "旧データは消えない"
        assert "declared" in [r[1] for r in lcon.execute("PRAGMA table_info(sessions)")]
        database.init_db()
        names2 = {r[0] for r in sqlite3.connect(legacy).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names2 == names
    finally:
        database.DB_PATH = orig
    print("OK 旧スキーマ: 起動時に *_legacy_日付 へ改名して退避（DROP しない）、新スキーマで作り直す")

print("\nALL PASSED")
