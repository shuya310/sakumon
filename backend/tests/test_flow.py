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

import main  # noqa: E402
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


def fake_judge(message, expression, user_id=None):
    """モック：先頭の記号で判定。本文末尾の「@物/助数詞」で item / unit を返す（無ければ None）。"""
    CALLS["judge"] += 1
    jr = dict(_JUDGE[message[0]], item=None, unit=None)
    if "@" in message:
        item, _, unit = message.rsplit("@", 1)[1].partition("/")
        jr["item"], jr["unit"] = item or None, unit or None
    return jr


def fake_classify(message, recent=None, expression=None, user_id=None):
    CALLS["classify"] += 1
    return "sakumon" if len(message) > 1 and message[1] == ":" and message[0] in _JUDGE else "taiwa"


def fake_llm(child_message, input_kind, judge_result, history, recent_turns, response_type, expression,
             user_id=None):
    CALLS["llm"] += 1
    return {"message": f"[{response_type}] llm", "state": response_type}


def fake_declaration(text, user_id=None):
    CALLS["declare"] += 1
    for key, kind in (("1つ分", "tobun"), ("いくつ分", "hougan"), ("何倍", "bai")):
        if key in text:
            return kind
    return "unknown"


PHRASE = {"on": True}


def fake_extract_divisor_phrase(problem_text, divisor, user_id=None):
    """モック：本文の【…】を除数の句とみなす。PHRASE["on"] が False なら LLM 抽出失敗＝実装の予備（除数を含む文）に落ちる。"""
    CALLS["phrase"] = CALLS.get("phrase", 0) + 1
    if PHRASE["on"] and "【" in problem_text:
        return d.format_quote(problem_text.split("【", 1)[1].split("】", 1)[0])
    return d._sentence_with_number(problem_text, divisor)


def fake_role(text, divisor, user_id=None):
    CALLS["role"] = CALLS.get("role", 0) + 1
    for key, kind in (("わからない", "dont_know"), ("人数", "people"), ("1人分", "per_one"), ("くらべ", "base")):
        if key in text:
            return kind
    return "unknown"


ai_classify.classify_declaration = fake_declaration
main.ai_classify.classify_declaration = fake_declaration
ai_classify.classify_role = fake_role
main.ai_classify.classify_role = fake_role
d.extract_divisor_phrase = fake_extract_divisor_phrase
ai_judge.judge = fake_judge
main.ai_judge.judge = fake_judge
ai_classify.classify = fake_classify
main.ai_classify.classify = fake_classify
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
    assert {"sessions", "chat_logs", "app_config", "phase_changes"} <= tables
    cols = [r[1] for r in raw("PRAGMA table_info(chat_logs)")]
    assert cols == [
        "log_id", "session_id", "user_id", "phase", "expression", "created_at",
        "input_type", "message", "ai_message",
        "valid", "structure", "unknown", "issue", "is_new", "item", "unit",
        "response_type", "prompt_strength",
        "declared_structure", "declared_by", "declaration_met",
        "self_label", "self_label_text", "self_label_match",
        "role_answer", "role_corrected",
        "produced_structures", "stuck_count", "miss_count", "latency_ms",
    ], cols
    scols = [r[1] for r in raw("PRAGMA table_info(sessions)")]
    assert scols == ["session_id", "user_id", "phase", "expression", "parity_group", "session_start", "session_end",
                     "declared", "declared_by", "stuck_count", "miss_count", "help_count", "strength"], scols
    idx = {r[1]: r[2] for r in raw("PRAGMA index_list(sessions)")}
    assert idx.get("idx_sessions_user_phase") == 1, "UNIQUE(user_id, phase)"
    print("OK スキーマ: chat_logs 30列・sessions 13列（状態機械6列）・UNIQUE(user_id, phase)")

    # ===== 強度の遷移規則（decide_strength は状態遷移。カウンタから毎回計算し直さない） =====
    ds = main.decide_strength
    # 0→1 は stuck が 2 に達したときだけ
    assert ds(0, stuck_up=True, stuck_after=1) == (0, "none"), "同構造1回目では介入しない"
    assert ds(0, stuck_up=True, stuck_after=2) == (1, "stuck")
    assert ds(0, miss_up=True) == (0, "none"), "強度0では miss だけでは上がらない"
    assert ds(0, help_up=True) == (0, "none"), "強度0では help だけでは上がらない"
    assert ds(0, stuck_up=True, stuck_after=2, miss_up=True) == (1, "stuck"), "0→1 は必ず1段"
    # 強度1以降は stuck / miss / help のどれが増えても +1（上限3）
    assert ds(1, stuck_up=True, stuck_after=3) == (2, "stuck")
    assert ds(1, miss_up=True) == (2, "miss")
    assert ds(1, help_up=True) == (2, "help")
    assert ds(2, help_up=True) == (3, "help")
    assert ds(3, stuck_up=True, stuck_after=9) == (3, "stuck"), "上限3"
    assert ds(3, miss_up=True) == (3, "miss")
    # 同じターンで stuck と miss が両方増えても +1 は1回。trigger は miss を優先
    assert ds(1, stuck_up=True, stuck_after=3, miss_up=True) == (2, "miss")
    # 何も増えなければ据え置き
    assert ds(2) == (2, "none") and ds(1) == (1, "none")
    # 新構造到達で 0
    assert ds(3, is_new=True) == (0, "none") and ds(1, is_new=True, miss_up=True) == (0, "none")
    print("OK 強度の遷移: 0→1 は stuck=2 のみ、1以降はどのカウンタが増えても +1（上限3）、新構造で 0")

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
    assert cfg["expression_assignment"] == {"odd": {"1": "24 ÷ 6", "2": "24 ÷ 8", "3": "24 ÷ 3"},
                                            "even": {"1": "24 ÷ 3", "2": "24 ÷ 8", "3": "24 ÷ 6"}}
    a = post("/api/login", user_id="01").json()
    b = post("/api/login", user_id="02").json()
    assert a["expression"] == "24 ÷ 6" and b["expression"] == "24 ÷ 3"
    assert raw("SELECT expression FROM sessions WHERE session_id=?", a["session_id"])[0][0] == "24 ÷ 6"
    q = client.get(f"/api/config?user_id=01&session_id={a['session_id']}").json()
    assert q["expression"] == "24 ÷ 6"
    assert client.get(f"/api/config?user_id=02&session_id={a['session_id']}").json()["expression"] is None, "他人のセッションの式は返さない"
    print("OK 式: 01→24÷6 / 02→24÷3（フェーズ1）。/api/config は本人のセッションの式だけ返す")

    # ===== フェーズ1：ログイン＝探して無ければ作る（1児童1フェーズ1セッション） =====
    a2 = post("/api/login", user_id="01").json()
    assert a["session_id"] == a2["session_id"], "同じフェーズで再ログインしても同一セッション"
    assert a["phase"] == 1 and a["show_support"] is False
    c = post("/api/login", user_id="03").json()
    assert len({a["session_id"], b["session_id"], c["session_id"]}) == 3
    sidA, sidB, sidC = a["session_id"], b["session_id"], c["session_id"]
    try:
        raw("INSERT INTO sessions (user_id, phase, expression, parity_group, session_start) VALUES ('01', 1, '24 ÷ 6', 'odd', '2026-09-18 10:00:00')")
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

    # フェーズ1の提出：判定は動くが表示は「おくったよ」。response_type / ai_message は記録しない
    r1 = judge(sidA, "01", "T: おりがみ24まいを6人で")
    assert r1["message"] == "おくったよ" and r1["response_type"] is None and r1["dialog"] is None
    assert r1["valid"] is None and r1["structure"] is None and r1["history"] == [] and r1["accepted"] is False
    judge(sidA, "01", "T: クッキー24こを6人で")
    judge(sidA, "01", "X: 場面矛盾")
    rt = judge(sidA, "01", "わからない")
    assert rt["message"] == "おくったよ"
    logs = logs_of(sidA)
    assert [l["response_type"] for l in logs] == [None] * 4
    assert [l["ai_message"] for l in logs] == [None] * 4
    assert [l["prompt_strength"] for l in logs] == [None] * 4
    assert [l["phase"] for l in logs] == [1, 1, 1, 1]
    assert logs[0]["structure"] == "tobun" and logs[0]["is_new"] is True and logs[0]["produced_structures"] == "tobun"
    assert logs[1]["is_new"] is False and logs[2]["issue"] == "scene_contradiction"
    assert [l["stuck_count"] for l in logs] == [0] * 4 and [l["miss_count"] for l in logs] == [0] * 4
    assert all(l["expression"] == "24 ÷ 6" for l in logs)
    assert all(l["latency_ms"] is not None for l in logs)
    print("OK フェーズ1: 表示は『おくったよ』のみ。判定・produced は記録、カウンタは動かない、response_type は空")

    # ===== フェーズ2へ切替（別セッション：フェーズ1は引き継がない） =====
    admin_post("/admin/api/phase", phase=2)
    assert client.get("/api/config").json()["phase"] == 2
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sidA)[0][0] is not None
    a3 = post("/api/session/new", user_id="01").json()
    assert a3["session_id"] != sidA and a3["expression"] == "24 ÷ 8"
    assert post("/api/session/new", user_id="01").json()["session_id"] == a3["session_id"]
    assert post("/api/session/new", user_id="02").json()["expression"] == "24 ÷ 8"
    sid2A = a3["session_id"]
    assert a3["show_support"] is True and a3["declared"] is None and a3["dialog"] is None
    assert a3["history"] == [] and a3["problems"] == [] and a3["conversation"] == []
    assert "choices" not in a3, "3択は廃止"
    post("/api/session/new", user_id="03")
    print("OK フェーズ2: 新セッション（式 24÷8）で開始し、フェーズ1の産出・到達構造・会話を引き継がない")

    # ===== 3章の文言（praise / form / done / talk） =====
    r = judge(sid2A, "01", "T: おりがみ24まいを8人で")
    assert r["response_type"] == "praise" and r["is_new"] is True and r["prompt_strength"] == 0
    assert r["message"] == "新しい 問題が できたね！\nほかにも、**求めるものが ちがう** 問題は 作れるかな？"
    assert r["accepted"] is True and r["history"] == ["tobun"]
    llm_before = CALLS["llm"]
    r = judge(sid2A, "01", "T: ジュース24Lを8人で")
    assert r["response_type"] == "praise" and r["is_new"] is False and r["prompt_strength"] == 0
    assert r["message"] == "いいね、また 一つ できたね。\n今度は **求めるものが ちがう** 問題も 作れそうかな？"
    assert CALLS["llm"] == llm_before, "praise は定型（LLM を呼ばない）"
    r = judge(sid2A, "01", "X: だめ")
    assert r["response_type"] == "form" and r["valid"] is False
    assert r["message"] == "求める ことの 答えが、お話の 中に もう 書いて あるよ。どこか さがして みよう。"
    assert CALLS["llm"] == llm_before, "form も定型"
    r = judge(sid2A, "01", "N: 問いなし")
    assert r["message"] == "求める ことを きく 文が ないみたいだよ。さいごに「〜は いくつですか」を 書いて みよう。"
    r = judge(sid2A, "01", "W: 数ちがい")
    assert r["message"] == "24と 8を つかう 問題に しよう。いまの お話だと しきが ちがって しまうよ。"
    r = judge(sid2A, "01", "P: 文章題でない")
    assert r["message"] == "まだ お話に なって いないみたいだよ。「〜が あります」から はじめて みよう。"
    r = judge(sid2A, "01", "むずかしい")
    assert r["response_type"] == "talk" and r["message"] == "[talk] llm" and r["stuck_count"] == 1
    r = judge(sid2A, "01", "H: あめ24こを8こずつ")
    assert r["response_type"] == "praise" and r["is_new"] is True and sorted(r["history"]) == ["hougan", "tobun"]
    r = judge(sid2A, "01", "B: 24本は8本の何倍")
    assert r["response_type"] == "done" and r["all_reached"] is True
    assert r["message"] == "3つ とも できたね！\n1つ分の 大きさ、いくつ分、何倍——ぜんぶ ちがう ものを 求める 問題が そろったよ。"
    r = judge(sid2A, "01", "B: 24人は8人の何倍")
    assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["declared"] is None
    p2 = logs_of(sid2A)
    assert [l["response_type"] for l in p2] == ["praise", "praise", "form", "form", "form", "form", "talk", "praise", "done", "done"]
    assert [l["stuck_count"] for l in p2] == [0, 1, 1, 1, 1, 1, 1, 0, 0, 1]
    assert [l["miss_count"] for l in p2] == [0] * 10
    assert all(l["ai_message"] for l in p2), "フェーズ2は全ターン ai_message を記録"
    for l in p2:
        no_banned(l["ai_message"])
    resumed = post("/api/session/resume", session_id=sid2A, user_id="01").json()
    assert len(resumed["conversation"]) == 10 and resumed["conversation"][0]["prompt_strength"] == 0
    assert [p["structure"] for p in resumed["problems"]] == ["tobun", "tobun", "hougan", "bai", "bai"]
    print("OK 文言: praise(新/反復)・form(4種)・done は仕様の表どおり。不成立・対話でカウンタ不動、新構造で0")

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
    assert r["accepted"] is False and r["message"] == d.PRAISE_REPEAT and r["stuck_count"] == 1
    row = logs_of(sidB2)[-1]
    assert row["input_type"] == "resend" and row["latency_ms"] is None and row["stuck_count"] == 1
    resumed = post("/api/session/resume", session_id=sidB2, user_id="02").json()
    assert sum(1 for p in resumed["problems"] if p["text"] == "T: べつの") == 1, "再送で一覧に重複しない"
    print("OK 判定エラー: 3-2 の文言・一覧に載せない・送り直しは判定し直す。判定済み本文の再送は API を呼ばずカウンタ不動")

    # 待ち状態の問い合わせ（送信中でなければ null）
    assert client.get("/api/judge/status?user_id=02").json() == {"state": None}
    assert client.get("/api/judge/status?user_id=abc").status_code == 400

    # ===== is_new はフェーズスコープ =====
    database.save_log(session_id=99999, user_id="02", phase=2, expression="24 ÷ 8", input_type="sakumon",
                      message="別セッションの倍", ai_message=None, valid=True, structure="bai", unknown="ratio",
                      is_new=True, produced_structures=["tobun", "bai"], stuck_count=0, miss_count=0)
    assert database.get_produced("02", 2) == ["tobun", "bai"]
    r = judge(sidB2, "02", "B: 24本は8本の何倍")
    assert r["is_new"] is False, "別セッションで既出の構造は is_new=0"
    assert database.get_produced("02", 1) == []
    raw("DELETE FROM chat_logs WHERE session_id=99999")
    print("OK is_new: フェーズスコープ（別セッションでも既出なら 0、別フェーズは数えない）")

    # ===== 状態機械（仕様 v2 7章の9項目） =====
    sidS = post("/api/login", user_id="11").json()["session_id"]

    # (1) 不成立を3回連続 → stuck_count は 0 のまま（form のみ）
    for i in range(3):
        r = judge(sidS, "11", f"X: 不成立{i}")
        assert r["response_type"] == "form" and r["stuck_count"] == 0 and r["miss_count"] == 0
    assert [l["stuck_count"] for l in logs_of(sidS)] == [0, 0, 0]
    print("OK 状態機械(1): 不成立3連続で stuck_count は 0 のまま")

    # (2) 等分除→等分除→等分除 → stuck 0→1→2、3問目で弱
    r = judge(sidS, "11", "T: おりがみ24まいを8人で")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 0 and r["is_new"] is True
    r = judge(sidS, "11", "T: あめ24こを8人で")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 1
    assert r["strength"] == 0 and r["strength_trigger"] == "none", "同構造1回目では強度は 0 のまま"
    r = judge(sidS, "11", "T: みかんが24こあります。【8人で 分けます】。1人分は何こになりますか。")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 1 and r["stuck_count"] == 2, r
    assert r["strength"] == 1 and r["strength_trigger"] == "stuck" and r["help_count"] == 0
    assert database.get_session(sidS)["strength"] == 1, "強度は sessions に状態として保持"
    assert r["message"] == "3ばんの お話で、8は 何を あらわして いるかな？", r["message"]
    for label in d.STRUCTURE_LABEL.values():
        assert label not in r["message"], ("弱で構造のラベルを出さない", label)
    assert r["dialog"] == "role" and r["declared"] is None, "弱：ターン1（役割）の答え待ち。まだ予告は立たない"
    resumed = post("/api/session/resume", session_id=sidS, user_id="11").json()
    assert resumed["dialog"] == "role", "再入場でもターン1を復元"
    print("OK 状態機械(2): 同構造 0→1→2、3問目で弱（ターン1『3ばんの お話で、8は 何を あらわして いるかな？』）")

    # (3a) ターン1：判定（tobun＝8は人数）と食い違う答え「1人分の数」→ 1回だけ訂正（児童の文を引用・役割名は言わない）
    rb, pb = CALLS.get("role", 0), CALLS.get("phrase", 0)
    r = post("/api/judge", session_id=sidS, user_id="11", message="1人分の数", declaring=True).json()
    assert r["input_type"] == "role" and r["role_answer"] == "per_one" and r["role_corrected"] is True, r
    assert CALLS["role"] == rb + 1 and CALLS["phrase"] == pb + 1
    assert r["message"] == ("本当に そうかな？ お話では『8人で 分けます』と 書いて あるよ。\n"
                            "8は 何の 数に なって いるかな？"), r["message"]
    for w in ("人数", "1つ分", "いくつ分", "何倍"):
        assert w not in r["message"], ("訂正で役割名・構造ラベルを言わない", w)
    assert r["dialog"] == "role" and r["declared"] is None and r["prompt_strength"] == 1
    row = logs_of(sidS)[-1]
    assert row["input_type"] == "role" and row["role_answer"] == "per_one" and row["role_corrected"] is True
    assert row["message"] == "1人分の数" and row["response_type"] == "prompt" and row["stuck_count"] == 2
    assert post("/api/session/resume", session_id=sidS, user_id="11").json()["dialog"] == "role", "訂正後もターン1を復元"
    # 2回目の答え：また食い違っても問い返さず（ループさせない）ターン2へ
    r = post("/api/judge", session_id=sidS, user_id="11", message="やっぱり1人分の数", declaring=True).json()
    assert r["input_type"] == "role" and r["role_answer"] == "per_one" and r["role_corrected"] is False, r
    assert r["message"] == "じゃあ 次は、8を 何の 数に して みたい？" and r["dialog"] == "declaration"
    assert logs_of(sidS)[-1]["role_corrected"] is False
    st = database.get_session(sidS)
    assert st["stuck_count"] == 2 and st["miss_count"] == 0 and st["strength"] == 1, "役割の答えではカウンタ・強度を動かさない"
    assert post("/api/session/resume", session_id=sidS, user_id="11").json()["dialog"] == "declaration", "ターン2を復元"
    print("OK 役割の宣言(ターン1): 判定と食い違う答えは1回だけ訂正（児童の文『8人で 分けます』を引用）。2回目は正誤にかかわらずターン2へ")

    # (3) ターン2で「いくつ分をきく」と入力 → declared=hougan, declared_by=child、目標が固定表示される
    r = post("/api/judge", session_id=sidS, user_id="11", message="いくつ分をきく", declaring=True).json()
    assert r["input_type"] == "declaration" and r["message"] is None and r["dialog"] is None, r
    assert r["declared"] == "hougan" and r["declared_by"] == "child" and r["target_label"] == "いくつ分", r
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "child"
    assert st["stuck_count"] == 2 and st["miss_count"] == 0, "予告ではカウンタを動かさない"
    dd = logs_of(sidS)[-1]
    assert dd["input_type"] == "declaration" and dd["declared_structure"] == "hougan" and dd["declared_by"] == "child"
    assert dd["message"] == "いくつ分をきく" and dd["latency_ms"] is not None
    resumed = post("/api/session/resume", session_id=sidS, user_id="11").json()
    assert resumed["declared"] == "hougan" and resumed["target_label"] == "いくつ分", "目標の固定表示"
    # 予告が済んだあとに declaring で届いた作問以外の文は対話（待っているターンが無い）
    r = post("/api/judge", session_id=sidS, user_id="11", message="りんごの問題", declaring=True).json()
    assert r["input_type"] == "taiwa" and r["declared"] == "hougan", "待っているターンが無ければ対話。予告は消さない"
    # 対話モードでも作問を書けば通常の作問処理（予告として扱わない）
    r = post("/api/judge", session_id=sidS, user_id="11", message="X: 予告モードで作問", declaring=True).json()
    assert r["input_type"] == "sakumon" and r["response_type"] == "form" and r["declared"] == "hougan"
    # declaring なし（通常）の対話は talk のまま。/api/declare も直接使える
    r = judge(sidS, "11", "むずかしい")
    assert r["input_type"] == "taiwa" and r["response_type"] == "talk"
    r = post("/api/declare", session_id=sidS, user_id="11", text="1つ分がいくつか").json()
    assert r["declared"] == "tobun" and r["declared_by"] == "child"
    r = post("/api/declare", session_id=sidS, user_id="11", text="いくつ分をきく").json()
    assert r["declared"] == "hougan"
    assert post("/api/declare", session_id=sidA, user_id="01", text="いくつ分").status_code == 400
    print("OK 状態機械(3): ターン2で「いくつ分をきく」→ declared=hougan / child。作問なら通常処理、unknown は立てない")

    # (4) その次に等分除 → declaration_met=0, miss=1 → 中（目標の指定。3択は無い）
    r = judge(sidS, "11", "T: えんぴつが24本あります。8人で分けます。1人分は何本ですか。@えんぴつ/本")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 2, r
    assert r["declaration_met"] is False and r["miss_count"] == 1 and r["stuck_count"] == 3
    assert r["strength"] == 2 and r["strength_trigger"] == "miss", "stuck と miss が同時に増えても +1 は1回。trigger は miss"
    assert r["message"] == "えんぴつの お話は そのままで いいよ。8を「1人分の 数」に して みよう。", r["message"]
    row = logs_of(sidS)[-1]
    assert row["item"] == "えんぴつ" and row["unit"] == "本", "判定が読み取った物・助数詞をログに残す"
    assert r["dialog"] is None and "choices" not in r, "中：3択は出さない。入力欄は作問モードのまま"
    row = logs_of(sidS)[-1]
    assert row["declared_structure"] == "hougan" and row["declared_by"] == "child" and row["declaration_met"] is False
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "system", "中：prompt 発行と同時にシステムが目標を立てる"
    assert r["declared"] == "hougan" and r["declared_by"] == "system" and r["target_label"] == "いくつ分"
    assert post("/api/session/resume", session_id=sidS, user_id="11").json()["dialog"] is None
    assert client.post("/api/self_label", json={"session_id": sidS, "user_id": "11", "choice": "bai"}).status_code == 404, "/api/self_label は廃止"
    print("OK 状態機械(4): 予告と違う構造 → declaration_met=0, miss=1 → 中（目標の指定。3択・/api/self_label は廃止）")

    # (5) 中の直後にまた予告と違う構造 → miss=2 → 強（場面固定の文言・ref_no は最新の表示番号）
    r = judge(sidS, "11", "T: ジュース24Lを8人で@ジュース/L")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 3, r
    assert r["declaration_met"] is False and r["miss_count"] == 2 and r["stuck_count"] == 4
    assert r["strength"] == 3 and r["strength_trigger"] == "miss"
    assert r["message"] == "「ジュースが 24L あります。1人に 8Lずつ 分けます。」 この あとに、求める 文を 書いて みよう。", r["message"]
    assert r["dialog"] is None and r["declared"] == "hougan" and r["declared_by"] == "system"
    row = logs_of(sidS)[-1]
    assert row["declared_by"] == "system" and row["declaration_met"] is False
    print("OK 状態機械(5): 中の直後にまた不一致 → miss=2 → 強（場面文提示：いくつ分）")

    # 中・強の対話に答えず作問を送っても止まらない（対話は打ち切り、予告は立ったまま）
    r = judge(sidS, "11", "X: 途中で作問")
    assert r["response_type"] == "form" and r["declared"] == "hougan" and r["miss_count"] == 2
    assert r["strength"] == 3 and r["strength_trigger"] == "none", "不成立では強度も動かない"
    print("OK 対話を無視した作問: ブロックしない・予告は立ったまま")

    # (6) 包含除に到達 → stuck / miss が両方 0 に戻り、強度0の称賛
    r = judge(sidS, "11", "H: あめ24こを8こずつ")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["is_new"] is True, r
    assert r["message"] == d.PRAISE_NEW
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
    r = judge(sidS, "11", "B: 24本は8本の何倍")
    assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["all_reached"] is True
    assert r["message"] == d.DONE_MESSAGE
    for i in range(4):
        r = judge(sidS, "11", f"T: 3つそろった後の反復{i}")
        assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["declared"] is None and r["dialog"] is None, r
        assert r["strength"] == 0
    print("OK 状態機械(7): 3構造で done、以降くり返しても予告支援は出ない")

    # ---- 役割の宣言：一致／わからない／判別不能 は訂正せずターン2へ。訂正の引用は LLM → 除数を含む文 → 引用なし ----
    sidQ = post("/api/login", user_id="14").json()["session_id"]
    judge(sidQ, "14", "T: a"); judge(sidQ, "14", "T: b")
    r = judge(sidQ, "14", "H: あめが24こあります。8こずつふくろに入れます。ふくろは何まいいりますか。")
    assert r["prompt_strength"] == 0 and r["is_new"] is True and r["dialog"] is None
    judge(sidQ, "14", "H: b2")
    r = judge(sidQ, "14", "H: クッキーが24まいあります。1人に8まいずつ配ります。何人に配れますか。")
    assert r["prompt_strength"] == 1 and r["dialog"] == "role" and r["message"] == "5ばんの お話で、8は 何を あらわして いるかな？"
    # 一致（hougan＝1人分の数）→ 訂正なしでターン2
    pb = CALLS.get("phrase", 0)
    r = post("/api/judge", session_id=sidQ, user_id="14", message="1人分の数だよ", declaring=True).json()
    assert r["role_answer"] == "per_one" and r["role_corrected"] is False and r["dialog"] == "declaration"
    assert r["message"] == "じゃあ 次は、8を 何の 数に して みたい？" and CALLS.get("phrase", 0) == pb, "一致なら句の抽出は呼ばない"
    # ターン2で unknown → declared は立てず、再質問もしない。作問入力に戻る
    r = post("/api/judge", session_id=sidQ, user_id="14", message="りんごの問題", declaring=True).json()
    assert r["input_type"] == "declaration" and r["classified"] == "unknown" and r["declared"] is None and r["dialog"] is None
    assert logs_of(sidQ)[-1]["declared_structure"] is None
    r = judge(sidQ, "14", "H: ドーナツが24こあります。8こずつ箱に入れます。箱は何箱いりますか。")
    assert r["prompt_strength"] == 2 and r["strength"] == 2 and r["strength_trigger"] == "stuck"
    assert r["declared"] == "bai" and r["declared_by"] == "system", "未到達は bai"
    assert r["message"] == ("6ばんの お話は そのままで いいよ。8を、もう 1人が もっている ものの 数に して みよう。"
                            "24こと くらべると、どんな ことが 求められるかな？"), ("item が取れないときの中", r["message"])
    # 「わからない」→ 訂正せずターン2へ
    sidR = post("/api/login", user_id="16").json()["session_id"]
    judge(sidR, "16", "T: a"); judge(sidR, "16", "T: b")
    r = judge(sidR, "16", "T: りんごが24こあります。8人で同じ数ずつ分けます。1人分は何こですか。")
    assert r["dialog"] == "role"
    r = post("/api/judge", session_id=sidR, user_id="16", message="わからない", declaring=True).json()
    assert r["role_answer"] == "dont_know" and r["role_corrected"] is False and r["dialog"] == "declaration"
    # ターン2に答えず作問を送る → 対話は打ち切り、通常処理。予告は立たないまま
    r = post("/api/judge", session_id=sidR, user_id="16", message="T: 途中で作問", declaring=True).json()
    assert r["input_type"] == "sakumon" and r["prompt_strength"] == 2 and r["declaration_met"] is None and r["dialog"] is None
    assert logs_of(sidR)[-1]["declared_structure"] is None, "答えなかったので予告は立たない"
    # 判別不能（unknown）→ 訂正せずターン2。LLM の句抽出に失敗 → 除数を含む文を引用。それも無理なら引用なしの定型文
    sidU = post("/api/login", user_id="18").json()["session_id"]
    judge(sidU, "18", "T: a"); judge(sidU, "18", "T: b")
    judge(sidU, "18", "T: 24このあめを8人に同じ数ずつ配ります。1人何こですか。")
    r = post("/api/judge", session_id=sidU, user_id="18", message="えーと", declaring=True).json()
    assert r["role_answer"] == "unknown" and r["role_corrected"] is False and r["dialog"] == "declaration"
    assert d._sentence_with_number("みかんが24こあります。8人で同じ数ずつ分けます。1人分は何こですか。", 8) == "8人で同じ数ずつ分けます"
    assert d._sentence_with_number("みかんが24こあって8人で同じ数ずつ分けると1人分は何こになるでしょうかというもんだいです。", 8) is None, "40字超は引用しない"
    assert d._sentence_with_number("18こを3人で", 8) is None, "18 の 8 は除数ではない"
    assert d.role_correction_message(None, "24 ÷ 8") == "本当に そうかな？ お話を もう一度 読んで みよう。\n8は 何の 数に なって いるかな？"
    assert d.expected_divisor_role("tobun", "one_unit") == "people" and d.expected_divisor_role("hougan", "num_units") == "per_one"
    assert d.expected_divisor_role("bai", "ratio") == "base" and d.expected_divisor_role("bai", "base") is None, "倍率が除数のときは訂正しない"
    assert d.expected_divisor_role("invalid", None) is None
    assert d.format_quote("　1人分は 何こ  ですか。") == "1人分は 何こ ですか"
    assert d.format_quote("「何倍ですか？」") == "何倍ですか？" and d.format_quote("。") is None and d.format_quote(None) is None
    print("OK 役割の宣言: 一致／わからない／判別不能は訂正なし。途中の作問は打ち切り。引用は LLM → 除数の文 → 引用なし")

    # (8) taiwa / resend でカウンタが動かない
    sidT = post("/api/login", user_id="12").json()["session_id"]
    judge(sidT, "12", "T: a"); judge(sidT, "12", "T: b")
    assert database.get_session(sidT)["stuck_count"] == 1
    judge(sidT, "12", "わからない")
    judge(sidT, "12", "わからない")          # 同一本文 → resend
    judge(sidT, "12", "T: b")                # 直前と違う本文なので通常処理 → stuck 2 → 弱
    judge(sidT, "12", "T: b")                # 同一本文 → resend
    lt = logs_of(sidT)
    assert [l["input_type"] for l in lt] == ["sakumon", "sakumon", "taiwa", "resend", "sakumon", "resend"]
    assert [l["stuck_count"] for l in lt] == [0, 1, 1, 1, 2, 2]
    assert [l["miss_count"] for l in lt] == [0] * 6
    st1 = database.get_session(sidT)
    assert st1["stuck_count"] == 2 and st1["miss_count"] == 0 and st1["strength"] == 1
    print("OK 状態機械(8): taiwa / resend ではカウンタが動かない")

    # 中・強の3構造の文言（要件定義 4-5 を一字一句。{item}{unit}{dividend}{divisor} の埋め込み）
    assert d.mid_message("tobun", "24 ÷ 8", 2, "あめ", "こ") == "あめの お話は そのままで いいよ。8を「何人で 分けるか」の 数に して みよう。"
    assert d.mid_message("bai", "24 ÷ 8", 2, "えんぴつ", "本") == ("えんぴつは そのままで いいよ。8を、もう 1人が もっている えんぴつの 数に して みよう。"
                                                                  "24本と くらべると、どんな ことが 求められるかな？")
    assert d.mid_message("tobun", "24 ÷ 8", 4, None, None) == "4ばんの お話は そのままで いいよ。8を「何人で 分けるか」の 数に して みよう。"
    assert d.strong_message("tobun", "24 ÷ 8", 2, "あめ", "こ") == "「あめが 24こ あります。8人で 同じ 数ずつ 分けます。」 この あとに、求める 文を 書いて みよう。"
    assert d.strong_message("bai", "24 ÷ 8", 2, "えんぴつ", "本", session_id=10) == (
        "「たろうさんは えんぴつを 24本、お友だちは 8本 もって います。」 この あとに、「何倍」を つかって 求める 文を 書いて みよう。")
    assert d.strong_message("bai", "24 ÷ 8", 2, "えんぴつ", "本", session_id=11).startswith("「はなこさんは"), "人物名はセッションごとに周期的"
    assert d.strong_message("hougan", "24 ÷ 6", 2, None, None) == "「ものが 24こ あります。1人に 6こずつ 分けます。」 この あとに、求める 文を 書いて みよう。"
    for text in [d.PRAISE_NEW, d.PRAISE_REPEAT, d.ROLE_ASK, d.ROLE_CORRECTION, d.ROLE_CORRECTION_FALLBACK,
                 d.ROLE_NEXT, d.DONE_MESSAGE,
                 d.TALK_FALLBACK, d.TALK_FALLBACK_REWRITE, *d.FORM_MESSAGES.values(), *d.MID_MESSAGES.values(),
                 *d.MID_MESSAGES_NOITEM.values(), *d.STRONG_MESSAGES.values(), *d.STRUCTURE_LABEL.values()]:
        no_banned(text)
    assert d.violates_boundary("この種類のお話はいいね", "talk", "24 ÷ 8") == "banned_vocab:種類"
    assert d.violates_boundary("何をたずねているかな", "talk", "24 ÷ 8") == "banned_vocab:たずね"
    for label in d.STRUCTURE_LABEL.values():
        for text in (d.ROLE_ASK, d.ROLE_CORRECTION, d.ROLE_CORRECTION_FALLBACK, d.ROLE_NEXT):
            assert label not in text, ("弱（役割の宣言）に構造ラベルを出さない", label)
    print("OK 語彙(6章): 児童向け文言に「種類」「たずねる」「聞いていること」「等分除」「包含除」が無い。弱に構造ラベルなし。LLM 出力もガード")

    # ===== 教師画面 live =====
    live = client.get("/admin/api/live", headers=AUTH).json()
    users = {s["user_id"]: s for s in live["students"]}
    assert users["01"]["submitted"] == 9 and users["01"]["valid"] == 5, users["01"]
    assert users["01"]["structures"] == ["tobun", "hougan", "bai"]
    assert users["12"]["stuck_count"] == 2 and users["12"]["declared"] is None
    assert users["03"]["submitted"] == 0 and users["03"]["online"] is True
    print("OK 教師画面: 提出数・成立数・到達・予告・反復/不一致・直近の応答・接続状態")

    # ===== 管理画面からの式の上書き =====
    r = client.post(f"/admin/api/sessions/{sid2A}/expression", json={"expression": "24÷4"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["expression"] == "24 ÷ 4"
    assert client.get(f"/api/config?user_id=01&session_id={sid2A}").json()["expression"] == "24 ÷ 4"
    assert post("/api/session/resume", session_id=sid2A, user_id="01").json()["expression"] == "24 ÷ 4"
    assert client.post(f"/admin/api/sessions/{sid2A}/expression", json={"expression": "25÷4"}, headers=AUTH).status_code == 400
    assert client.post("/admin/api/sessions/99999/expression", json={"expression": "24÷4"}, headers=AUTH).status_code == 404
    r = judge(sid2A, "01", "W: 上書き後")
    assert r["message"].startswith("24と 4を つかう") and logs_of(sid2A)[-1]["expression"] == "24 ÷ 4"
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
        assert pl["expression"] == {1: "24 ÷ 6", 3: "24 ÷ 3"}[ph] and pl["dialog"] is None and pl["declared"] is None
        for msg in ("T: a", "T: b", "T: c", "T: d", "T: e"):
            r = judge(sidP, "13", msg)
            assert r["response_type"] is None and r["prompt_strength"] is None and r["message"] == "おくったよ"
            assert r["declared"] is None and r["stuck_count"] is None and r["dialog"] is None
            assert r["strength"] is None and r["help_count"] is None and r["strength_trigger"] is None
        assert post("/api/declare", session_id=sidP, user_id="13", text="いくつ分").status_code == 400
        assert post("/api/judge", session_id=sidP, user_id="13", message="いくつ分", declaring=True).json()["input_type"] == "taiwa", \
            "フェーズ1・3では declaring を無視して対話として記録"
        lp = logs_of(sidP)
        assert all(l["response_type"] is None and l["prompt_strength"] is None and l["ai_message"] is None for l in lp)
        assert [l["stuck_count"] for l in lp] == [0] * 6 and [l["is_new"] for l in lp[:5]] == [True, False, False, False, False]
        assert lp[-1]["input_type"] == "taiwa"
        assert database.get_session(sidP)["stuck_count"] == 0
    print("OK 状態機械(9): フェーズ1・3では予告支援なし・カウンタも動かない")

    # ===== フェーズ3：別式・新セッション =====
    assert client.get("/api/config").json()["phase"] == 3
    a4 = post("/api/session/new", user_id="01").json()
    assert a4["session_id"] != sidA and a4["phase"] == 3 and a4["expression"] == "24 ÷ 3"
    assert post("/api/session/new", user_id="02").json()["expression"] == "24 ÷ 6"
    r = judge(a4["session_id"], "01", "T: 24このあめを3人で")
    assert r["message"] == "おくったよ" and r["history"] == []
    assert logs_of(a4["session_id"])[0]["phase"] == 3 and logs_of(a4["session_id"])[0]["expression"] == "24 ÷ 3"
    admin_post("/admin/api/phase", phase=2)
    assert post("/api/session/new", user_id="01").json()["session_id"] == sid2A
    admin_post("/admin/api/phase", phase=3)
    r = judge(sid2A, "01", "T: おそく届いた")
    assert r["phase"] == 2 and r["show_support"] is True
    assert raw("SELECT COUNT(*) FROM chat_logs cl JOIN sessions s ON s.session_id=cl.session_id WHERE cl.phase != s.phase")[0][0] == 0
    print("OK フェーズ3: 01→24÷3 / 02→24÷6。chat_logs.phase は常に sessions.phase と一致")

    # ===== CSV =====
    r = client.get("/admin/api/export/csv", headers=AUTH)
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert list(rows[0].keys()) == database.CSV_FIELDS
    for col in ("declared_structure", "declared_by", "declaration_met", "self_label", "self_label_text",
                "self_label_match", "role_answer", "role_corrected",
                "prompt_strength", "produced_structures", "stuck_count", "miss_count", "latency_ms"):
        assert col in rows[0], col
    decl = [x for x in rows if x["input_type"] == "declaration"]
    assert decl and decl[0]["declared_structure"] == "hougan" and decl[0]["declared_by"] == "child"
    roles = [x for x in rows if x["input_type"] == "role"]
    assert roles[0]["role_answer"] == "per_one" and roles[0]["role_corrected"] == "1" and roles[1]["role_corrected"] == "0"
    assert not any(x["input_type"] == "self_label" for x in rows)
    assert all(x["latency_ms"] != "" for x in rows if x["input_type"] != "resend")
    assert any(x["declaration_met"] == "1" for x in rows) and any(x["prompt_strength"] == "3" for x in rows)
    assert all(x["created_at"][:4] == str(now_jst.year) for x in rows)
    print("OK CSV: 新列がすべて出る（declaration / role 行・latency_ms）・JST")

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
            expression_a TEXT NOT NULL DEFAULT '24 ÷ 4', expression_b TEXT NOT NULL DEFAULT '18 ÷ 3',
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
