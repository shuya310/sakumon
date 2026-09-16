"""対話AI（talk）の実出力と境界ガード（構造名・答え・語彙・休けい等）の確認。実際に API を呼ぶ。
form / praise / prompt / done は定型文（ai_dialogue の表）なので LLM は呼ばない。
実行: cd backend && ./venv/bin/python tests/dialogue_probe.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_dialogue as d

EXPR = "24 ÷ 8"
P1 = "おりがみが24まいあります。8人で分けると1人分は何まいですか"
P2 = "あめが24こあります。8人に同じ数ずつ配ります。1人何こですか"
recent = [{"child": P2, "ai": d.PRAISE_REPEAT}]
role = d.divisor_role_label("tobun", "one_unit", 8)
problems = [{"text": P1, "divisor_role": role}, {"text": P2, "divisor_role": role}]
last = {"text": P2, "structure": "tobun", "unknown": "one_unit", "divisor_role": role, "item": "あめ", "unit": "こ"}


def ctx(strength, target=None):
    return {"strength": strength, "target": target, "problems": problems, "last_problem": last}


cases = [
    ("強度0/わからない（産出なし）", "わからない", [], {"strength": 0}, []),
    ("強度0/求めているものって何？", "もとめているものってなに？", ["tobun"], ctx(0), recent),
    ("強度1/8ってなんの数？", "8ってなんのかず？", ["tobun"], ctx(1), recent),
    ("強度1/同じじゃない？", "2つのもんだい ちがうやつ 作ったよね？", ["tobun"], ctx(1), recent),
    ("強度2/8ってなんの数？（目標いくつ分）", "8ってなんのかず？", ["tobun"], ctx(2, "hougan"), recent),
    ("強度2/もうやだ", "もうやだ つづけられない", ["tobun"], ctx(2, "hougan"), recent),
    ("強度3/ヒント（目標いくつ分）", "ヒント ちょうだい", ["tobun"], ctx(3, "hougan"), recent),
    ("強度2/答え教えて", "こたえ おしえて", ["tobun"], ctx(2, "hougan"), recent),
]

for name, msg, hist, c, rec in cases:
    out = d._llm_message(msg, "taiwa", None, hist, rec, "talk", EXPR, context=c)
    fb = "（定型文にフォールバック）" if out["state"].endswith("_fallback") else ""
    print(f"[{name}] {fb}\n  → {out['message']}\n")
