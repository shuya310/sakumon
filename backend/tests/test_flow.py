"""フェーズ管理・応答の種類・セッション一意性・JST・認証・CSV の決定論的な動作確認（LLM はモック）。

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
import ai_dialogue  # noqa: E402

# ---- モック：メッセージ先頭の記号で判定を決める ----
_JUDGE = {
    "T": {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None},
    "H": {"valid": True, "structure": "hougan", "unknown": "num_units", "issue": None},
    "B": {"valid": True, "structure": "bai", "unknown": "ratio", "issue": None},
    "X": {"valid": False, "structure": "invalid", "unknown": None, "issue": "scene_contradiction"},
    "E": {"valid": False, "structure": "invalid", "unknown": None, "issue": "error", "error": "boom",
          "meta": {"retry_count": 3, "latency_ms": 65000, "status": "failed"}},
}

CALLS = {"judge": 0, "classify": 0, "llm": 0}


def fake_judge(message, expression, user_id=None):
    CALLS["judge"] += 1
    return dict(_JUDGE[message[0]])


def fake_classify(message, recent=None, expression=None, user_id=None):
    CALLS["classify"] += 1
    return "sakumon" if len(message) > 1 and message[1] == ":" and message[0] in _JUDGE else "taiwa"


def fake_llm(child_message, input_kind, judge_result, history, recent_turns, response_type, expression,
             user_id=None):
    CALLS["llm"] += 1
    return {"message": f"[{response_type}] llm", "state": response_type}


def fake_declaration(text, user_id=None):
    CALLS["declare"] = CALLS.get("declare", 0) + 1
    for key, kind in (("1つ分", "tobun"), ("いくつ分", "hougan"), ("何倍", "bai")):
        if key in text:
            return kind
    return "unknown"


ai_classify.classify_declaration = fake_declaration
main.ai_classify.classify_declaration = fake_declaration
ai_judge.judge = fake_judge
main.ai_judge.judge = fake_judge
ai_classify.classify = fake_classify
main.ai_classify.classify = fake_classify
ai_dialogue._llm_message = fake_llm

client = TestClient(main.app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"x:test-pass").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()}


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


with client:
    # ===== スキーマ =====
    tables = {r[0] for r in raw("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "chat_logs", "app_config", "phase_changes"} <= tables
    cols = [r[1] for r in raw("PRAGMA table_info(chat_logs)")]
    assert cols == [
        "log_id", "session_id", "user_id", "phase", "expression", "created_at",
        "input_type", "message", "ai_message",
        "valid", "structure", "unknown", "issue", "is_new",
        "response_type", "prompt_strength",
        "declared_structure", "declared_by", "declaration_met",
        "self_label", "self_label_text", "self_label_match",
        "produced_structures", "stuck_count", "miss_count", "latency_ms",
    ], cols
    scols = [r[1] for r in raw("PRAGMA table_info(sessions)")]
    assert scols == ["session_id", "user_id", "phase", "expression", "parity_group", "session_start", "session_end",
                     "declared", "declared_by", "stuck_count", "miss_count"], scols
    idx = {r[1]: r[2] for r in raw("PRAGMA index_list(sessions)")}
    assert idx.get("idx_sessions_user_phase") == 1, "UNIQUE(user_id, phase)"
    assert "run_id" not in [r[1] for r in raw("PRAGMA table_info(app_config)")]
    print("OK スキーマ: 3章のとおり（chat_logs 26列・sessions 7列・UNIQUE(user_id, phase)・run_id なし）")

    # ===== 認証 =====
    assert client.get("/admin/api/config").status_code == 401
    assert client.get("/admin").status_code == 401
    assert client.get("/admin/api/config", headers=BAD).status_code == 401
    assert client.get("/admin/api/config", headers=AUTH).status_code == 200
    assert client.get("/api/config").status_code == 200  # 認証不要
    print("OK 認証: /admin* は401、正しいパスワードで200、/api/config は認証不要")

    # ===== 初期設定 =====
    cfg = client.get("/api/config").json()
    assert cfg["phase"] == 1 and cfg["expression"] == "24 ÷ 4" and cfg["dividend"] == 24 and cfg["divisor"] == 4
    assert "run_id" not in cfg
    admin_post("/admin/api/expressions", expression_a="24÷4", expression_b="18 / 3")
    assert client.get("/api/config").json()["expression"] == "24 ÷ 4"
    r = client.post("/admin/api/expressions", json={"expression_a": "25 ÷ 4", "expression_b": "18 ÷ 3"}, headers=AUTH)
    assert r.status_code == 400
    print("OK 式: app_config から取得・正規化・不正な式は400")

    # ===== フェーズ1：ログイン＝探して無ければ作る（1児童1フェーズ1セッション） =====
    a = post("/api/login", user_id="01").json()
    a2 = post("/api/login", user_id="01").json()
    assert a["session_id"] == a2["session_id"], "同じフェーズで再ログインしても同一セッション"
    assert a["phase"] == 1 and a["show_support"] is False and "run_id" not in a
    b = post("/api/login", user_id="02").json()
    c = post("/api/login", user_id="03").json()
    assert len({a["session_id"], b["session_id"], c["session_id"]}) == 3
    sidA, sidB, sidC = a["session_id"], b["session_id"], c["session_id"]
    assert raw("SELECT COUNT(*) FROM sessions WHERE user_id='01' AND phase=1")[0][0] == 1
    # DB レベルでも2つ目は作れない
    try:
        raw("INSERT INTO sessions (user_id, phase, expression, parity_group, session_start) VALUES ('01', 1, '24 ÷ 4', 'odd', '2026-09-18 10:00:00')")
        raise AssertionError("UNIQUE が効いていない")
    except sqlite3.IntegrityError:
        pass
    sess = raw("SELECT parity_group, session_start, session_end FROM sessions WHERE session_id=?", sidA)[0]
    assert sess[0] == "odd" and sess[2] is None
    assert raw("SELECT parity_group FROM sessions WHERE session_id=?", sidB)[0][0] == "even"
    # created_at / session_start は JST
    now_jst = datetime.now(database.JST).replace(tzinfo=None)
    started = datetime.strptime(sess[1], "%Y-%m-%d %H:%M:%S")
    assert abs((now_jst - started).total_seconds()) < 120, (sess[1], now_jst)
    print("OK セッション: UNIQUE(user_id, phase)・parity_group（奇偶）・session_start は JST")

    # 所有権チェック
    r = post("/api/judge", session_id=sidA, user_id="02", message="T: x")
    assert r.status_code == 403
    r = post("/api/session/resume", session_id=sidA, user_id="02")
    assert r.status_code == 403
    assert post("/api/session/resume", session_id=sidA, user_id="01").status_code == 200
    print("OK 所有権: 他人の session_id への judge/resume は403")

    # フェーズ1の提出：判定は動くが表示は「おくったよ」。response_type / ai_message は記録しない
    r1 = judge(sidA, "01", "T: おりがみ24まいを4人で 1人分は")
    assert r1["message"] == "おくったよ" and r1["response_type"] is None
    assert r1["valid"] is None and r1["structure"] is None and r1["history"] == [] and r1["accepted"] is False
    judge(sidA, "01", "T: クッキー24こを4人で")
    judge(sidA, "01", "X: 場面矛盾")
    rt = judge(sidA, "01", "わからない")
    assert rt["message"] == "おくったよ"
    logs = logs_of(sidA)
    assert [l["response_type"] for l in logs] == [None] * 4
    assert [l["ai_message"] for l in logs] == [None] * 4
    assert [l["prompt_strength"] for l in logs] == [None] * 4
    assert [l["phase"] for l in logs] == [1, 1, 1, 1]
    assert logs[0]["structure"] == "tobun" and logs[0]["is_new"] is True and logs[0]["unknown"] == "one_unit"
    assert logs[0]["valid"] is True and logs[0]["produced_structures"] == "tobun"
    assert logs[1]["structure"] == "tobun" and logs[1]["is_new"] is False
    assert logs[2]["issue"] == "scene_contradiction" and logs[2]["valid"] is False and logs[2]["miss_count"] == 0
    assert logs[3]["input_type"] == "taiwa" and logs[3]["stuck_count"] == 0 and logs[3]["miss_count"] == 0
    assert [l["stuck_count"] for l in logs] == [0, 0, 0, 0], "フェーズ1では状態機械を動かさない"
    assert all(l["expression"] == "24 ÷ 4" for l in logs)
    assert all(l["latency_ms"] is not None for l in logs)
    assert all(l["declared_by"] is None and l["self_label"] is None for l in logs)
    print("OK フェーズ1: 表示は『おくったよ』のみ。判定・produced は記録、カウンタは動かない、response_type は空")

    # ===== フェーズ2へ切替（別セッション：フェーズ1は引き継がない） =====
    admin_post("/admin/api/phase", phase=2)
    assert client.get("/api/config").json()["phase"] == 2
    # フェーズ1のセッションには終了時刻が打たれる
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sidA)[0][0] is not None
    a3 = post("/api/session/new", user_id="01").json()
    assert a3["session_id"] != sidA, "フェーズ2は新しいセッション"
    assert post("/api/session/new", user_id="01").json()["session_id"] == a3["session_id"], \
        "フェーズ2で二重に呼んでも重複作成しない"
    sid2A = a3["session_id"]
    assert a3["show_support"] is True and a3["ui_strength"] == 0
    assert a3["history"] == [] and a3["problems"] == [] and a3["conversation"] == [], \
        "フェーズ1の産出・到達構造・会話はフェーズ2に引き継がない"
    post("/api/session/new", user_id="03")   # 03 もフェーズ2に入場（提出はしない）
    print("OK フェーズ2: 新セッションで開始し、フェーズ1の産出・到達構造・会話を引き継がない。旧セッションは終了時刻あり")

    # 1問目（新しい聞き方）→ praise（LLM）
    r = judge(sid2A, "01", "T: おりがみ24まいを4人で 1人分は")
    assert r["response_type"] == "praise" and r["is_new"] is True and r["message"] == "[praise] llm"
    assert r["history"] == ["tobun"] and r["accepted"] is True and r["prompt_strength"] == 0
    # 反復 → praise（定型。LLM は呼ばない）
    llm_before = CALLS["llm"]
    r = judge(sid2A, "01", "T: ジュース24Lを4人で")
    assert r["response_type"] == "praise" and r["is_new"] is False
    assert r["message"] == ai_dialogue.REPEAT_MESSAGE and CALLS["llm"] == llm_before
    assert "ちがうことを聞く" not in r["message"]
    # 不成立 → form
    r = judge(sid2A, "01", "X: だめ")
    assert r["response_type"] == "form" and r["message"] == "[form] llm" and r["valid"] is False
    # 対話 → talk。カウンタは動かない
    r = judge(sid2A, "01", "むずかしい")
    assert r["response_type"] == "talk" and r["message"] == "[talk] llm"
    assert r["stuck_count"] == 1 and r["miss_count"] == 0
    r = judge(sid2A, "01", "きゅうしょく おいしかった")
    assert r["response_type"] == "talk" and r["stuck_count"] == 1
    # 新構造 → praise（LLM）
    r = judge(sid2A, "01", "H: あめ24こを4こずつ")
    assert r["response_type"] == "praise" and r["is_new"] is True and sorted(r["history"]) == ["hougan", "tobun"]
    # 3つ目 → done（構造名は出さない）
    r = judge(sid2A, "01", "B: 24人は4人の何倍")
    assert r["response_type"] == "done" and "3つとも作れたね" in r["message"] and r["all_reached"] is True
    assert "等分除" not in r["message"] and "倍の" not in r["message"]
    r = judge(sid2A, "01", "B: 24本は4本の何倍")
    assert r["response_type"] == "done" and "もう3つとも" in r["message"]
    print("OK 応答の種類: 新構造 praise(LLM)・反復 praise(定型)・不成立 form・対話 talk・3つそろって done")

    p2 = logs_of(sid2A)
    assert all(l["phase"] == 2 for l in p2), "フェーズ2のセッションにはフェーズ2のターンだけ"
    assert [l["response_type"] for l in p2] == ["praise", "praise", "form", "talk", "talk", "praise", "done", "done"]
    assert [l["prompt_strength"] for l in p2] == [0] * 8
    assert [l["produced_structures"] for l in p2] == [
        "tobun", "tobun", "tobun", "tobun", "tobun", "tobun,hougan", "tobun,hougan,bai", "tobun,hougan,bai"]
    assert [l["stuck_count"] for l in p2] == [0, 1, 1, 1, 1, 0, 0, 1], "既出で+1、不成立・対話は据え置き、新構造で0"
    assert [l["miss_count"] for l in p2] == [0] * 8
    assert [l["ai_message"] for l in p2][:3] == ["[praise] llm", ai_dialogue.REPEAT_MESSAGE, "[form] llm"]
    assert all(l["ai_message"] for l in p2), "フェーズ2は全ターン ai_message を記録"
    print("OK ログ: produced_structures は固定順の累積、stuck は連続回数、ai_message は全ターン")

    # 再入場：会話は response_type / is_new 付きで返る
    resumed = post("/api/session/resume", session_id=sid2A, user_id="01").json()
    conv = resumed["conversation"]
    assert len(conv) == 8 and conv[0]["response_type"] == "praise" and conv[0]["is_new"] is True
    assert conv[1]["is_new"] is False and conv[6]["response_type"] == "done"
    assert [p["structure"] for p in resumed["problems"]] == ["tobun", "tobun", "hougan", "bai", "bai"]
    assert resumed["all_reached"] is True
    print("OK 再入場: 会話・産出一覧・到達構造を復元")

    # ===== 判定保留・再送 =====
    sidB2 = post("/api/session/new", user_id="02").json()["session_id"]
    assert sidB2 != sidB
    r = judge(sidB2, "02", "X: c")
    assert r["response_type"] == "form"
    r = judge(sidB2, "02", "T: 成立")
    assert r["response_type"] == "praise" and r["is_new"] is True
    # judge が API 不通 → 判定保留として受理（一覧に載る・構造は空・再送は求めない）、miss に数えない
    r = judge(sidB2, "02", "E: err")
    assert r["message"] == main.JUDGE_PENDING_MESSAGE, r["message"]
    assert "もう一度" not in r["message"]
    assert r["accepted"] is True and r["valid"] is None and r["structure"] is None and r["response_type"] == "error"
    logsB = logs_of(sidB2)
    assert logsB[-1]["response_type"] == "error" and logsB[-1]["issue"] == "pending" and logsB[-1]["valid"] is None
    assert logsB[-1]["miss_count"] == 0 and logsB[-1]["stuck_count"] == 0, "保留はカウンタを動かさない"
    resumed = post("/api/session/resume", session_id=sidB2, user_id="02").json()
    assert resumed["problems"][-1] == {"text": "E: err", "structure": None}, "再入場時も一覧に残る"
    assert resumed["history"] == ["tobun"], "保留は到達構造に数えない"
    # 同じ本文の連続再送 → API を呼ばず直前の結果を返す（一覧には追加しない）
    calls_before = dict(CALLS)
    r = judge(sidB2, "02", "E: err")
    assert CALLS == calls_before, "再送で classify / judge / LLM を呼ばない"
    assert r["message"] == main.JUDGE_PENDING_MESSAGE and r["accepted"] is False
    logsB = logs_of(sidB2)
    assert logsB[-1]["input_type"] == "resend" and logsB[-1]["response_type"] == "error"
    assert logsB[-1]["ai_message"] == main.JUDGE_PENDING_MESSAGE and logsB[-1]["latency_ms"] is None
    assert logsB[-1]["stuck_count"] == 0 and logsB[-1]["miss_count"] == 0
    resumed = post("/api/session/resume", session_id=sidB2, user_id="02").json()
    assert sum(1 for p in resumed["problems"] if p["text"] == "E: err") == 1, "再送で一覧に重複しない"
    # 別の本文なら通常どおり API を呼ぶ
    r = judge(sidB2, "02", "T: べつの")
    assert CALLS["judge"] == calls_before["judge"] + 1 and r["accepted"] is True
    print("OK 判定保留: API不通の作問は受理して一覧に載せる・カウンタを動かさない・同一本文の再送はAPIを呼ばない")

    # 待ち状態の問い合わせ（送信中でなければ null）
    assert client.get("/api/judge/status?user_id=02").json() == {"state": None}
    assert client.get("/api/judge/status?user_id=abc").status_code == 400

    # ===== is_new はフェーズスコープ（同じ児童・同じフェーズなら別セッションでも既出） =====
    # UNIQUE(user_id, phase) で通常は1セッションだが、get_produced は user_id+phase で引く
    database.save_log(session_id=99999, user_id="02", phase=2, expression="24 ÷ 4", input_type="sakumon",
                      message="別セッションの倍", ai_message=None, valid=True, structure="bai", unknown="ratio",
                      is_new=True, produced_structures=["tobun", "bai"], stuck_count=0, miss_count=0)
    assert database.get_produced("02", 2) == ["tobun", "bai"]
    r = judge(sidB2, "02", "B: 24本は4本の何倍")
    assert r["is_new"] is False, "別セッションで既出の構造は is_new=0"
    assert database.get_produced("02", 1) == [], "フェーズが違えば数えない"
    raw("DELETE FROM chat_logs WHERE session_id=99999")
    print("OK is_new: フェーズスコープ（別セッションでも既出なら 0、別フェーズは数えない）")

    # ===== 教師画面 live =====
    live = client.get("/admin/api/live", headers=AUTH).json()
    users = {s["user_id"]: s for s in live["students"]}
    assert users["01"]["submitted"] == 6 and users["01"]["valid"] == 5, users["01"]
    assert users["01"]["structures"] == ["tobun", "hougan", "bai"]
    assert users["01"]["stuck_count"] == 1 and users["01"]["miss_count"] == 0
    assert users["01"]["last_response_type"] == "done"
    assert users["03"]["submitted"] == 0 and users["03"]["online"] is True  # ログインしただけ（提出0）
    assert "learner_state" not in users["01"] and "current_level" not in users["01"]
    print("OK 教師画面: 児童ごとの提出数・成立数・到達構造・困り/不成立・直近の応答・接続状態")

    # ===== ログアウト → 終了時刻、再入場で消える =====
    assert post("/api/session/end", session_id=sid2A, user_id="01").status_code == 200
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sid2A)[0][0] is not None
    assert post("/api/session/end", session_id=sid2A, user_id="02").status_code == 403
    post("/api/login", user_id="01")
    assert raw("SELECT session_end FROM sessions WHERE session_id=?", sid2A)[0][0] is None, "再入場で再開"
    print("OK 終了時刻: ログアウトで記録、入り直すと消える")

    # ===== フェーズ3へ：新セッション、到達構造リセット =====
    admin_post("/admin/api/phase", phase=3)
    assert client.get("/api/config").json()["expression"] == "18 ÷ 3"
    a4 = post("/api/session/new", user_id="01").json()
    assert a4["session_id"] != sidA and a4["phase"] == 3 and a4["show_support"] is False
    assert a4["history"] == [] and a4["problems"] == [] and a4["conversation"] == []
    a5 = post("/api/session/new", user_id="01").json()
    assert a5["session_id"] == a4["session_id"], "フェーズ3で二重に呼んでも重複作成しない"
    r = judge(a4["session_id"], "01", "T: 18このあめを3人で")
    assert r["message"] == "おくったよ" and r["history"] == []
    logs3 = logs_of(a4["session_id"])
    assert logs3[0]["phase"] == 3 and logs3[0]["expression"] == "18 ÷ 3" and logs3[0]["is_new"] is True
    assert logs3[0]["response_type"] is None and logs3[0]["produced_structures"] == "tobun"
    # 誤操作で 3→2 に戻しても、フェーズ2のセッションに戻れる
    admin_post("/admin/api/phase", phase=2)
    assert post("/api/session/new", user_id="01").json()["session_id"] == sid2A
    admin_post("/admin/api/phase", phase=3)
    # フェーズ切替直後に旧画面（フェーズ2のセッション）から届いた送信は、セッションのフェーズで記録する
    r = judge(sid2A, "01", "T: おそく届いた")
    assert r["phase"] == 2 and r["show_support"] is True
    assert logs_of(sid2A)[-1]["phase"] == 2
    assert raw("SELECT COUNT(*) FROM chat_logs cl JOIN sessions s ON s.session_id=cl.session_id WHERE cl.phase != s.phase")[0][0] == 0
    print("OK フェーズ3: 別式・新セッション・到達構造はゼロから。chat_logs.phase は常に sessions.phase と一致")

    # ===== 状態機械（仕様 v2 7章の9項目） =====
    admin_post("/admin/api/phase", phase=2)
    sidS = post("/api/login", user_id="11").json()["session_id"]

    # (1) 不成立を3回連続 → stuck_count は 0 のまま（form のみ）
    for _ in range(3):
        r = judge(sidS, "11", f"X: 不成立{_}")
        assert r["response_type"] == "form" and r["stuck_count"] == 0 and r["miss_count"] == 0
    assert [l["stuck_count"] for l in logs_of(sidS)] == [0, 0, 0]
    print("OK 状態機械(1): 不成立3連続で stuck_count は 0 のまま")

    # (2) 等分除→等分除→等分除 → stuck 0→1→2、3問目で弱
    r = judge(sidS, "11", "T: おりがみ24まいを4人で")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 0 and r["is_new"] is True
    r = judge(sidS, "11", "T: あめ24こを4人で")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["stuck_count"] == 1
    r = judge(sidS, "11", "T: みかん24こを4人で")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 1 and r["stuck_count"] == 2, r
    assert r["declared"] is None, "弱ではまだ予告は立たない（児童が書く）"
    print("OK 状態機械(2): 同構造 0→1→2、3問目で弱（強さ1）")

    # (3) 弱で「いくつ分をきく」と入力 → declared=hougan, declared_by=child、目標が入場情報に出る
    r = post("/api/declare", session_id=sidS, user_id="11", text="いくつ分をきく").json()
    assert r["declared"] == "hougan" and r["declared_by"] == "child", r
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "child"
    assert st["stuck_count"] == 2 and st["miss_count"] == 0, "予告ではカウンタを動かさない"
    d = logs_of(sidS)[-1]
    assert d["input_type"] == "declaration" and d["declared_structure"] == "hougan" and d["declared_by"] == "child"
    assert d["message"] == "いくつ分をきく" and d["latency_ms"] is not None
    resumed = post("/api/session/resume", session_id=sidS, user_id="11").json()
    assert resumed["declared"] == "hougan" and resumed["declared_by"] == "child", "目標が固定表示できる"
    # unknown なら立てない・再質問しない
    r = post("/api/declare", session_id=sidS, user_id="11", text="りんごの問題").json()
    assert r["declared"] is None and r["classified"] == "unknown"
    assert database.get_session(sidS)["declared"] == "hougan", "unknown で既存の予告は消さない"
    assert post("/api/declare", session_id=sidA, user_id="01", text="いくつ分").status_code == 400, "フェーズ2以外は受け付けない"
    print("OK 状態機械(3): 弱で「いくつ分をきく」→ declared=hougan / declared_by=child")

    # (4) その次に等分除 → declaration_met=0, miss=1 → 中
    r = judge(sidS, "11", "T: えんぴつ24本を4人で")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 2, r
    assert r["declaration_met"] is False and r["miss_count"] == 1 and r["stuck_count"] == 3
    row = logs_of(sidS)[-1]
    assert row["declared_structure"] == "hougan" and row["declared_by"] == "child" and row["declaration_met"] is False
    assert row["miss_count"] == 1 and row["prompt_strength"] == 2
    st = database.get_session(sidS)
    assert st["declared"] == "hougan" and st["declared_by"] == "system", "中：システムが未到達構造を目標に立てる"
    print("OK 状態機械(4): 予告と違う構造 → declaration_met=0, miss=1 → 中（強さ2）")

    # (5) 中の直後にまた予告と違う構造 → miss=2 → 強
    r = judge(sidS, "11", "T: ジュース24Lを4人で")
    assert r["response_type"] == "prompt" and r["prompt_strength"] == 3, r
    assert r["declaration_met"] is False and r["miss_count"] == 2 and r["stuck_count"] == 4
    row = logs_of(sidS)[-1]
    assert row["declared_by"] == "system" and row["declaration_met"] is False
    assert database.get_session(sidS)["declared"] == "hougan"
    print("OK 状態機械(5): 中の直後にまた不一致 → miss=2 → 強（強さ3）")

    # (6) 包含除に到達 → stuck / miss が両方 0 に戻り、強度0の称賛
    r = judge(sidS, "11", "H: あめ24こを4こずつ")
    assert r["response_type"] == "praise" and r["prompt_strength"] == 0 and r["is_new"] is True, r
    assert r["stuck_count"] == 0 and r["miss_count"] == 0 and r["declaration_met"] is True
    st = database.get_session(sidS)
    assert st["stuck_count"] == 0 and st["miss_count"] == 0 and st["declared"] is None
    row = logs_of(sidS)[-1]
    assert row["declared_structure"] == "hougan" and row["declaration_met"] is True and row["produced_structures"] == "tobun,hougan"
    print("OK 状態機械(6): 新構造到達で stuck / miss が両方 0、強度0の称賛、予告は消費")

    # (7) 3構造そろう → done。以降支援なし
    r = judge(sidS, "11", "B: 24本は4本の何倍")
    assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["all_reached"] is True
    for msg in ("T: 3つそろった後の反復1", "T: 3つそろった後の反復2", "T: 3つそろった後の反復3", "T: 3つそろった後の反復4"):
        r = judge(sidS, "11", msg)
        assert r["response_type"] == "done" and r["prompt_strength"] == 0 and r["declared"] is None, r
    print("OK 状態機械(7): 3構造で done、以降くり返しても予告支援は出ない")

    # (8) taiwa / resend でカウンタが動かない
    sidT = post("/api/login", user_id="12").json()["session_id"]
    judge(sidT, "12", "T: a"); judge(sidT, "12", "T: b")
    st0 = database.get_session(sidT)
    assert st0["stuck_count"] == 1
    judge(sidT, "12", "わからない")
    judge(sidT, "12", "わからない")          # 同一本文 → resend
    judge(sidT, "12", "T: b")                # 直前と違う本文なので通常処理 → stuck 2 → 弱
    judge(sidT, "12", "T: b")                # 同一本文 → resend
    lt = logs_of(sidT)
    assert [l["input_type"] for l in lt] == ["sakumon", "sakumon", "taiwa", "resend", "sakumon", "resend"]
    assert [l["stuck_count"] for l in lt] == [0, 1, 1, 1, 2, 2]
    assert [l["miss_count"] for l in lt] == [0] * 6
    st1 = database.get_session(sidT)
    assert st1["stuck_count"] == 2 and st1["miss_count"] == 0
    print("OK 状態機械(8): taiwa / resend ではカウンタが動かない")

    # (9) Phase1 / Phase3 で予告支援が一切出ない（判定だけ記録、カウンタも動かない）
    for ph in (1, 3):
        admin_post("/admin/api/phase", phase=ph)
        sidP = post("/api/login", user_id="13").json()["session_id"]
        for msg in ("T: a", "T: b", "T: c", "T: d", "T: e"):
            r = judge(sidP, "13", msg)
            assert r["response_type"] is None and r["prompt_strength"] is None and r["message"] == "おくったよ"
            assert r["declared"] is None and r["stuck_count"] is None
        assert post("/api/declare", session_id=sidP, user_id="13", text="いくつ分").status_code == 400
        lp = logs_of(sidP)
        assert all(l["response_type"] is None and l["prompt_strength"] is None for l in lp)
        assert [l["stuck_count"] for l in lp] == [0] * 5 and [l["is_new"] for l in lp] == [True, False, False, False, False]
        assert database.get_session(sidP)["stuck_count"] == 0
    admin_post("/admin/api/phase", phase=3)
    print("OK 状態機械(9): フェーズ1・3では予告支援なし・カウンタも動かない")

    # ===== CSV =====
    r = client.get("/admin/api/export/csv", headers=AUTH)
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == database.CSV_FIELDS
    for col in ("run_id", "session_phase", "session_new_count", "support_level", "learner_state", "stall_count",
                "button_pressed", "target_structure", "stumble", "display_type", "state"):
        assert col not in rows[0], col
    decl = [x for x in rows if x["input_type"] == "declaration"]
    assert decl and decl[0]["declared_structure"] == "hougan" and decl[0]["declared_by"] == "child"
    pend = [x for x in rows if x["user_id"] == "02" and x["issue"] == "pending"]
    assert len(pend) == 1 and pend[0]["response_type"] == "error" and pend[0]["valid"] == ""
    r01 = [x for x in rows if x["user_id"] == "01" and x["phase"] == "2"]
    assert r01[0]["response_type"] == "praise" and r01[0]["is_new"] == "1" and r01[0]["produced_structures"] == "tobun"
    assert r01[0]["session_start"] and r01[0]["parity_group"] == "odd"
    assert all(x["created_at"][:4] == str(now_jst.year) for x in rows)
    print("OK CSV: 新列のみ・旧列なし・JST")

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
        INSERT INTO app_config (id, current_phase, expression_a, expression_b, run_id) VALUES (1, 2, '30 ÷ 5', '18 ÷ 3', 3);
    """)
    con.commit(); con.close()
    orig = database.DB_PATH
    database.DB_PATH = legacy
    try:
        database.init_db()
        names = {r[0] for r in sqlite3.connect(legacy).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert any(n.startswith("chat_logs_legacy_") for n in names) and any(n.startswith("sessions_legacy_") for n in names), names
        assert any(n.startswith("app_config_legacy_") for n in names)
        lcon = sqlite3.connect(legacy)
        legacy_logs = [n for n in names if n.startswith("chat_logs_legacy_")][0]
        assert lcon.execute(f"SELECT message FROM {legacy_logs}").fetchone()[0] == "旧データ", "旧データは消えない"
        assert [r[1] for r in lcon.execute("PRAGMA table_info(chat_logs)")][:3] == ["log_id", "session_id", "user_id"]
        assert lcon.execute("SELECT COUNT(*) FROM chat_logs").fetchone()[0] == 0
        assert database.get_config()["current_phase"] == 1, "設定は既定値で作り直す（式は管理画面で再設定）"
        database.init_db()  # 2回目は何もしない
        names2 = {r[0] for r in sqlite3.connect(legacy).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names2 == names
    finally:
        database.DB_PATH = orig
    print("OK 旧スキーマ: 起動時に *_legacy_日付 へ改名して退避（DROP しない）、新スキーマで作り直す")

print("\nALL PASSED")
