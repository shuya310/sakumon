"""作問文の構造同定。

責務は「作問された文章の成立性・構造・求める量の同定」に限る。児童向けメッセージ・
表示種別・信号機の状態は生成しない（それらは main.py が決定論的に計算し、
児童向けの声かけは ai_dialogue が生成する）。

判定は3層（成立性 → 構造 → 求める量と整合チェック）をプロンプト内で明示的に分ける。
出力はJSONのみ。パース失敗時は1回リトライし、それでも失敗したら issue="error" を返す。

戻り値:
  {"valid": bool,
   "structure": "tobun" | "hougan" | "bai" | "invalid",
   "unknown": "one_unit" | "num_units" | "ratio" | "base" | "rate" | None,
   "issue": None | "scene_contradiction" | "wrong_number" | "incomplete_text"
            | "wrong_operation" | "no_question" | "not_problem"}
"""

import json
import os

import anthropic

from config import MODEL, parse_expression

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ALL_STRUCTURES = ("tobun", "hougan", "bai")
UNKNOWNS = ("one_unit", "num_units", "ratio", "base", "rate")
ISSUES = ("scene_contradiction", "wrong_number", "incomplete_text",
          "wrong_operation", "no_question", "not_problem")

# structure と unknown の整合表。矛盾したら structure を優先し、先頭の unknown に補正する。
UNKNOWN_FOR_STRUCTURE = {
    "tobun": ("one_unit",),
    "hougan": ("num_units",),
    "bai": ("ratio", "base", "rate"),
}

# 旧コードの不成立コードを新コードへ写す（"reversed" は廃止。逆立式は wrong_number）
_LEGACY_ISSUE = {"reversed": "wrong_number", "error": "error"}

SYSTEM_PROMPT = """あなたは小学4年生の算数文章題を分析する判定器です。
児童が入力した文章題について、式 {expression} で解ける文章題として成立しているか、
成立しているならどの構造か、何を求めているか、を判定します。
児童向けのメッセージやヒントは書きません。判定結果のJSONだけを返してください。
JSON以外の文字は一切出力しないでください。

## 評価する式
{expression}（被除数 {dividend}、除数 {divisor}、商 {quotient}）

## 判定の3層（この順に考える）

### 第1層：成立性（valid）
次の3つをすべて満たすとき valid=true。
1. 数量を含む場面が書かれている
2. 求める量を問う文がある（疑問文でも命令文でもよい。「〜ですか」「〜は？」「〜を求めなさい」）
   ★問いの文が書かれていなければ、場面から何を求めるか推測できても valid=false, issue="no_question"。
   例「あめが{dividend}こあります。{divisor}人にわけます。」→ 問いがないので no_question（1人分を問うと補ってはいけない）。
3. その場面と問いが、式 {expression} で解ける（答えが {quotient} になる）
1つでも満たさなければ valid=false とし、issue に理由コードを入れる。

### 第2層：構造（structure）── 判別点は「除数 {divisor} が場面の中で何を指しているか」
- 除数が「分割数」（いくつに分けるか：{divisor}人に、{divisor}つの班に、{divisor}等分に）
  → 被除数と除数は異種（まい÷人、L÷人、cm÷等分数） → structure="tobun"（等分除）。求めるのは「1つ分の大きさ」。
- 除数が「1つ分の大きさ」（{divisor}こずつ、{divisor}人ずつ、1ふくろに{divisor}こ、1回に{divisor}まい）
  → 被除数と除数は同種（こ÷こ） → structure="hougan"（包含除）。求めるのは「いくつ分」。
- 除数が「比べる相手の量（基準量）」または「倍率（{divisor}倍）」
  → 被除数と除数は同種（人÷人、本÷本、cm÷cm） → structure="bai"（倍）。
- 包含除と倍の分かれ目：除数が「まとめる単位（〜ずつ）」なら包含除、
  「比べる相手の量（〜は…の何倍）」や「倍率（〜の{divisor}倍）」なら倍。

倍は「乗法的比較の場面」全体を指す。求める量は3通りあり、どれも valid=true, structure="bai"。
- 倍率を問う：「{dividend}このボールは{divisor}このボールの何倍ですか」→ unknown="ratio"
- 基準量を問う：「白い花の{divisor}倍が赤い花で{dividend}本です。白い花は何本ですか」（□×{divisor}={dividend}）→ unknown="base"
  ★この形（{divisor}倍した結果が{dividend}で、もとの量を問う）は必ず valid=true, structure="bai", unknown="base" とする。
- 割合を問う：2つの量の関係を割合として問う（「〜に対して」「〜あたり」「〜に1つの割合」）→ unknown="rate"
  ただし、単に全体を等しく分けて1つ分を問うだけの文（「{dividend}人を{divisor}つの班に分けると1班は何人」）は
  tobun であって rate ではない。rate は「割合」「〜に対して」など比べる関係が文中に明示されている場合に限る。
- 倍率を問う文「AはBの何倍ですか」では、A（〜は）が比較量、B（〜の）が基準量で、式は A÷B。
  A÷B が {expression} になるときだけ成立。A={divisor}, B={dividend} の向き
  （例「青いリボンは{divisor}cm、赤いリボンは{dividend}cmです。青いリボンは赤いリボンの何倍ですか」＝{divisor}÷{dividend}）は
  逆立式なので valid=false, issue="wrong_number"。数値が両方そろっていても、向きが逆なら成立にしない。
- 短い逆立式（「{divisor}こは{dividend}この何倍ですか」）も同じく wrong_number。

### 第3層：求める量（unknown）と整合チェック
unknown を次の5つから選ぶ。
- "one_unit"：1つ分の大きさ（1人分・1つあたり・1本の長さ）→ tobun
- "num_units"：いくつ分（何人に配れる・何ふくろ・何回・何グループ）→ hougan
- "ratio"：倍率（何倍）→ bai
- "base"：基準量（もとの大きさ・もとにする量）→ bai
- "rate"：割合 → bai
structure と unknown が矛盾したら第2層に戻って判定し直す。valid=false のとき unknown は null。

## issue（valid=false のときの理由コード。valid=true のときは null）
- "scene_contradiction"：場面矛盾。問いの答えが、場面の中ですでに与えられている、または
  場面と問いの向きがずれている。次の3つの判定則のどれかにあたるもの。
  1. 分割数が確定しているのに分割数を問う
     例「{dividend}人が{divisor}人のグループに分かれます。1グループ何人ですか」
     →「{divisor}人のグループ」で1グループの人数は確定済み。{expression} が求めるのはグループ数であり、問いと対応しない。
  2. 1つ分が確定しているのに1つ分を問う
     例「{dividend}このあめを1人{divisor}こずつ配ります。1人何こもらえますか」
  3. 被除数の指す量を取りちがえていて、割る対象の総数が場面にない
     例：配る相手の人数が別に書かれていて問いと合わない、{dividend}が何の総数なのか場面と食いちがう
  要素は揃っていて文章としては読めるが、場面と問いがかみ合っていない状態。
  「意味が読めない・要素が欠けている」（not_problem）とは別物として扱う。
- "wrong_number"：文章題としては成立するが、式が {expression} にならない
  （使う数値がちがう、逆立式 {divisor}÷{dividend} になる、答えが {quotient} にならない）
- "wrong_operation"：わり算では解けない（かけ算・たし算・ひき算の問題になっている）
- "incomplete_text"：文が途中で切れていて、求める量を特定できない
- "no_question"：場面だけで、何を求めるかが書かれていない
- "not_problem"：場面の記述自体がなく、文章題として成立しない（単語の羅列・意味不明・作問ではない文）

## 判定の原則
- 表記の不備には寛容に、論理と式の一致には厳格に。
- 誤字・脱字・助詞の誤り・助数詞の不一致・ひらがな表記・句読点の欠けは不問。文の意図が読めるなら成立性を否定しない。
- 書かれていない場面を補って解釈しない。数値やその意味を変える補完もしない
  （「{divisor}人のグループ」を「{divisor}つのグループ」と読み替えるのは不可）。
- 「1人分」「1つ分」「1こあたり」「1本は何cm」など1つ分を問う言い方はすべて one_unit。
- 「同じ数ずつ分ける」は等分の説明であり、分ける相手の数（{divisor}人）が書かれていれば tobun。

## 返すJSON（この形式のみ）
{
  "valid": true or false,
  "structure": "tobun" or "hougan" or "bai" or "invalid",
  "unknown": "one_unit" or "num_units" or "ratio" or "base" or "rate" or null,
  "issue": null or "scene_contradiction" or "wrong_number" or "incomplete_text" or "wrong_operation" or "no_question" or "not_problem"
}"""


def build_system_prompt(expression: str) -> str:
    dividend, divisor = parse_expression(expression)
    return (SYSTEM_PROMPT
            .replace("{expression}", f"{dividend} ÷ {divisor}")
            .replace("{dividend}", str(dividend))
            .replace("{divisor}", str(divisor))
            .replace("{quotient}", str(dividend // divisor)))


def _text_from(response) -> str:
    """応答から最初の text ブロックを取り出す（思考ブロック混入への保険）。"""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("no text block in response")


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def normalize(result: dict) -> dict:
    """LLMの生JSONを仕様の形に正規化する（structure と unknown の整合をコード側で強制）。"""
    valid = bool(result.get("valid"))
    structure = result.get("structure")
    unknown = result.get("unknown")
    issue = result.get("issue")
    if valid and structure in ALL_STRUCTURES:
        allowed = UNKNOWN_FOR_STRUCTURE[structure]
        if unknown not in allowed:
            unknown = allowed[0]
        return {"valid": True, "structure": structure, "unknown": unknown, "issue": None}
    issue = _LEGACY_ISSUE.get(issue, issue)
    if issue not in ISSUES:
        issue = "not_problem"
    return {"valid": False, "structure": "invalid", "unknown": None, "issue": issue}


def judge(message: str, expression: str) -> dict:
    """作問文の成立性・構造・求める量を同定する。

    パース失敗が続いた場合は {"valid": False, "structure": "invalid", "unknown": None,
    "issue": "error", "error": ...} を返す（児童の責任ではない技術的失敗）。
    """
    system = build_system_prompt(expression)
    user_content = f"式: {expression}\n児童の入力: {message}"

    last_err = None
    for attempt in range(2):  # 1回リトライ
        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=256,
                thinking={"type": "disabled"},  # sonnet-5 は既定でonのため明示off
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            return normalize(_parse(_text_from(response)))
        except Exception as e:
            last_err = e
            print(f"[ai_judge] judge failed (attempt {attempt + 1}): {type(e).__name__}: {e}")

    return {"valid": False, "structure": "invalid", "unknown": None, "issue": "error", "error": str(last_err)}
