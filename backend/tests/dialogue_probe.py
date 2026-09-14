"""対話AI（talk）の実出力と境界ガード（構造名・求める量の語・語彙・休けい等）の確認。実際に API を呼ぶ。
form / praise / prompt / done は定型文（ai_dialogue の表）なので LLM は呼ばない。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_dialogue as d

EXPR = "24 ÷ 8"
recent = [{"child": "おりがみが24まいあります。8人で分けると1人分は何まいですか", "ai": d.PRAISE_NEW}]

cases = [
    ("talk/わからない（産出なし）", "わからない", []),
    ("talk/わからない（産出あり）", "わからない", ["tobun"]),
    ("talk/もうやだ", "もうやだ つづけられない", ["tobun"]),
    ("talk/答え教えて", "こたえ おしえて", ["tobun"]),
    ("talk/あいさつ", "こんにちは", []),
]

for name, msg, hist in cases:
    out = d._llm_message(msg, "taiwa", None, hist, recent, "talk", EXPR)
    fb = "（定型文にフォールバック）" if out["state"].endswith("_fallback") else ""
    print(f"[{name}] {fb}\n  → {out['message']}\n")
