"""フェーズ管理・支援水準・学習者状態・認証・CSV の決定論的な動作確認（LLM はモック）。

実行: cd backend && ./venv/bin/python tests/test_flow.py
一時DBを使うので data/sakumon.db には触れない。
"""
import base64
import csv
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = str(Path(_tmp) / "test.db")
os.environ.setdefault("ADMIN_PASSWORD", "test-pass")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import ai_judge  # noqa: E402
import ai_classify  # noqa: E402
import ai_dialogue  # noqa: E402

# ---- モック：メッセージ先頭の記号で判定を決める ----
_JUDGE = {
    "T": {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None},
    "H": {"valid": True, "structure": "hougan", "unknown": "num_units", "issue": None},
    "B": {"valid": True, "structure": "bai", "unknown": "ratio", "issue": None},
    "X": {"valid": False, "structure": "invalid", "unknown": None, "issue": "scene_contradiction"},
    "E": {"valid": False, "structure": "invalid", "unknown": None, "issue": "error", "error": "boom"},
}


def fake_judge(message, expression):
    return dict(_JUDGE[message[0]])


def fake_classify(message, recent=None, expression=None):
    return "sakumon" if len(message) > 1 and message[1] == ":" and message[0] in _JUDGE else "taiwa"


def fake_llm(child_message, input_kind, judge_result, history, recent_turns, support_level, expression, target):
    return {"message": f"[{support_level}] llm", "state": support_level}


ai_judge.judge = fake_judge
main.ai_judge.judge = fake_judge
ai_classify.classify = fake_classify
main.ai_classify.classify = fake_classify
ai_dialogue._llm_message = fake_llm

client = TestClient(main.app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"x:test-pass").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()}


def post(path, **body):
    r = client.post(path, json=body)
    return r


def judge(sid, uid, msg, button=None):
    r = post("/api/judge", session_id=sid, user_id=uid, message=msg, button_pressed=button)
    assert r.status_code == 200, r.text
    return r.json()


def admin_post(path, **body):
    r = client.post(path, json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


with client:
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
    admin_post("/admin/api/expressions", expression_a="24÷4", expression_b="18 / 3")
    assert client.get("/api/config").json()["expression"] == "24 ÷ 4"
    r = client.post("/admin/api/expressions", json={"expression_a": "25 ÷ 4", "expression_b": "18 ÷ 3"}, headers=AUTH)
    assert r.status_code == 400
    print("OK 式: app_config から取得・正規化・不正な式は400")

    # ===== フェーズ1：ログイン＝探して無ければ作る =====
    a = post("/api/login", user_id="01").json()
    a2 = post("/api/login", user_id="01").json()
    assert a["session_id"] == a2["session_id"], "同じフェーズで再ログインしても同一セッション"
    assert a["phase"] == 1 and a["show_support"] is False
    b = post("/api/login", user_id="02").json()
    c = post("/api/login", user_id="03").json()
    assert len({a["session_id"], b["session_id"], c["session_id"]}) == 3
    sidA, sidB, sidC = a["session_id"], b["session_id"], c["session_id"]

    # 所有権チェック
    r = post("/api/judge", session_id=sidA, user_id="02", message="T: x")
    assert r.status_code == 403
    r = post("/api/session/resume", session_id=sidA, user_id="02")
    assert r.status_code == 403
    assert post("/api/session/resume", session_id=sidA, user_id="01").status_code == 200
    print("OK 所有権: 他人の session_id への judge/resume は403")

    # フェーズ1の提出：判定は動くが表示は「おくったよ」
    r1 = judge(sidA, "01", "T: おりがみ24まいを4人で 1人分は")
    assert r1["message"] == "おくったよ" and r1["display_type"] == "ack"
    assert r1["valid"] is None and r1["structure"] is None and r1["history"] == []
    r2 = judge(sidA, "01", "T: クッキー24こを4人で")
    r3 = judge(sidA, "01", "X: 場面矛盾")
    rt = judge(sidA, "01", "こんにちは")
    assert rt["message"] == "おくったよ"
    logs = client.get(f"/admin/api/sessions/{sidA}", headers=AUTH).json()
    assert [l["support_level"] for l in logs] == ["none", "none", "none", "none"]
    assert [l["phase"] for l in logs] == [1, 1, 1, 1]
    assert logs[0]["structure"] == "tobun" and logs[0]["is_new"] is True and logs[0]["unknown"] == "one_unit"
    assert logs[1]["structure"] == "tobun" and logs[1]["is_new"] is False and logs[1]["stall_count"] == 1
    assert logs[2]["issue"] == "scene_contradiction" and logs[2]["learner_state"] == "S1"
    assert logs[3]["input_type"] == "taiwa"
    assert all(l["expression"] == "24 ÷ 4" for l in logs)
    print("OK フェーズ1: 表示は『おくったよ』のみ、judge/classify の結果は全ターン記録（support_level=none）")

    # 児童B：フェーズ1で2問連続不成立 → S0
    judge(sidB, "02", "X: a")
    rb = judge(sidB, "02", "X: b")
    logsB = client.get(f"/admin/api/sessions/{sidB}", headers=AUTH).json()
    assert logsB[1]["learner_state"] == "S0"
    print("OK 学習者状態: 2問連続不成立で S0")

    # ===== フェーズ2へ切替（別セッション：フェーズ1は引き継がない） =====
    admin_post("/admin/api/phase", phase=2)
    assert client.get("/api/config").json()["phase"] == 2
    a3 = post("/api/session/new", user_id="01").json()
    assert a3["session_id"] != sidA, "フェーズ2は新しいセッション"
    assert post("/api/session/new", user_id="01").json()["session_id"] == a3["session_id"], \
        "フェーズ2で二重に呼んでも重複作成しない"
    sid2A = a3["session_id"]
    assert a3["show_support"] is True and a3["ui_level"] == 0
    assert a3["history"] == [] and a3["problems"] == [] and a3["conversation"] == [], \
        "フェーズ1の産出・信号機・会話はフェーズ2に引き継がない"
    post("/api/session/new", user_id="03")   # 03 もフェーズ2に入場（提出はしない）
    print("OK フェーズ2: 新セッションで開始し、フェーズ1の産出・到達構造・会話を引き継がない")

    # 1問目（フェーズ2で最初の成立）→ 新構造なので discover
    r = judge(sid2A, "01", "T: おりがみ24まいを4人で 1人分は")
    assert r["display_type"] == "new_structure" and r["history"] == ["tobun"]

    # 反復 → level1（産出一覧は右パネルに誘導。チャットには列挙しない）
    r = judge(sid2A, "01", "T: ジュース24Lを4人で")
    assert r["display_type"] == "level1", r
    assert r["message"] == "今までに 作った お話が、右に ならんでいるよ。\nもう一度 読みかえしてみよう。"
    assert r["highlight_problems"] is True
    assert "1つ分" not in r["message"] and r["ui_level"] == 1
    # 反復 → level2（2択ボタン）
    r = judge(sid2A, "01", "T: えんぴつ24本を4人で")
    assert r["display_type"] == "level2" and r["buttons"] == ["同じ", "ちがう"]
    assert "この3つは、聞いていることが 同じかな？ ちがうかな？" in r["message"]
    assert r["highlight_problems"] is True
    # 「同じ」→ 水準は上がらない
    r = judge(sid2A, "01", "同じ", button="同じ")
    assert r["display_type"] == "level2" and "ちがうことを聞くお話" in r["message"]
    # 反復 → level3（求める量の明示）
    r = judge(sid2A, "01", "T: リボン24cmを4人で")
    assert r["display_type"] == "level3" and "「1つ分はいくつ？」「いくつ分ある？」「何倍？」" in r["message"]
    assert r["ui_level"] == 3
    # 反復 → level4（LLM、target_structure=hougan）
    r = judge(sid2A, "01", "T: あめ24こを4人で")
    assert r["display_type"] == "level4" and r["message"] == "[level4] llm"
    # 反復 → level4 のまま（上限）
    r = judge(sid2A, "01", "T: みかん24こを4人で")
    assert r["display_type"] == "level4"
    # 新構造 → discover、水準リセット
    r = judge(sid2A, "01", "H: あめ24こを4こずつ")
    assert r["display_type"] == "new_structure" and r["message"] == "[discover] llm"
    assert sorted(r["history"]) == ["hougan", "tobun"]
    # 反復 → level1 からやり直し
    r = judge(sid2A, "01", "H: クッキー24こを4こずつ")
    assert r["display_type"] == "level1"
    # 複数構造がまじった状態の level2 は反復した構造の番号を名指しする
    r = judge(sid2A, "01", "H: ジュース24Lを4Lずつ")
    assert r["display_type"] == "level2" and "7ばんと8ばんと9ばんは、聞いていることが" in r["message"], r["message"]
    # 「ちがう」→ 即時 level3（テキスト優先。ボタンは「同じ」を押してから書き換えた想定）
    r = judge(sid2A, "01", "ちがうと思う", button="同じ")
    assert r["display_type"] == "level3"
    # 次の反復は level4
    r = judge(sid2A, "01", "H: えんぴつ24本を4本ずつ")
    assert r["display_type"] == "level4"
    # 不成立 → form（水準は動かない）
    r = judge(sid2A, "01", "X: だめ")
    assert r["display_type"] == "normal" and r["message"] == "[form] llm" and r["valid"] is False
    # 対話（困り表明）→ 水準を1段上げる。ここは上限なので level4
    r = judge(sid2A, "01", "むずかしい")
    assert r["display_type"] == "level4" and r["message"] == "[level4] llm"
    # 対話（困りではないつぶやき）→ talk のまま
    r = judge(sid2A, "01", "きゅうしょく おいしかった")
    assert r["display_type"] == "normal" and r["message"] == "[talk] llm"
    # 3つ目 → goal（構造名は出さない）
    r = judge(sid2A, "01", "B: 24人は4人の何倍")
    assert r["display_type"] == "goal" and "3つとも作れたね" in r["message"] and r["all_reached"] is True
    assert "等分除" not in r["message"] and "倍の" not in r["message"]
    r = judge(sid2A, "01", "B: 24本は4本の何倍")
    assert r["display_type"] == "goal" and "もう3つとも" in r["message"]
    print("OK 水準遷移: 反復で 1→2→3→4（1提出1段階・上限4）、新構造で discover→1にリセット、"
          "『同じ』は据え置き・『ちがう』は即時3、不成立は form、goal は構造名なし")

    p2 = client.get(f"/admin/api/sessions/{sid2A}", headers=AUTH).json()
    assert all(l["phase"] == 2 for l in p2), "フェーズ2のセッションにはフェーズ2のターンだけ"
    seq = [(l["input_type"], l["support_level"], l["is_new"]) for l in p2]
    assert [s[1] for s in seq] == ["discover", "level1", "level2", "level2", "level3", "level4", "level4",
                                   "discover", "level1", "level2", "level3", "level4", "form", "level4",
                                   "talk", "goal", "goal"], seq
    same_turn = p2[3]
    assert same_turn["button_pressed"] == "同じ" and same_turn["message"] == "同じ"
    diff_turn = p2[10]
    assert diff_turn["button_pressed"] == "同じ" and diff_turn["message"] == "ちがうと思う"
    assert p2[5]["target_structure"] == "hougan"
    assert p2[7]["learner_state"] == "S3", p2[7]["learner_state"]  # level4 直後の新構造 → S3（暫定）
    assert all(l["support_level"] for l in p2), "support_level は全ターン必ず記録"
    print("OK ログ: 全ターンに support_level、ボタン値と送信文の両方、target_structure、S3暫定値")

    # 児童B（S0）：フェーズ2で不成立 → form のみ、成立で脱出
    # フェーズ1で2問不成立でも、S0 はフェーズ2の提出だけで数え直す
    sidB2 = post("/api/session/new", user_id="02").json()["session_id"]
    assert sidB2 != sidB
    r = judge(sidB2, "02", "X: c")
    assert r["display_type"] == "normal"
    logsB = client.get(f"/admin/api/sessions/{sidB2}", headers=AUTH).json()
    assert logsB[-1]["learner_state"] == "S1" and logsB[-1]["support_level"] == "form", \
        "フェーズ1の不成立は数えないので、フェーズ2の1問目だけでは S0 にならない"
    r = judge(sidB2, "02", "X: d")
    logsB = client.get(f"/admin/api/sessions/{sidB2}", headers=AUTH).json()
    assert logsB[-1]["learner_state"] == "S0" and logsB[-1]["support_level"] == "form"
    r = judge(sidB2, "02", "T: 成立")
    logsB = client.get(f"/admin/api/sessions/{sidB2}", headers=AUTH).json()
    assert logsB[-1]["learner_state"] == "S1" and logsB[-1]["support_level"] == "discover"
    # judge エラー → フォールバック、S0 判定に数えない
    r = judge(sidB2, "02", "E: err")
    assert r["message"] == ai_dialogue.FALLBACK_MESSAGE
    logsB = client.get(f"/admin/api/sessions/{sidB2}", headers=AUTH).json()
    assert logsB[-1]["support_level"] == "error" and logsB[-1]["issue"] == "error"
    print("OK S0: 不成立は form のみ、成立1問で脱出、judge エラーは error として記録")

    # ===== 教師画面 live =====
    live = client.get("/admin/api/live", headers=AUTH).json()
    users = {s["user_id"]: s for s in live["students"]}
    assert users["01"]["submitted"] == 13 and users["01"]["valid"] == 12, users["01"]
    assert sorted(users["01"]["structures"]) == ["bai", "hougan", "tobun"]
    assert users["03"]["submitted"] == 0 and users["03"]["online"] is True  # ログインしただけ（提出0）
    assert users["01"]["online"] is True
    print("OK 教師画面: 児童ごとの提出数・成立数・到達構造・接続状態")

    # ===== フェーズ3へ：新セッション、信号機・停滞リセット =====
    admin_post("/admin/api/phase", phase=3)
    assert client.get("/api/config").json()["expression"] == "18 ÷ 3"
    a4 = post("/api/session/new", user_id="01").json()
    assert a4["session_id"] != sidA and a4["phase"] == 3 and a4["show_support"] is False
    assert a4["history"] == [] and a4["problems"] == [] and a4["conversation"] == []
    a5 = post("/api/session/new", user_id="01").json()
    assert a5["session_id"] == a4["session_id"], "フェーズ3で二重に呼んでも重複作成しない"
    r = judge(a4["session_id"], "01", "T: 18このあめを3人で")
    assert r["message"] == "おくったよ" and r["history"] == []
    logs3 = client.get(f"/admin/api/sessions/{a4['session_id']}", headers=AUTH).json()
    assert logs3[0]["phase"] == 3 and logs3[0]["expression"] == "18 ÷ 3" and logs3[0]["is_new"] is True
    assert logs3[0]["stall_count"] == 0
    # 誤操作で 3→2 に戻しても、フェーズ2のセッションに戻れる
    admin_post("/admin/api/phase", phase=2)
    assert post("/api/session/new", user_id="01").json()["session_id"] == sid2A
    admin_post("/admin/api/phase", phase=3)
    print("OK フェーズ3: 別式・新セッション・信号機と停滞カウントはゼロから。判定はログのみ")

    # ===== 新しい回 =====
    admin_post("/admin/api/new_run")
    cfg = client.get("/api/config").json()
    assert cfg["phase"] == 1 and cfg["run_id"] == 2
    a6 = post("/api/login", user_id="01").json()
    assert a6["session_id"] not in (sidA, sid2A, a4["session_id"]), "新しい回では旧セッションを拾わない"
    print("OK 新しい回: run_id が進み、旧セッションを再開しない（データは残る）")

    # ===== 困り表明で支援水準が上がる（対話も停滞シグナルとして扱う） =====
    admin_post("/admin/api/phase", phase=2)
    sidS = post("/api/login", user_id="09").json()["session_id"]
    # 1問も作れていないうちの困り → 産出の比較は成り立たないので場面想起（水準4）
    r = judge(sidS, "09", "わからない")
    assert r["display_type"] == "level4" and r["target_structure"] == "tobun", r
    # 1問成立 → discover で水準リセット
    r = judge(sidS, "09", "T: あめ24こを4人で")
    assert r["display_type"] == "new_structure"
    # 以降の困りは作問の反復と同じ段（1→2→3）を上がる
    r = judge(sidS, "09", "どうしたらいいの")
    assert r["display_type"] == "level1" and r["highlight_problems"] is True, r
    r = judge(sidS, "09", "思いつかない")
    assert r["display_type"] == "level2" and r["buttons"] == ["同じ", "ちがう"], r
    r = judge(sidS, "09", "やっぱりむずかしい")
    assert r["display_type"] == "level3", r
    # 3構造そろったあとの困りは上げる先がないので talk
    judge(sidS, "09", "H: あめ24こを4こずつ")
    judge(sidS, "09", "B: 24本は4本の何倍")
    r = judge(sidS, "09", "わからない")
    assert r["display_type"] == "normal" and r["message"] == "[talk] llm", r
    # 困りではないつぶやきは水準を動かさない
    before = client.get(f"/admin/api/sessions/{sidS}", headers=AUTH).json()
    r = judge(sidS, "09", "きょうは 雨だね")
    logsS = client.get(f"/admin/api/sessions/{sidS}", headers=AUTH).json()
    assert logsS[-1]["support_level"] == "talk" and len(logsS) == len(before) + 1
    assert [l["support_level"] for l in logsS] == [
        "level4", "discover", "level1", "level2", "level3", "discover", "goal", "talk", "talk"], logsS
    print("OK 困り表明: 対話でも水準が1段ずつ上がる（産出なしは水準4・3つそろえば talk）")

    # ===== CSV =====
    r = client.get("/admin/api/export/csv", headers=AUTH)
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    for col in ("phase", "support_level", "learner_state", "unknown", "issue", "button_pressed",
                "stall_count", "target_structure", "expression", "run_id", "log_id", "valid", "display_type"):
        assert col in rows[0], col
    # 復元例：児童01のフェーズ2で「水準Nの声かけ直後の提出で新構造が出たか」
    r01 = [x for x in rows if x["user_id"] == "01" and x["phase"] == "2"]
    pairs = [(prev["support_level"], cur["is_new"], cur["structure"])
             for prev, cur in zip(r01, r01[1:]) if cur["input_type"] == "sakumon"]
    assert ("level4", "1", "hougan") in pairs, pairs
    print("OK CSV: 追加カラムを含む。水準N直後の新構造出現をCSVから復元できる")

print("\nALL PASSED")
