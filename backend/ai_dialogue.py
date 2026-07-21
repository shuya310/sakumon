"""児童の作問を支援する対話AI。

児童向けの声かけ（称賛・気づかせ・成立不備の問いかけ・クリア）と、図の指示
（figure）・次に作ってほしい構造（target_structure）をすべてここで生成する。
構造同定は ai_judge が行い、その結果を受け取る（ここでは判定しない）。
出力はJSONのみ。パース失敗時は1回リトライし、それでも失敗したら児童向け
フォールバック文言を返す。
"""

import json
import os
from dotenv import load_dotenv
import anthropic

from kanji_rule import KANJI_RULE

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-5"

FALLBACK_MESSAGE = "もういちど おくってみてね"

STRUCTURE_JP = {
    "tobun": "等分除（分ける話・1人分をさがす）",
    "hougan": "包含除（分ける話・何人分をさがす）",
    "bai": "倍（くらべる話）",
}

_RAW_PROMPT = """あなたは小学4年生が「わり算のお話づくり（作問）」をするのを助ける先生です。
子どもが同じ式（例：18÷3）から、いろいろな種類のお話を作れるように導きます。

# あなたのゴール
子どもに「数量関係の構造的理解」を深めさせること。
具体的には、同じ式でも【分ける話（等分除）】【分ける話（包含除）】
【くらべる話（倍）】という3つの違う構造があることに気づかせ、
まだ作っていない構造のお話を、子ども自身の力で作れるように促すこと。

# 3つの構造
- 等分除：全体を何人かに同じ数ずつ分けて「1人分」をさがす（□×3=18の□）
- 包含除：全体を何こずつかに分けて「何人分」をさがす（3×□=18の□）
- 倍　　：2つの大きさをくらべて「何倍」かをさがす（くらべる話）

# あなたが受け取る情報
- 子どもの発話
- この発話が「作問」か「対話」かの区別
- 直近の作問の判定結果（成立しているか／どの構造か／向きが正しいか）
- これまでに作れた構造のリスト（信号機の点灯状態）
- 直近のやりとりの履歴

# あなたのやること：まず子どもの「今の状態」を読み取る
下の表で今どの状態かを判断し、それに合った指導を1つだけ返します。
表は例であり、状況に応じて総合的に判断してください。

| 子どもの状態 | 何が起きているか | 指導の方向 |
| お話が成立していない | 文章題の形になっていない | 足りない要素を1つ問いかける |
| 同じ構造をくり返している | まだ多義性に気づけていない（停滞） | まだ作っていない構造へ「見方」を向ける |
| 「同じ話じゃないの？」等の質問 | 題材で区別し、構造で区別できていない | 題材ではなく「さがし方」で見ることを示す |
| 向きが逆（倍の逆立式など） | 構造は合うが基準量と比較量を取りちがえている | どちらをもとにするかに気づかせる |
| 新しい構造ができた | 多義性の把握が一歩進んだ | その構造を言葉で意味づけ、次の穴を示す |
| 3つの構造ができた | ゴールに到達 | 3つとも答えは同じでも「さがすもの」が違うことを確認 |

# 図を出す判断（figureフィールド）
支援は【言葉が主役・図は補助】。基本は短い言葉で導く。
次のときだけ figure に構造名を入れる（フロントが描画する）。
- 新しい構造ができたとき：その構造（"tobun"/"hougan"/"bai"）
- 同じ構造で停滞しているとき：まだ作っていない構造をひとつ
- 「同じ話じゃないの？」への回答：今の構造の図
- 3つ達成："all"
- 子どもが「図で見たい」「よくわからない」を2回以上くり返したとき：
  今みちびいている構造の図を必ず出す
お話が成立していないときは figure は null。

# 文字づかいのルール
{KANJI_RULE}

# その他のルール（厳守）
- 1〜2文で短く。長い説明はしない。
- 答え（数や、完成した問題文そのもの）は絶対に教えない。
- 「だれが・なにを・なんこ・どうする」のような穴うめの型を与えない。
- やさしく、はげます口調。

# 出力（JSONのみ）
{
  "message": "子どもへの声かけ（1〜2文）",
  "figure": "tobun" | "hougan" | "bai" | "all" | null,
  "target_structure": "次に作ってほしい構造（tobun/hougan/bai）または null",
  "state": "読み取った子どもの状態（ログ用）"
}"""

SYSTEM_PROMPT = _RAW_PROMPT.replace("{KANJI_RULE}", KANJI_RULE)


def _history_labels(history: list[str]) -> str:
    if not history:
        return "まだ何も作れていない"
    return "・".join(STRUCTURE_JP.get(h, h) for h in history)


def _build_situation(input_kind: str, jr: dict | None) -> str:
    """直近の作問の判定結果を、モデルが読める説明文にする。"""
    if input_kind == "taiwa" or not jr:
        return "これは対話（質問・つぶやき・こまった等）です。新しい作問ではありません。"
    if not jr.get("valid"):
        issue = jr.get("issue")
        if issue == "reversed":
            return "作問したが、倍の向きが逆（基準量と比較量を取りちがえている）。式が18÷3にならない。"
        if issue == "wrong_number":
            return "作問したが、式が18÷3にならない（数値や演算がちがう）。"
        return "作問したが、文章題として成立していない（要素が欠けている・意味が読めない）。"
    label = STRUCTURE_JP.get(jr.get("structure"), jr.get("structure"))
    if jr.get("completes_all"):
        return f"作問成立。構造は{label}。これで3つの構造がすべてそろった（ゴール到達）。"
    if jr.get("is_new"):
        return f"作問成立。構造は{label}。これは新しく作れた構造。"
    return f"作問成立。構造は{label}。ただしこの構造はすでに作ったことがある（同じ構造のくり返し・停滞）。"


def _build_history(recent_turns: list[dict] | None) -> str:
    if not recent_turns:
        return "（まだやりとりがない）"
    lines = []
    for t in recent_turns:
        if t.get("child"):
            lines.append(f"子ども: {t['child']}")
        if t.get("ai"):
            lines.append(f"先生: {t['ai']}")
    return "\n".join(lines) if lines else "（まだやりとりがない）"


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None) -> dict:
    """児童向けの声かけを生成する。

    戻り値: {"message", "figure", "target_structure", "state"}
    パース失敗が続いた場合は message にフォールバック文言を入れて返す。
    """
    user_content = f"""子どもの発話: {child_message}
この発話の区別: {"作問" if input_kind == "sakumon" else "対話"}

直近の作問の判定結果:
{_build_situation(input_kind, judge_result)}

これまでに作れた構造（信号機の点灯状態）: {_history_labels(history)}

直近のやりとりの履歴:
{_build_history(recent_turns)}"""

    for attempt in range(2):  # 1回リトライ
        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            result = _parse(response.content[0].text)
            message = result.get("message")
            if message:
                figure = result.get("figure")
                if figure not in ("tobun", "hougan", "bai", "all"):
                    figure = None
                target = result.get("target_structure")
                if target not in ("tobun", "hougan", "bai"):
                    target = None
                return {
                    "message": message,
                    "figure": figure,
                    "target_structure": target,
                    "state": result.get("state"),
                }
        except Exception as e:
            print(f"[ai_dialogue] dialogue failed (attempt {attempt + 1}): {type(e).__name__}: {e}")

    return {"message": FALLBACK_MESSAGE, "figure": None, "target_structure": None, "state": "fallback"}
