"""児童の作問を支援する対話AI（支援水準 0〜4）。

支援水準は main.py が決定論的に決めて渡す。ここでは水準ごとに声かけを組み立てる。

  form     【水準0・不成立作問】成立性のフィードバック（LLM＋コード側ガード、失敗時は定型文）
  level1   自分の産出を比べさせる：「聞いていることは同じ？ちがう？」＋2択ボタン（定型・LLM不使用）
  level2   未到達の「求める量」を1つだけ名指す（定型・LLM不使用。信号機の表示はフロント側）
  level3   最終段。児童の状態で2モードに分かれる（state 列で区別できる）
             ・産出あり（level3_rewrite）：自分の既存産出を素材として再利用させ、
               問いだけを差し替える課題を出す（定型・LLM不使用）
             ・産出なし／S0（level3_scene）：場面が何も浮かんでいないので素材を想起させる
               （LLM＋コード側ガード、失敗時は定型文）
  discover 新構造が出た（称賛のみ。LLM＋ガード、失敗時は定型文）
  goal     3構造そろった（定型）
  talk     作問以外の入力（LLM＋ガード、失敗時は定型文）
  none     フェーズ1・3。表示しないので LLM は呼ばず「おくったよ」だけ返す

絶対に守る境界（コード側でも検査する）：
  - 完成した問題文・その骨格を渡さない
  - 構造名（等分除・包含除・倍）を児童に見せない。返すのは常に「求める量」の言葉
  - 数量関係（{dividend}こを{divisor}こずつ 等）を渡さない
  - 答え（数値）を教えない

テープ図・構造図（figure / tape_diagram）は config.ENABLE_FIGURES=False で全面無効化。
コードは残すが呼ばれない。
"""

import os
import re

import anthropic

from config import MODEL, ENABLE_FIGURES, parse_expression
from llm_json import extract_json
from kanji_rule import KANJI_RULE

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

FALLBACK_MESSAGE = "もう一度 おくってみてね"
ACK_MESSAGE = "おくったよ"          # フェーズ1・3の最小表示

STRUCTURE_ORDER = ("tobun", "hougan", "bai")

# 児童に返してよい言葉は「求める量」だけ。構造名は使わない。
QUESTION_JA = {
    "tobun": "1つ分はいくつ？",
    "hougan": "いくつ分ある？",
    "bai": "何倍？",
}
UNKNOWN_JA = {
    "one_unit": "1つ分", "num_units": "いくつ分",
    "ratio": "何倍", "base": "もとの大きさ", "rate": "1つあたり",
}

COMPARE_BUTTONS = ["同じ", "ちがう"]

# structured outputs（output_config.format）のスキーマ。JSON 以外を書けなくする。
# check を先頭に置いて、声かけを書く前に境界（構造名・数量関係・答えを渡さない）を
# 自分で確認させる（プロパティは順に生成されるので、順序に意味がある）。
OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "check": {"type": "string"},
            "message": {"type": "string"},
            "state": {"type": "string"},
        },
        "required": ["check", "message", "state"],
        "additionalProperties": False,
    },
}

# ===== 定型文（LLM不使用） =====

def _subject(problems: list[dict], current_structure: str | None) -> str:
    """比較させる対象の呼び方。全部が同じ構造なら「この3つは」、複数構造がまじっていれば
    反復した構造の番号を名指しする（「1ばんと3ばんは」）。正解が「ちがう」になる問いを出さないため。
    番号は右パネルの通し番号（1.2.3…）に合わせる。"""
    n = len(problems)
    same_idx = [i for i, p in enumerate(problems) if p.get("structure") == current_structure]
    if current_structure and 2 <= len(same_idx) < n:
        return "と".join(f"{i + 1}ばん" for i in same_idx) + "は"
    return f"この{n}つは"


def compare_message(problems: list[dict], current_structure: str | None) -> str:
    """【水準1】産出一覧は右パネルに常設なので、チャットには列挙しない。
    一覧を見させたうえで「聞いていることは同じ？ちがう？」と問う。
    題材（りんご→えんぴつ）ではなく「聞いていること」で自分の産出を分類させるのが、この段の仕事。
    停滞中の児童に「たくさん作れている」と伝わらないよう、数はほめない。"""
    return ("右の「作った お話」を 見てみよう。\n"
            f"{_subject(problems, current_structure)}、聞いていることが 同じかな？ ちがうかな？")


def compare_same_reply() -> str:
    """水準1の問いに「同じ」と答えられた＝気づけている。水準は上げず、挑戦だけ促す。"""
    return "そうだね、聞いていることは 同じだね。じゃあ、ちがうことを聞くお話は 作れるかな？"


def rewrite_message(problems: list[dict], target_structure: str | None) -> str:
    """【水準3・産出あり】自分の既存産出を素材にして、問いだけを差し替える課題を出す。

    新しい素材を探させてはいけない。素材の差し替え（りんご→えんぴつ）は 9/1 で観測された
    同型反復そのもので、最終段でそれを促すと誤概念を強化してしまう。
    渡すのは「素材は自分のものを流用してよい」という許可だけで、素材も数量関係も渡さない。
    ねらいは「素材を変えれば別の問題」の逆＝場面を固定して問いを変える操作をさせること
    （Brown & Walter の What-If-Not 戦略）。

    「場面はそのまま」とは言わない。24 ÷ 8 で 1つ分 ⇄ いくつ分 に移ると除数 8 の意味が
    変わるので、文言も変えないと成立しない。そこに気づくこと自体がねらいなので、
    「同じ もの を つかって いい」までに留める。
    """
    if not problems or not target_structure:
        return ""
    n = len(problems)   # 右パネルの通し番号。直前に書いた1問を指す
    return (f"{n}ばんの お話と 同じ ものを つかって いいよ。\n"
            f"こんどは「{QUESTION_JA[target_structure]}」を 聞く お話に できるかな？")


def unknown_hint_message(target_structure: str | None) -> str:
    """【水準2】まだ聞いていない「求める量」を1つだけ言葉で渡す。

    3つを列挙しない。答えの空間を一度に渡すと、以降の産出が「言われたから作った」になり、
    意図的産出として測れなくなる（＝主要指標が壊れる）。
    「3つあって、あと何個空いているか」は、ラベルなし3灯の信号機が同時に見せる。
    """
    if not target_structure:
        return "ほかにも 聞けることが あるよ。右の ⭐ を 見てみよう。"
    return ("ほかにも 聞けることが あるよ。\n"
            f"たとえば「{QUESTION_JA[target_structure]}」を 聞く お話は 作れるかな？")


def goal_message(first_time: bool) -> str:
    if first_time:
        return "すごい！3つとも作れたね！ 聞いていることが ちがうお話が 3つそろったよ。"
    return "もう3つとも作れているよ。ほかにも ちがうお話が 作れそうかな？"


FORM_FALLBACK = {
    "scene_contradiction": "お話の中に、聞いている答えが もう書いてあるみたいだよ。もう一度 読んでみよう。",
    "wrong_number": "そのお話、{expression} の式になるかな？ 数を たしかめてみよう。",
    "wrong_operation": "そのお話は わり算で答えが出るかな？ たしかめてみよう。",
    "incomplete_text": "お話が とちゅうで 切れているみたい。つづきを 書いてみよう。",
    "no_question": "何を聞くお話かな？ 聞きたいことを 最後に 書いてみよう。",
    "not_problem": "{expression} になる お話を 作ってみよう。",
}
DISCOVER_FALLBACK = "いいね！新しいお話が できたね！"
TALK_FALLBACK = "そうなんだね。またお話を 作ってみてね。"
LEVEL3_FALLBACK = "身近なところで、{dividend}こ あるものは何かな？ 教室や きゅうしょくの時間を 思い出してみよう。"


def pick_unreached_structure(history: list[str]) -> str | None:
    """信号機がまだ点いていない構造をひとつ、決定論的な固定順で選ぶ。"""
    for s in STRUCTURE_ORDER:
        if s not in history:
            return s
    return None


# ===== LLM（form / discover / level3 / talk） =====

_RAW_PROMPT = """あなたは小学4年生が「わり算のお話づくり（作問）」をするのを助ける先生です。
子どもは、式 {expression} になるお話を作っています。

# 絶対に守る境界（これを破ると研究が成立しない）
- 完成した問題文、またはその骨格を渡さない。そのまま書き写せば問題文になる一文を出してはならない。
- 「だれが・なにを・なんこ・どうする」のような穴うめの型を与えない。
- 構造の名前（等分除・包含除・倍）を子どもに見せない。「どんな種類のお話か」を分類して伝えない。
- 数量関係（だれが何をどう分けるか・何と何を比べるか）を渡さない。
  「{dividend}こを{divisor}こずつ」「{divisor}人で分ける」のように、数と数の関係を含む言い方は禁止。
- 「何を求めたか」「1つ分」「いくつ分」「何倍」などの言葉は、level3 以外では使わない。
- 答え（数値 {quotient}）を教えない。
- 1〜2文で短く。やさしく、はげます口調。

# 支援の段階（main.py が決めて渡す。これに従う）
- form：お話が成立していない。足りていない点を1つだけ、問いかけの形で返す。
  判定理由を参考に「何を聞いているのかが書いてあるかな？」「そのお話、{expression} の式になるかな？」のように
  直し方の方向だけを示す。正しい問題文の例は書かない。子どもの文の一部（「〜」と書いてあるね）を引いてもよい。
- discover：新しい種類のお話ができた。称賛だけを返す。お話の題材（あめ・リボン・班 など）に触れてほめてよいが、
  「何を求めたか」「どんな種類か」「次は何を作るか」は一切言わない。次の構造を名指ししない。
  ★「分ける」「配る」「くらべる」「〜ずつ」「何倍」など、お話の中の操作や数量関係を言い当てる言葉も使わない
  （「4人に分けるところがいいね」「長さをくらべるお話だね」は不可）。ほめるのは題材と、作れたこと自体だけ。
- level3：まだ1問も作れておらず、場面が何も思い浮かんでいない状態
  （すでに作れている子はここに来ない。コード側で別の定型文に分岐している）。
  「場面」を【素材（何の話か）】と【数量関係（だれが何をどう分けるか）】に分け、素材にだけ触れる。
  子どもの身近な場面（教室・きゅうしょく・体育・家・お店・遠足）から、素材を思い出させる問いかけをする。
  OK例：「教室にあるもので、{dividend}こあるものって何かな？」「きゅうしょくの時間だと、どんな場面が思いつく？」
  NG例：「{dividend}このあめを1人に{divisor}こずつ配ったら…みたいなお話はどう？」（数量関係を丸ごと渡している）
  まだ作れていない聞き方（下に示す）を意識して素材を選んでよいが、数量関係は書かない。
- talk：作問以外の入力のうち、困り（「わからない」「どうしたら」等）ではないもの
  （つぶやき・感想・あいさつ・確認）。やさしく短く受け止め、作問にもどれるよう軽くうながす。
  構造の名前や「何を求めるか」は教えない。
  ※困りの訴えは talk には来ない（main.py が支援水準を1段上げて構造支援に回す）。
    ここで「自分で考えてみよう」と突き放して堂々めぐりにしないこと。

# 文字づかい
{KANJI_RULE}

# 出力（JSONのみ。JSON以外の文字は出力しない）
{
  "check": "これから書く声かけが境界を破っていないかの自己確認（1文・ログ用）",
  "message": "子どもへの声かけ（1〜2文）",
  "state": "読み取った子どもの状態（ログ用・短く）"
}"""

_ISSUE_JA = {
    "scene_contradiction": "場面矛盾：問いの答えが場面の中ですでに与えられている、または場面と問いの向きがずれている",
    "wrong_number": "式が {expression} にならない（数値がちがう・逆になっている）",
    "wrong_operation": "わり算ではなく、かけ算・たし算・ひき算の問題になっている",
    "incomplete_text": "文が途中で切れていて、求める量が特定できない",
    "no_question": "場面だけで、何を求めるかが書かれていない",
    "not_problem": "場面の記述がなく、文章題として成立していない",
}


def _build_system(expression: str) -> str:
    dividend, divisor = parse_expression(expression)
    return (_RAW_PROMPT
            .replace("{KANJI_RULE}", KANJI_RULE)
            .replace("{expression}", f"{dividend} ÷ {divisor}")
            .replace("{dividend}", str(dividend))
            .replace("{divisor}", str(divisor))
            .replace("{quotient}", str(dividend // divisor)))


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
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("no text block in response")


# ---- コード側ガード ----
_BANNED_ALWAYS = ("等分除", "包含除", "倍の話", "倍のお話", "くらべる話", "分ける話", "構造", "何を求め", "求める量")
_BANNED_UNKNOWN_WORDS = ("1つ分", "１つ分", "一つ分", "1人分", "１人分", "一人分", "いくつ分",
                         "何倍", "なんばい", "もとの大きさ", "もとにする", "1つあたり", "１つあたり",
                         "何人分", "何こ分", "さがしているもの", "さがすもの")
_BANNED_LEVEL3 = ("ずつ",)   # 水準3（素材想起）で数量関係を渡させない
# discover では操作・数量関係を言い当てる言葉も禁止（種類の分類を暗に伝えてしまうため）
_BANNED_DISCOVER = ("分け", "配", "くらべ", "比べ", "ずつ", "等分", "まとめ")


def _has_both_numbers(text: str, dividend: int, divisor: int) -> bool:
    def present(n: int) -> bool:
        return re.search(rf"(?<![0-9]){n}(?![0-9])", text) is not None
    return present(dividend) and present(divisor)


def violates_boundary(message: str, support_level: str, expression: str) -> str | None:
    """境界を破っていれば理由を返す（None なら合格）。"""
    for w in _BANNED_ALWAYS:
        if w in message:
            return f"banned:{w}"
    if support_level in ("discover", "talk", "form"):
        for w in _BANNED_UNKNOWN_WORDS:
            if w in message:
                return f"banned_unknown:{w}"
    if support_level == "level3":
        for w in _BANNED_LEVEL3:
            if w in message:
                return f"banned_level3:{w}"
    if support_level == "discover":
        for w in _BANNED_DISCOVER:
            if w in message:
                return f"banned_discover:{w}"
    if support_level in ("discover", "talk", "level3"):
        dividend, divisor = parse_expression(expression)
        if _has_both_numbers(message, dividend, divisor):
            return "both_numbers"
    if len(message) > 120:
        return "too_long"
    return None


def _fallback(support_level: str, judge_result: dict | None, expression: str) -> str:
    dividend, divisor = parse_expression(expression)
    expr = f"{dividend} ÷ {divisor}"
    if support_level == "form":
        issue = (judge_result or {}).get("issue")
        return FORM_FALLBACK.get(issue, FORM_FALLBACK["not_problem"]).replace("{expression}", expr)
    if support_level == "discover":
        return DISCOVER_FALLBACK
    if support_level == "level3":
        return LEVEL3_FALLBACK.replace("{dividend}", str(dividend))
    return TALK_FALLBACK


def _llm_message(child_message: str, input_kind: str, judge_result: dict | None,
                 history: list[str], recent_turns: list[dict] | None,
                 support_level: str, expression: str, target_structure: str | None) -> dict:
    dividend, divisor = parse_expression(expression)
    expr = f"{dividend} ÷ {divisor}"

    if input_kind == "taiwa" or not judge_result:
        situation = "これは対話（質問・つぶやき・こまった等）です。新しい作問ではありません。"
    elif not judge_result.get("valid"):
        issue = judge_result.get("issue")
        situation = "作問したが成立していない。判定理由：" + _ISSUE_JA.get(issue, "文章題として成立していない").replace("{expression}", expr)
    elif judge_result.get("is_new"):
        situation = "作問成立。これまでにない新しい聞き方のお話ができた（称賛のみ。中身の分類は言わない）。"
    else:
        situation = "作問成立。ただし、すでに作ったことのある聞き方のくり返し。"

    reached = len(history)
    extra = ""
    if support_level == "level3" and target_structure:
        extra = f"\nまだ作れていない聞き方: 「{QUESTION_JA[target_structure]}」（この聞き方に向く素材を思い出させる。数量関係は書かない）"

    user_content = f"""子どもの発話: {child_message}
この発話の区別: {"作問" if input_kind == "sakumon" else "対話"}
いまの支援の段階: {support_level}

直近の作問の判定結果:
{situation}

これまでに作れた聞き方の数: {reached} / 3{extra}

直近のやりとりの履歴:
{_build_history(recent_turns)}"""

    system = _build_system(expression)
    for attempt in range(2):  # 1回リトライ
        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=512,
                thinking={"type": "disabled"},
                system=system,
                output_config={"format": OUTPUT_SCHEMA},
                messages=[{"role": "user", "content": user_content}],
            )
            if response.stop_reason == "max_tokens":
                raise ValueError("response truncated (max_tokens)")
            result = extract_json(_text_from(response))
            message = (result.get("message") or "").strip()
            if not message:
                continue
            reason = violates_boundary(message, support_level, expression)
            if reason:
                print(f"[ai_dialogue] boundary violation ({support_level}, {reason}): {message}")
                continue  # リトライ（2回目も違反なら定型文へ）
            return {"message": message, "state": result.get("state") or support_level}
        except Exception as e:
            print(f"[ai_dialogue] dialogue failed (attempt {attempt + 1}): {type(e).__name__}: {e}")

    return {"message": _fallback(support_level, judge_result, expression), "state": f"{support_level}_fallback"}


# ===== 入口 =====

def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             support_level: str, expression: str,
             problems: list[dict] | None = None,
             target_structure: str | None = None,
             first_goal: bool = True) -> dict:
    """児童向けの声かけを組み立てる。

    戻り値: {"message", "buttons", "figure", "target_structure", "state", "tape_diagram",
             "highlight_problems"}
    figure / tape_diagram は ENABLE_FIGURES=False のため常に None。
    highlight_problems は右パネルの産出一覧を参照させる水準（1と、水準3の rewrite）で True。
    """
    base = {"buttons": None, "figure": None, "target_structure": target_structure,
            "tape_diagram": None, "highlight_problems": False}
    problems = problems or []
    current_structure = (judge_result or {}).get("structure")

    if support_level == "none":
        return {**base, "message": ACK_MESSAGE, "state": "no_support_phase"}
    if support_level == "level1":
        return {**base, "message": compare_message(problems, current_structure),
                "buttons": list(COMPARE_BUTTONS), "state": "level1_compare",
                "highlight_problems": True}
    if support_level == "level2":
        return {**base, "message": unknown_hint_message(target_structure),
                "state": "level2_unknown_hint"}
    if support_level == "goal":
        return {**base, "message": goal_message(first_goal), "state": "goal"}
    if support_level == "level3" and problems:
        # 産出がある子には素材を渡さない（＝同型反復を促さない）。自分の産出を使わせる。
        text = rewrite_message(problems, target_structure)
        if text:
            return {**base, "message": text, "state": "level3_rewrite",
                    "highlight_problems": True}
    if support_level in ("form", "discover", "level3", "talk"):
        out = _llm_message(child_message, input_kind, judge_result, history, recent_turns,
                           support_level, expression, target_structure)
        if support_level == "level3":
            # 水準3の2モードを CSV で機械的に分けられるよう、state の頭を固定する
            # （level3_rewrite / level3_scene。後半はLLMが読み取った状態）
            out["state"] = f"level3_scene / {out.get('state') or ''}".strip(" /")
        return {**base, **out}
    # 想定外の水準（保険）
    return {**base, "message": FALLBACK_MESSAGE, "state": "unknown_level"}


# ===== 以下、図の生成（ENABLE_FIGURES=False のため呼ばれない。後から戻せるように残す） =====

STRUCTURE_LABEL_JA = {"tobun": "等分除", "hougan": "包含除", "bai": "倍"}
TAPE_DIAGRAM_MESSAGE = "このテープ図に合うお話を考えてみよう。"


def _tape_diagram_payload(structure: str, dividend: int, divisor: int) -> dict:
    if structure == "tobun":
        known, unknown = {"全体量": dividend, "いくつ分": divisor}, "1あたり量"
    elif structure == "hougan":
        known, unknown = {"全体量": dividend, "1あたり量": divisor}, "いくつ分"
    else:  # bai
        known, unknown = {"比較量": dividend, "基準量": divisor}, "倍"
    return {"type": "tape_diagram", "structure": STRUCTURE_LABEL_JA[structure], "known": known, "unknown": unknown}


def _build_tape_diagram(history: list[str], expression: str) -> dict | None:
    if not ENABLE_FIGURES:
        return None
    structure = pick_unreached_structure(history)
    if not structure:
        return None
    dividend, divisor = parse_expression(expression)
    return {"message": TAPE_DIAGRAM_MESSAGE, "figure": None, "target_structure": structure,
            "state": "tape_diagram", "tape_diagram": _tape_diagram_payload(structure, dividend, divisor)}
