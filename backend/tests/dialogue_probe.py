"""対話AI（form / praise / talk）の実出力と境界ガードの確認。実際に API を呼ぶ。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_dialogue as d

EXPR = "24 ÷ 4"
recent = [{"child": "おりがみが24まいあります。4人で分けると1人分は何まいですか", "ai": "いいね！おりがみのお話ができたね！"}]

cases = [
    ("form/scene_contradiction", "24人が4人のグループに分かれます。1グループ何人ですか", "sakumon",
     {"valid": False, "issue": "scene_contradiction", "structure": "invalid", "unknown": None}, ["tobun"], "form"),
    ("form/no_question", "あめが24こあります。4人にわけます。", "sakumon",
     {"valid": False, "issue": "no_question", "structure": "invalid", "unknown": None}, [], "form"),
    ("form/wrong_number", "あめが20こあります。4人でわけると1人何こですか。", "sakumon",
     {"valid": False, "issue": "wrong_number", "structure": "invalid", "unknown": None}, ["tobun"], "form"),
    ("praise/tobun", "おりがみが24まいあります。4人で分けると1人分は何まいですか", "sakumon",
     {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None, "is_new": True}, [], "praise"),
    ("praise/bai", "赤いリボンは24cm、青いリボンは4cm。赤は青の何倍？", "sakumon",
     {"valid": True, "structure": "bai", "unknown": "ratio", "issue": None, "is_new": True}, ["tobun", "hougan"], "praise"),
    ("talk/わからない（産出なし）", "わからない", "taiwa", None, [], "talk"),
    ("talk/わからない（産出あり）", "わからない", "taiwa", None, ["tobun"], "talk"),
    ("talk/さがし方", "どんなさがし方があるの？おしえて", "taiwa", None, ["tobun"], "talk"),
    ("talk/あいさつ", "こんにちは", "taiwa", None, [], "talk"),
]

for name, msg, kind, jr, hist, rtype in cases:
    out = d._llm_message(msg, kind, jr, hist, recent, rtype, EXPR)
    fb = "（定型文にフォールバック）" if out["state"].endswith("_fallback") else ""
    print(f"[{name}] {fb}\n  → {out['message']}\n")
