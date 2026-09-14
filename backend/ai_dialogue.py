"""児童の作問に返す声かけ（response_type ごと）。

どの種類の応答を返すかは main.py が決定論的に決めて渡す。ここでは種類ごとに声かけを組み立てる。

  form     不成立作問への成立性のフィードバック（LLM＋コード側ガード、失敗時は定型文）
  praise   成立作問への称賛。新しい聞き方なら LLM（ガード付き）、既出の聞き方のくり返しなら定型文
  done     3構造そろった（定型）
  talk     作問以外の入力（LLM＋ガード、失敗時は定型文）。困り・ネガティブな表明も
           「やめたい」ではなく「足場不足」と解釈し、休けい・終了は提案せず、
           必ず具体的な手がかりを1つ出して作問にもどす
  prompt   予告支援（強さ 1=弱／2=中／3=強）。強さは main.py の状態機械が決める。
           文言は仕様 v2 3章で置き換える（いまは暫定文）

フェーズ1・3は表示しないので LLM は呼ばず、main.py が「おくったよ」だけ返す（ai_message は記録しない）。

絶対に守る境界（コード側でも検査する）：
  - 完成した問題文・その骨格を渡さない
  - 構造名（等分除・包含除・倍）を児童に見せない。返すのは常に「求める量」の言葉
  - 数量関係（{dividend}こを{divisor}こずつ 等）を渡さない
  - 答え（数値）を教えない
  - talk で休けい・活動の終了/中断・再開を児童の意欲まかせにする表現・過剰な謝罪を出さない

テープ図・構造図（figure / tape_diagram）は config.ENABLE_FIGURES=False で全面無効化。
コードは残すが呼ばれない。
"""

import re

from config import MODEL, ENABLE_FIGURES, parse_expression
from llm_json import extract_json
from kanji_rule import KANJI_RULE
import llm_call

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

def done_message(first_time: bool) -> str:
    if first_time:
        return "すごい！3つとも作れたね！ 聞いていることが ちがうお話が 3つそろったよ。"
    return "もう3つとも作れているよ。ほかにも ちがうお話が 作れそうかな？"


# 既出の聞き方のくり返し（成立はしている）。称賛だけ返し、次に何を作るかは言わない。
# ※ 仕様 v2 3-3 の文言で置き換える前提の暫定文。
REPEAT_MESSAGE = "できたね！ つぎの お話も 作ってみよう。"

# 予告支援（強さ別）。※ 仕様 v2 3-4〜3-6 の文言・2ステップ対話で置き換える前提の暫定文。
# 構造名・求める量の言葉・数量関係はここでも出さない。
PROMPT_MESSAGES = {
    1: "つぎは 何を 求める 問題に する？ きめてから、作って みよう。",
    2: "いま 作って くれた 問題は、何を 求めて いるのかな？ つぎの 目当てを 出すね。",
    3: "いまの お話は そのままで いいよ。求めるものだけ かえて みよう。",
}

FORM_FALLBACK = {
    "scene_contradiction": "お話の中に、聞いている答えが もう書いてあるみたいだよ。もう一度 読んでみよう。",
    "wrong_number": "そのお話、{expression} の式になるかな？ 数を たしかめてみよう。",
    "wrong_operation": "そのお話は わり算で答えが出るかな？ たしかめてみよう。",
    "incomplete_text": "お話が とちゅうで 切れているみたい。つづきを 書いてみよう。",
    "no_question": "何を聞くお話かな？ 聞きたいことを 最後に 書いてみよう。",
    "not_problem": "{expression} になる お話を 作ってみよう。",
}
PRAISE_FALLBACK = "いいね！新しいお話が できたね！"
# talk のフォールバックは2種類。まだ1問も作れていない子には素材想起(a)、
# 1問以上作れている子にはその問いだけを変える提案(b)。休けい・終了・謝罪は入れない。
TALK_FALLBACK = "そっか。じゃあ、身近なところで、{dividend}こ あるものは何かな？"
TALK_FALLBACK_REWRITE = "そっか。じゃあ、いま作った お話の「たずねているところ」だけ、かえてみようか。"


def pick_unreached_structure(history: list[str]) -> str | None:
    """まだ到達していない構造をひとつ、決定論的な固定順で選ぶ。"""
    for s in STRUCTURE_ORDER:
        if s not in history:
            return s
    return None


# ===== LLM（form / praise / talk） =====

_RAW_PROMPT = """あなたは小学4年生が「わり算のお話づくり（作問）」をするのを助ける先生です。
子どもは、式 {expression} になるお話を作っています。

# 絶対に守る境界（これを破ると研究が成立しない）
- 完成した問題文、またはその骨格を渡さない。そのまま書き写せば問題文になる一文を出してはならない。
- 「だれが・なにを・なんこ・どうする」のような穴うめの型を与えない。
- 構造の名前（等分除・包含除・倍）を子どもに見せない。「どんな種類のお話か」を分類して伝えない。
- 数量関係（だれが何をどう分けるか・何と何を比べるか）を渡さない。
  「{dividend}こを{divisor}こずつ」「{divisor}人で分ける」のように、数と数の関係を含む言い方は禁止。
- 「何を求めたか」「1つ分」「いくつ分」「何倍」などの言葉は使わない。
- 答え（数値 {quotient}）を教えない。
- 1〜2文で短く。やさしく、はげます口調。

# 応答の種類（main.py が決めて渡す。これに従う）
- form：お話が成立していない。足りていない点を1つだけ、問いかけの形で返す。
  判定理由を参考に「何を聞いているのかが書いてあるかな？」「そのお話、{expression} の式になるかな？」のように
  直し方の方向だけを示す。正しい問題文の例は書かない。子どもの文の一部（「〜」と書いてあるね）を引いてもよい。
- praise：新しい種類のお話ができた。称賛だけを返す。お話の題材（あめ・リボン・班 など）に触れてほめてよいが、
  「何を求めたか」「どんな種類か」「次は何を作るか」は一切言わない。次の構造を名指ししない。
  ★「分ける」「配る」「くらべる」「〜ずつ」「何倍」など、お話の中の操作や数量関係を言い当てる言葉も使わない
  （「4人に分けるところがいいね」「長さをくらべるお話だね」は不可）。ほめるのは題材と、作れたこと自体だけ。
- talk：作問以外の入力（つぶやき・感想・あいさつ・確認・困りやいやがる気持ちの表明など）。
  ★困り・いやがる・ネガティブな表明（「もうやだ」「続けられない」「なんだそれ」等）が来ても、
    「活動をやめたい」のサインだとは解釈しないこと。「いまの足場（手がかり）が足りない」だけだと考える。
  絶対に書かないこと：
    - 休けい・休息の提案（「休けいしてもいいよ」「少し休んでから」「今日はここまで」等）
    - 活動の終了・中断をにおわせる言い方
    - 再開を子どもの意欲まかせにする言い方（「また作りたくなったら教えてね」等）
    - 「ごめんね」など謝りすぎた言い方
  気持ちは一言だけ受け止めたら、必ずそのあとに具体的な手がかりを1つ出して作問にもどす
  （気持ちを受け止めるだけで終わらせない）。
  手がかりは次の2つのうち、状況に合う一方だけを出す（両方は出さない・新しい種類の手がかりを作らない）：
    (a) まだ1問も作れていない子には、身近な場面（教室・きゅうしょく・体育・家・お店 等）から
        {expression} に合いそうな具体物を思い出させる問いかけ。数量関係は書かない。
    (b) すでに1問以上作れている子には、そのお話の「たずねているところ（問い）」だけを
        変えてみようという提案。素材を変えさせたり、新しい場面を出したりしない。
    「これまでに作れた聞き方の数」が0なら (a)、1以上なら (b) を選ぶ。
  「答えを教えて」と言われても答えは渡さない。ただし断るだけで終わらせず、必ず上の(a)(b)いずれかの
    手がかりにつなげること（断って終わりにしない）。
  構造の名前や「何を求めるか」は教えない。

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
# praise では操作・数量関係を言い当てる言葉も禁止（種類の分類を暗に伝えてしまうため）
_BANNED_PRAISE = ("分け", "配", "くらべ", "比べ", "ずつ", "等分", "まとめ")
# talk では困り・ネガティブな表明を「やめたい」と誤読して活動の終了に誘導してしまう表現を禁止する
# （9/10 の試用で「休けいしてもいいよ」等が実害として出た）
_BANNED_TALK = ("休", "今日はここまで", "終わりにし", "中断", "たくなったら", "ごめん", "やめても")


def _has_both_numbers(text: str, dividend: int, divisor: int) -> bool:
    def present(n: int) -> bool:
        return re.search(rf"(?<![0-9]){n}(?![0-9])", text) is not None
    return present(dividend) and present(divisor)


def violates_boundary(message: str, response_type: str, expression: str) -> str | None:
    """境界を破っていれば理由を返す（None なら合格）。"""
    for w in _BANNED_ALWAYS:
        if w in message:
            return f"banned:{w}"
    for w in _BANNED_UNKNOWN_WORDS:
        if w in message:
            return f"banned_unknown:{w}"
    if response_type == "praise":
        for w in _BANNED_PRAISE:
            if w in message:
                return f"banned_praise:{w}"
    if response_type == "talk":
        for w in _BANNED_TALK:
            if w in message:
                return f"banned_talk:{w}"
    if response_type in ("praise", "talk"):
        dividend, divisor = parse_expression(expression)
        if _has_both_numbers(message, dividend, divisor):
            return "both_numbers"
    if len(message) > 120:
        return "too_long"
    return None


def _fallback(response_type: str, judge_result: dict | None, expression: str,
              has_problem: bool = False) -> str:
    dividend, divisor = parse_expression(expression)
    expr = f"{dividend} ÷ {divisor}"
    if response_type == "form":
        issue = (judge_result or {}).get("issue")
        return FORM_FALLBACK.get(issue, FORM_FALLBACK["not_problem"]).replace("{expression}", expr)
    if response_type == "praise":
        return PRAISE_FALLBACK
    if has_problem:
        return TALK_FALLBACK_REWRITE
    return TALK_FALLBACK.replace("{dividend}", str(dividend))


def _llm_message(child_message: str, input_kind: str, judge_result: dict | None,
                 history: list[str], recent_turns: list[dict] | None,
                 response_type: str, expression: str,
                 user_id: str | None = None) -> dict:
    dividend, divisor = parse_expression(expression)
    expr = f"{dividend} ÷ {divisor}"

    if input_kind == "taiwa" or not judge_result:
        situation = "これは対話（質問・つぶやき・こまった等）です。新しい作問ではありません。"
    elif not judge_result.get("valid"):
        issue = judge_result.get("issue")
        situation = "作問したが成立していない。判定理由：" + _ISSUE_JA.get(issue, "文章題として成立していない").replace("{expression}", expr)
    else:
        situation = "作問成立。これまでにない新しい聞き方のお話ができた（称賛のみ。中身の分類は言わない）。"

    user_content = f"""子どもの発話: {child_message}
この発話の区別: {"作問" if input_kind == "sakumon" else "対話"}
応答の種類: {response_type}

直近の作問の判定結果:
{situation}

これまでに作れた聞き方の数: {len(history)} / 3

直近のやりとりの履歴:
{_build_history(recent_turns)}"""

    system = _build_system(expression)

    def parse(response) -> dict:
        if response.stop_reason == "max_tokens":
            raise ValueError("response truncated (max_tokens)")
        return extract_json(_text_from(response))

    # API 呼び出し（タイムアウト・リトライ・同時実行制御）は llm_call に委ねる。
    # 境界違反・空メッセージは応答が返ったうえでの内容の問題なので、ここで1回だけ再依頼する。
    retry_total = 0
    for attempt in range(2):  # 1回リトライ
        try:
            result, meta = llm_call.call(
                user_id, parse,
                model=MODEL,
                max_tokens=512,
                thinking={"type": "disabled"},
                system=system,
                output_config={"format": OUTPUT_SCHEMA},
                messages=[{"role": "user", "content": user_content}],
            )
        except llm_call.LLMUnavailable as e:
            retry_total += e.retry_count
            print(f"[ai_dialogue] dialogue failed after {e.retry_count} retries: {e}")
            break  # API が応答しないなら再依頼しても無駄。定型文へ
        retry_total += meta["retry_count"]
        message = (result.get("message") or "").strip()
        if not message:
            continue
        reason = violates_boundary(message, response_type, expression)
        if reason:
            print(f"[ai_dialogue] boundary violation ({response_type}, {reason}): {message}")
            continue  # リトライ（2回目も違反なら定型文へ）
        return {"message": message, "state": result.get("state") or response_type,
                "meta": {"retry_count": retry_total, "status": "retried_ok" if retry_total else "ok"}}

    return {"message": _fallback(response_type, judge_result, expression, has_problem=bool(history)),
            "state": f"{response_type}_fallback",
            "meta": {"retry_count": retry_total, "status": "failed"}}


# ===== 入口 =====

def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             response_type: str, expression: str,
             first_done: bool = True, prompt_strength: int | None = None,
             user_id: str | None = None) -> dict:
    """児童向けの声かけを組み立てる。

    戻り値: {"message", "state"}（LLM を呼んだ種類では "meta" も付く：retry_count / status）
    """
    if response_type == "done":
        return {"message": done_message(first_done), "state": "done"}
    if response_type == "prompt":
        return {"message": PROMPT_MESSAGES.get(prompt_strength, PROMPT_MESSAGES[1]),
                "state": f"prompt_{prompt_strength}"}
    if response_type == "praise" and judge_result and not judge_result.get("is_new"):
        return {"message": REPEAT_MESSAGE, "state": "praise_repeat"}
    if response_type in ("form", "praise", "talk"):
        return _llm_message(child_message, input_kind, judge_result, history, recent_turns,
                            response_type, expression, user_id=user_id)
    # 想定外の種類（保険）
    return {"message": FALLBACK_MESSAGE, "state": "unknown_response_type"}


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
    return {"message": TAPE_DIAGRAM_MESSAGE, "figure": None, "state": "tape_diagram",
            "tape_diagram": _tape_diagram_payload(structure, dividend, divisor)}
