"""対話AI（talk）の実出力と境界ガード（構造名・答え・語彙・休けい・行き先・問いの文）の確認。実際に API を呼ぶ。
form / praise / prompt / done は定型文（ai_dialogue の表）なので LLM は呼ばない。
題材は 9/18 の模擬実践（#04）の流れ：包含除を3問（おにぎり・ビー玉・クッキー）→ 弱 → 中（等分除）→ 強。
実行: cd backend && ./venv/bin/python tests/dialogue_probe.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_dialogue as d

EXPR = "24 ÷ 4"
P1 = "おにぎりが24こあります。1人に4こずつくばります。おにぎりは何人にくばれますか。"
P2 = "ビー玉が24こひつようです。1人4こずつ買います。何人でビー玉をかいにいけばいいですか。"
P3 = "クッキーが24個あります。1人に4つくばると何人にくばることができますか。"
role = d.divisor_role_label("hougan", "num_units", 4)


def prob(text):
    return {"text": text, "divisor_role": role, "question_phrase": d.extract_question_phrase(text)}


problems = [prob(P1), prob(P2), prob(P3)]
last = {**prob(P3), "structure": "hougan", "unknown": "num_units", "item": "クッキー", "unit": "こ"}
praise = d.praise_message(False, EXPR, last["question_phrase"])
weak = d.weak_message([{**p, "structure": "hougan", "unit": "こ", "divisor_phrase": ph}
                       for p, ph in zip(problems, ("4こずつ くばります", "4こずつ 買います", "1人に 4つ くばると"))], EXPR)
mid = d.mid_message("tobun", EXPR, 3, "クッキー", "こ")


def ctx(strength, target=None, last_turn=None):
    return {"strength": strength, "target": target, "problems": problems, "last_problem": last, "last_turn": last_turn}


def turn(child, ai):
    return {"child": child, "ai": ai, "input_type": "sakumon", "response_type": "praise", "valid": True}


cases = [
    ("強度0/わからない（産出なし）", "わからない", [], {"strength": 0}, []),
    ("強度0/称賛への「どういうことですか？」", "求めるものがちがうってどういうことですか？", ["hougan"],
     ctx(0, last_turn=turn(P3, praise)), [{"child": P3, "ai": praise}]),
    ("強度0/何を求めればいいの？（行き先は言わない）", "何を求めればいいの？", ["hougan"],
     ctx(0, last_turn=turn(P3, praise)), [{"child": P3, "ai": praise}]),
    ("強度1/弱のあと「わかんない」（行き先・くらべる を言わない）", "わかんない", ["hougan"],
     ctx(1, last_turn={"child": "わかりません", "ai": d.DECLARATION_UNKNOWN_MESSAGE, "input_type": "declaration"}),
     [{"child": P3, "ai": weak}, {"child": "わかりません", "ai": d.DECLARATION_UNKNOWN_MESSAGE}]),
    ("強度1/求めるものって何？（LLM 版）", "もとめるものってなに？", ["hougan"], ctx(1), [{"child": P3, "ai": weak}]),
    ("強度1/2つはちがう？", "2ばんと3ばんはちがうお話だよ？", ["hougan"], ctx(1), [{"child": P3, "ai": weak}]),
    ("強度2/中のあと「どういうことですか？」（問いの文は書かない）", "どういうことですか？", ["hougan"],
     ctx(2, "tobun", last_turn={"child": "わかんない", "ai": mid, "input_type": "taiwa"}), [{"child": "わかんない", "ai": mid}]),
    ("強度2/答え教えて", "こたえ おしえて", ["hougan"], ctx(2, "tobun"), [{"child": "わかんない", "ai": mid}]),
    ("強度3/ヒント（目標 等分除）", "ヒント ちょうだい", ["hougan"], ctx(3, "tobun"), [{"child": "わかんない", "ai": mid}]),
]

print("弱:", weak, "\n中:", mid, "\n")
for name, msg, hist, c, rec in cases:
    out = d._llm_message(msg, "taiwa", None, hist, rec, "talk", EXPR, context=c)
    fb = "（定型文にフォールバック）" if out["state"].endswith("_fallback") else ""
    print(f"[{name}] help={out.get('is_help_request')} {fb}\n  → {out['message']}\n")
