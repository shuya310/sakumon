"""ai_judge の精度確認（第3部A・B）。実際に Anthropic API を呼ぶ。

実行: cd backend && ./venv/bin/python tests/judge_cases.py
式は 24 ÷ 4。expected は (valid, structure, unknown, issue)。
"許容" は研究上どちらでも問題ない境界事例（一致すれば✓、許容側なら△）。
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_judge  # noqa: E402

EXPR = "24 ÷ 4"

# (group, text, expected(valid, structure, unknown, issue), acceptable_alternatives)
CASES = [
    # --- 倍：倍率 ---
    ("倍・倍率", "赤いリボンは24cm、青いリボンは4cmです。赤いリボンは青いリボンの何倍ですか。", (True, "bai", "ratio", None), []),
    ("倍・倍率", "ボールが24こ、バットが4本あります。ボールの数はバットの数の何倍ですか。", (True, "bai", "ratio", None), []),
    ("倍・倍率", "24mのロープは4mのロープの何倍の長さですか。", (True, "bai", "ratio", None), []),
    ("倍・倍率", "お兄さんは24さい、弟は4さいです。お兄さんの年は弟の年の何倍ですか。", (True, "bai", "ratio", None), []),
    # --- 倍：基準量（必ず成立・bai・base）---
    ("倍・基準量", "白い花の4ばいが赤い花24本。白い花は何本？", (True, "bai", "base", None), []),
    ("倍・基準量", "白い花の4倍が赤い花で、赤い花は24本です。白い花は何本ですか。", (True, "bai", "base", None), []),
    ("倍・基準量", "ゆうきさんはシールを24まい持っています。これは妹の4倍です。妹は何まい持っていますか。", (True, "bai", "base", None), []),
    ("倍・基準量", "テープを4倍にのばすと24cmになりました。もとのテープは何cmですか。", (True, "bai", "base", None), []),
    ("倍・基準量", "赤いリボンの長さは24cmで、青いリボンの4倍です。青いリボンは何cmですか。", (True, "bai", "base", None), []),
    # --- 倍：割合 ---
    ("倍・割合", "24このりんごを4人に配ります。1人に対して何この割合ですか。", (True, "bai", "rate", None), [(True, "tobun", "one_unit", None)]),
    ("倍・割合", "24人の中で、4人がめがねをかけています。めがねをかけている人は何人に1人の割合ですか。", (True, "bai", "rate", None), []),
    ("倍・割合(仕様例)", "24人を4つに分けると、1つあたり何人の割合？", (True, "bai", "rate", None), [(True, "tobun", "one_unit", None)]),
    # --- 包含除と倍の境界 ---
    ("包含除", "あめが24こあります。1人に4こずつ配ると、何人に配れますか。", (True, "hougan", "num_units", None), []),
    ("包含除", "24このあめを4こずつふくろに入れます。ふくろは何まい必要ですか。", (True, "hougan", "num_units", None), []),
    ("包含除", "24mのロープを4mずつ切ります。何本できますか。", (True, "hougan", "num_units", None), []),
    ("包含除", "24人の子どもが4人ずつのグループに分かれます。グループはいくつできますか。", (True, "hougan", "num_units", None), []),
    ("包含除", "24ページの本を1日に4ページずつ読みます。何日で読み終わりますか。", (True, "hougan", "num_units", None), []),
    # --- 等分除 ---
    ("等分除", "おりがみが24まいあります。4人で同じ数ずつ分けると、1人分は何まいですか。", (True, "tobun", "one_unit", None), []),
    ("等分除", "24Lのジュースを4つのコップに同じ量ずつ入れます。1つのコップに何L入りますか。", (True, "tobun", "one_unit", None), []),
    ("等分除", "24人の子どもを4つの班に同じ人数ずつ分けます。1班は何人ですか。", (True, "tobun", "one_unit", None), []),
    ("等分除", "24cmのテープを4等分します。1本は何cmですか。", (True, "tobun", "one_unit", None), []),
    # --- 表記の不備に寛容 ---
    ("表記寛容", "あめが24こあります4人でわけると1人なんこになりますか", (True, "tobun", "one_unit", None), []),
    ("表記寛容", "おりがみが24まいあります。4にんでおなじかずずつわけると、ひとりぶんはなんまいですか。", (True, "tobun", "one_unit", None), []),
    ("表記寛容", "クッキーが24こあります。4人にくばると、1人何本ですか。", (True, "tobun", "one_unit", None), []),
    # --- 逆立式 → wrong_number ---
    ("逆立式", "4こは24この何倍ですか。", (False, "invalid", None, "wrong_number"), []),
    ("逆立式", "青いリボンは4cm、赤いリボンは24cmです。青いリボンは赤いリボンの何倍ですか。", (False, "invalid", None, "wrong_number"), []),
    # --- 場面矛盾（9/1実データ）---
    ("場面矛盾", "24人が4人のグループに分かれます。そうすると1グループ何人ですか。", (False, "invalid", None, "scene_contradiction"), []),
    ("場面矛盾", "4人あわせて100まもっています。Aくんは24まいもっています。20人います。1人4まいだと何人にわたしますか?", (False, "invalid", None, "scene_contradiction"), []),
    ("場面矛盾", "24このあめを1人に4こずつ配ります。1人何こもらえますか。", (False, "invalid", None, "scene_contradiction"), []),
    # --- その他の不成立 ---
    ("問いなし", "あめが24こあります。4人にわけます。", (False, "invalid", None, "no_question"), []),
    ("途中切れ", "あめが24こあります。4人でわけると", (False, "invalid", None, "incomplete_text"), [(False, "invalid", None, "no_question")]),
    ("演算ちがい", "あめが24こあります。4こもらいました。全部で何こですか。", (False, "invalid", None, "wrong_operation"), []),
    ("数ちがい", "あめが20こあります。4人でわけると1人何こですか。", (False, "invalid", None, "wrong_number"), []),
    ("文章題でない", "わりざん たのしい", (False, "invalid", None, "not_problem"), []),
    ("文章題でない", "24÷4=6", (False, "invalid", None, "not_problem"), [(False, "invalid", None, "no_question")]),
]


def run(case):
    group, text, expected, alts = case
    r = ai_judge.judge(text, EXPR)
    got = (r["valid"], r["structure"], r["unknown"], r["issue"])
    mark = "✓" if got == expected else ("△" if got in alts else "✗")
    return group, text, expected, got, mark


def fmt(t):
    v, s, u, i = t
    return f"{'成立' if v else '不成立'}/{s}/{u or '-'}/{i or '-'}"


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(run, CASES))
    ok = sum(1 for r in results if r[4] == "✓")
    acc = sum(1 for r in results if r[4] == "△")
    print(f"| # | 分類 | 入力 | 期待 | 判定 | |")
    print(f"|---|---|---|---|---|---|")
    for n, (group, text, expected, got, mark) in enumerate(results, 1):
        print(f"| {n} | {group} | {text} | {fmt(expected)} | {fmt(got)} | {mark} |")
    print(f"\n一致 {ok} / 許容 {acc} / 不一致 {len(results) - ok - acc}  （全 {len(results)} 件）")
