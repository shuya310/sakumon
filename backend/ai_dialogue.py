"""児童の作問を支援する対話AI。

児童向けの声かけ（称賛・気づかせ・成立不備の問いかけ・クリア）と、図の指示
（figure）・次に作ってほしい構造（target_structure）をすべてここで生成する。
構造同定は ai_judge が行い、その結果を受け取る（ここでは判定しない）。

支援は「段階的支援」。新しい構造ができた直後は次の構造を名指しせず問いかけで
気づかせ、停滞や「わからない」で初めて具体的な見方を示す。段階は main.py が
決定論的に決め、support_level として渡す。

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

FALLBACK_MESSAGE = "もう一度 おくってみてね"

STRUCTURE_JP = {
    "tobun": "等分除（分ける話・1人分をさがす）",
    "hougan": "包含除（分ける話・何人分をさがす）",
    "bai": "倍（くらべる話）",
}

# main.py が決める支援の段階。プロンプトはこの値で声かけを変える。
SUPPORT_LEVEL_JP = {
    "form": "form（お話が成立していない：足りない要素を1つだけ問いかける）",
    "discover": "discover（新しい構造ができた：称賛＋意味づけ。★次の構造は名指しせず、問いかけで気づかせる）",
    "concrete": "concrete（同じ構造がつづく／わからない：ここで初めて、まだ作っていない構造への見方を具体的に示してよい）",
    "goal": "goal（3つの構造ができた：さがすものの違いを確認する）",
    "talk": "talk（その他の質問・つぶやき：やさしく短く受け止め、作問にもどれるようにうながす）",
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
- いまの「支援の段階」（下記のどれか）

# 支援の段階（最重要・これに従って声かけを変える）
- discover（新しい構造ができた）：まず称賛し、「何をさがしたか」を短い言葉で意味づける。
  ★ここでは、次に作る構造を絶対に名指ししない。答えの方向を渡さない。
  かわりに「ほかにどんなさがし方があるかな？」のように問いかけ、子ども自身に気づかせる。
- concrete（同じ構造がつづく／「わからない・図で見たい」）：ここで初めて、
  まだ作っていない構造への「見方（さがし方）」を具体的に示してよい。
  題材（りんご→えんぴつ 等）を変えることではなく、“何をさがすか”の違いに目を向けさせる。
- form（お話が成立していない）：足りない要素を1つだけ問いかける。
- goal（3つの構造ができた）：3つとも答えは同じでも「さがすもの」が違うことを確認する。
- talk（その他の質問・つぶやき）：やさしく短く受け止め、作問にもどれるよう軽くうながす。

# 「同じ話じゃないの？」と言われたとき
題材で区別しようとして、構造で区別できていないサイン。
題材ではなく「何をさがしているか（さがし方）」で見ることを、今の構造の図とともに示す。

# 向きが逆（倍の逆立式など）のとき
構造は合うが基準量と比較量を取りちがえている。どちらをもとにするかに気づかせる。

# 図を出す判断（figureフィールド）
支援は【言葉が主役・図は補助】。基本は短い言葉で導く。次のときだけ figure に構造名を入れる。
- discover：今できた構造（"tobun"/"hougan"/"bai"）を入れる。
- concrete：まだ作っていない構造をひとつ入れる。ただし「同じ話じゃないの？」への回答なら“今の構造”。
- goal："all"。
- form / talk：基本は null。ただし「図で見たい」「よくわからない」が2回以上つづいたら、
  今みちびいている構造を必ず入れる。

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


def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             support_level: str = "talk") -> dict:
    """児童向けの声かけを生成する。

    support_level は main.py が決める支援の段階（form/discover/concrete/goal/talk）。
    戻り値: {"message", "figure", "target_structure", "state"}
    パース失敗が続いた場合は message にフォールバック文言を入れて返す。
    """
    user_content = f"""子どもの発話: {child_message}
この発話の区別: {"作問" if input_kind == "sakumon" else "対話"}
いまの支援の段階: {SUPPORT_LEVEL_JP.get(support_level, support_level)}

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
                thinking={"type": "disabled"},  # 短い声かけに思考は不要（sonnet-5は既定でonのため明示off）
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            result = _parse(_text_from(response))
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
