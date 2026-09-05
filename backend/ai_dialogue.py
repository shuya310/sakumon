"""児童の作問を支援する対話AI。

児童向けの声かけ（称賛・気づかせ・成立不備の問いかけ・クリア）と、図の指示
（figure）・次に作ってほしい構造（target_structure）をすべてここで生成する。
構造同定は ai_judge が行い、その結果を受け取る（ここでは判定しない）。

支援は「段階的支援」。新しい構造ができた直後は次の構造を名指しせず問いかけで
気づかせ、停滞（同じ構造のくり返し＝stall_count）が続くほどhint1→hint2→hint3と
少しずつ具体的にしていく（hint1:気づきを促す問いかけのみ／hint2:題材ではなく
数量関係で比べるよう“比較の軸”を言葉で転換させる／hint3:まだ作っていない構造の
テープ図を提示する）。段階（stall_countから導いたsupport_level）は main.py が
決定論的に決めて渡す。

hint3は物語文の生成を一切ともなわない安全境界のため、LLMを呼ばずコード側
（_build_tape_diagram）だけで生成する。known/unknownの数値は18÷3固定。

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

STRUCTURE_LABEL_JA = {"tobun": "等分除", "hougan": "包含除", "bai": "倍"}

# 18÷3 固定（main.py の EXPRESSION と対応）。hint3のテープ図はこの2値のみで組み立てる。
DIVIDEND = 18
DIVISOR = 3

TAPE_DIAGRAM_MESSAGE = "このテープ図に合うお話を考えてみよう。"

# main.py が決める支援の段階。プロンプトはこの値で声かけを変える。
SUPPORT_LEVEL_JP = {
    "form": "form（お話が成立していない：足りない要素を1つだけ問いかける）",
    "discover": "discover（新しい構造ができた：称賛＋意味づけ。★次の構造は名指しせず、問いかけで気づかせる）",
    "hint1": "hint1（同じ構造を1回くり返した：図はまだ出さない。前と同じかどうかに気づかせる問いかけのみ）",
    "hint2": "hint2（くり返しが続く、または『同じ話じゃないの？』『わからない』と言われた："
             "図はまだ出さない。かわりに“比べる軸”を転換させる。題材（りんご／えんぴつ等）で"
             "比べるのをやめて、「何をさがしているか（数量関係）」で比べるよう、比較の軸を"
             "言葉ではっきり示す）",
    "hint3": "hint3（hint2でも変化がない／混乱がくり返される：テープ図を提示する段階。"
             "このメッセージ・図はコード側が決定論的に生成するため、通常ここにはこない）",
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
- hint1（同じ構造を1回くり返した）：図はまだ出さない。
  「前のお話と、さがしているものは同じかな？」のように、くり返しに気づかせる
  問いかけだけにとどめる。次の構造の名前や求め方には触れない。
- hint2（くり返しが続く／「同じ話じゃないの？」「わからない」等）：図はまだ出さない。
  かわりに“比べる軸”そのものを転換させる。子どもは題材（りんご→えんぴつ等、話の
  中身）で「違うお話のつもり」になっていることが多い。題材ではなく「何をさがして
  いるか（数量関係）」で比べればさっきと同じだと気づけるよう、比較の軸を
  はっきり言葉で示す（例：「題材は変わったけど、さがしているものは同じかな？
  ちがうかな？」のように、比べる対象を“話の中身”から“さがしているもの”へ
  はっきり切りかえて問いかける）。次に何をさがせばよいかはまだ名指ししない。
- hint3（hint2でも変化がない／混乱がくり返される）：テープ図を提示する段階。
  この段階のメッセージと図はコード側が決定論的に生成するため、あなたが
  呼ばれることは通常ない。万一この段階であなたが呼ばれた場合も、図は出さず、
  hint2と同様に短く問いかけるだけにとどめる。
- form（お話が成立していない）：足りない要素を1つだけ問いかける。
- goal（3つの構造ができた）：3つとも答えは同じでも「さがすもの」が違うことを確認する。
- talk（その他の質問・つぶやき）：やさしく短く受け止め、作問にもどれるよう軽くうながす。
  ★ここで構造の名前や「何をさがすか」を教えてはいけない（それはhint1〜3の役目）。
  「どんな探し方がある？」と聞かれても、具体的には答えず、自分で考えるよう返す。

# 「同じ話じゃないの？」と言われたとき
題材で区別しようとして、構造で区別できていないサイン。support_levelは
通常hint2以上になっている。図は使わず、「何をさがしているか（さがし方）」で見れば
同じだと気づかせる問いかけで、比較の軸を題材から数量関係へ切りかえる。

# 向きが逆（倍の逆立式など）のとき
構造は合うが基準量と比較量を取りちがえている。どちらをもとにするかに気づかせる。

# 図を出す判断（figureフィールド）
支援は【言葉が主役・図は補助】。基本は短い言葉で導く。次のときだけ figure に構造名を入れる。
- discover：今できた構造（"tobun"/"hougan"/"bai"）を入れる。
- hint1：null（図はまだ出さない）。
- hint2：null（図はまだ出さない。この段階は図ではなく“比較の軸”を言葉で転換させる段階）。
- hint3：null（この段階のテープ図はコード側が決定論的に生成する。あなたは出さない）。
- goal："all"。
- form / talk：常に null。図を出すかどうかはmain.pyが決めるhint1〜3にまかせ、
  ここでは絶対に出さない。

# 文字づかいのルール
{KANJI_RULE}

# その他のルール（厳守）
- 1〜2文で短く。長い説明はしない。
- 答え（数や、完成した問題文そのもの）は絶対に教えない。
- 「だれが・なにを・なんこ・どうする」のような穴うめの型を与えない。
- hint3でも、そのまま書き写せば問題文になってしまうような完成した一文は渡さない。
  「何をさがすか」という視点のちがいだけを短く示し、文づくりは子どもに残す。
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


_STRUCTURE_ORDER = ("tobun", "hougan", "bai")


def _pick_unreached_structure(history: list[str]) -> str | None:
    """信号機がまだ点いていない構造をひとつ、決定論的な固定順で選ぶ。"""
    for s in _STRUCTURE_ORDER:
        if s not in history:
            return s
    return None


def _tape_diagram_payload(structure: str) -> dict:
    """hint3で提示するテープ図のJSON。18÷3固定の数値のみで組み立てる。
    物語文は一切含めない（呼び出し側はこの戻り値以外に本文を生成しない）。
    """
    if structure == "tobun":
        known, unknown = {"全体量": DIVIDEND, "いくつ分": DIVISOR}, "1あたり量"
    elif structure == "hougan":
        known, unknown = {"全体量": DIVIDEND, "1あたり量": DIVISOR}, "いくつ分"
    else:  # bai
        known, unknown = {"比較量": DIVIDEND, "基準量": DIVISOR}, "倍"
    return {
        "type": "tape_diagram",
        "structure": STRUCTURE_LABEL_JA[structure],
        "known": known,
        "unknown": unknown,
    }


def _build_tape_diagram(history: list[str]) -> dict | None:
    """hint3の応答をLLMを介さずコード側だけで決定論的に生成する。

    未到達構造が無い（理論上goalで止まるはずの縁）場合は None を返し、
    呼び出し側は通常のLLM対話にフォールバックする。
    """
    structure = _pick_unreached_structure(history)
    if not structure:
        return None
    return {
        "message": TAPE_DIAGRAM_MESSAGE,
        "figure": None,
        "target_structure": structure,
        "state": "hint3_tape_diagram",
        "tape_diagram": _tape_diagram_payload(structure),
    }


def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             support_level: str = "talk") -> dict:
    """児童向けの声かけを生成する。

    support_level は main.py が決める支援の段階（form/discover/hint1/hint2/hint3/goal/talk）。
    戻り値: {"message", "figure", "target_structure", "state", "tape_diagram"}
    パース失敗が続いた場合は message にフォールバック文言を入れて返す。

    hint3（停滞3回目以降）は物語文を絶対に生成してはいけない安全境界のため、
    LLMを一切呼ばずコード側の _build_tape_diagram だけで応答を組み立てる。
    """
    if support_level == "hint3":
        tape = _build_tape_diagram(history)
        if tape:
            return tape
        # 3構造すべて到達済みなのにhint3が来た場合（本来はgoalで止まる想定の保険）。
        # 未到達構造が無いのでテープ図は作れず、通常のLLM対話にフォールバックする。

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
                # form/talk/hint1/hint2/hint3では図を出さない。プロンプト任せにせずコード側でも強制する。
                # （hint3は通常上の短絡で処理済みだが、フォールバック経路の保険として含める）
                if support_level in ("form", "talk", "hint1", "hint2", "hint3"):
                    figure = None
                target = result.get("target_structure")
                if target not in ("tobun", "hougan", "bai"):
                    target = None
                return {
                    "message": message,
                    "figure": figure,
                    "target_structure": target,
                    "state": result.get("state"),
                    "tape_diagram": None,
                }
        except Exception as e:
            print(f"[ai_dialogue] dialogue failed (attempt {attempt + 1}): {type(e).__name__}: {e}")

    return {
        "message": FALLBACK_MESSAGE, "figure": None, "target_structure": None,
        "state": "fallback", "tape_diagram": None,
    }
