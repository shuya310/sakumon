"""対話AI（form / discover / level4 / talk）の実出力と境界ガードの確認。実際に API を呼ぶ。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_dialogue as d

EXPR = "24 ÷ 4"
recent = [{"child": "おりがみが24まいあります。4人で分けると1人分は何まいですか", "ai": "いいね！おりがみのお話ができたね！"}]

cases = [
    ("form/scene_contradiction", "24人が4人のグループに分かれます。1グループ何人ですか", "sakumon",
     {"valid": False, "issue": "scene_contradiction", "structure": "invalid", "unknown": None}, ["tobun"], "form", None),
    ("form/no_question", "あめが24こあります。4人にわけます。", "sakumon",
     {"valid": False, "issue": "no_question", "structure": "invalid", "unknown": None}, [], "form", None),
    ("form/wrong_number", "あめが20こあります。4人でわけると1人何こですか。", "sakumon",
     {"valid": False, "issue": "wrong_number", "structure": "invalid", "unknown": None}, ["tobun"], "form", None),
    ("discover/tobun", "おりがみが24まいあります。4人で分けると1人分は何まいですか", "sakumon",
     {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None, "is_new": True}, [], "discover", None),
    ("discover/bai", "赤いリボンは24cm、青いリボンは4cm。赤は青の何倍？", "sakumon",
     {"valid": True, "structure": "bai", "unknown": "ratio", "issue": None, "is_new": True}, ["tobun", "hougan"], "discover", None),
    ("level4/target=bai", "ジュースが24Lあります。4人で分けると1人何Lですか", "sakumon",
     {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None, "is_new": False}, ["tobun", "hougan"], "level4", "bai"),
    ("level4/target=hougan", "みかんが24こあります。4人で分けると1人何こですか", "sakumon",
     {"valid": True, "structure": "tobun", "unknown": "one_unit", "issue": None, "is_new": False}, ["tobun"], "level4", "hougan"),
    ("talk/わからない", "わからない", "taiwa", None, ["tobun"], "talk", None),
    ("talk/さがし方", "どんなさがし方があるの？おしえて", "taiwa", None, ["tobun"], "talk", None),
    ("talk/あいさつ", "こんにちは", "taiwa", None, [], "talk", None),
]

for name, msg, kind, jr, hist, level, target in cases:
    out = d._llm_message(msg, kind, jr, hist, recent, level, EXPR, target)
    fb = "（定型文にフォールバック）" if out["state"].endswith("_fallback") else ""
    print(f"[{name}] {fb}\n  → {out['message']}\n")
