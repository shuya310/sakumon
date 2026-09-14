"""児童に返す文言（仕様 v2 3章）。文言は仕様の表を一字一句そのまま使う（言い換えない）。

どの種類・強さの応答を返すかは main.py の状態機械（2章）が決める。ここでは文言を組み立てるだけ。

  form     不成立作問への形式の支援（issue に応じて1点だけ。定型）             3-2
  praise   新構造の称賛／同じ構造1回目（定型）                                   3-3
  prompt   予告支援。1=弱（予告を書かせる）2=中（自己ラベル→目標の指定）3=強（目標＋場面固定） 3-4〜3-6
  done     3つそろった（定型）                                                    3-7
  talk     作問以外の入力（LLM＋コード側ガード、失敗時は定型文）
  error    judge が API 不通（定型。3-2 の error）

`**…**` は強調（フロントで太字にする）。改行は \\n。

語彙（6章）：児童向け文言では「種類」「たずねる」「聞いていること」「ちがうことを聞く」を使わない。
「求める」「求めているもの」「求めるものが ちがう」に統一。構造名（等分除・包含除・倍）は出さない。

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

# 3-1 構造の児童向けラベル（内部名は児童に出さない）
STRUCTURE_LABEL = {
    "tobun": "1つ分の 大きさ",
    "hougan": "いくつ分",
    "bai": "何倍",
}

# 3-2 形式の支援（issue に応じて1点だけ）
FORM_MESSAGES = {
    "no_question": "求める ことを きく 文が ないみたいだよ。さいごに「〜は いくつですか」を 書いて みよう。",
    "wrong_number": "{dividend}と {divisor}を つかう 問題に しよう。いまの お話だと しきが ちがって しまうよ。",
    "not_problem": "まだ お話に なって いないみたいだよ。「〜が あります」から はじめて みよう。",
    "scene_contradiction": "求める ことの 答えが、お話の 中に もう 書いて あるよ。どこか さがして みよう。",
    "error": "うまく よみとれなかったよ。もう一度 おくって みてね。",
}
# 表に無い issue は最も近い1点に寄せる（複数あっても1つに絞る）
_FORM_ALIAS = {
    "wrong_operation": "wrong_number",   # しきが ちがう
    "incomplete_text": "no_question",    # 求める文まで書けていない
}

# 3-3 なし（強度0）
PRAISE_NEW = "新しい 問題が できたね！\nほかにも、**求めるものが ちがう** 問題は 作れるかな？"
PRAISE_REPEAT = "いいね、また 一つ できたね。\n今度は **求めるものが ちがう** 問題も 作れそうかな？"

# 3-4 弱（強度1）
PROMPT_WEAK = "つぎは **何を 求める** 問題に する？\nきめてから、作って みよう。"

# 3-5 中（強度2）— 2ステップ対話
SELF_LABEL_STEP1 = "いま 作って くれた 問題は、**何を 求めて いる** のかな？"
SELF_LABEL_STEP2 = "それは この 3つの どれかな？\n　□ 1つ分の 大きさ　□ いくつ分　□ 何倍"
TARGET_MESSAGES = {
    "tobun": "じゃあ 今度は「**1つ分の 大きさ**」を 求める 問題に して みよう。\n"
             "{dividend}こを {divisor}人で 同じ数ずつ 分けたら、1人分は いくつに なるかな。",
    "hougan": "じゃあ 今度は「**いくつ分**」を 求める 問題に して みよう。\n"
              "{dividend}こを {divisor}こずつの まとまりに したら、まとまりは いくつ できるかな。",
    "bai": "{divisor}を **1つの かたまり**と みると、{dividend}の 中に かたまりは いくつ あるかな。\n"
           "それを「{divisor}の **何倍**」と いうよ。\n"
           "{divisor}を もとにして、「**何倍**」を 求める 問題に して みよう。",
}

# 3-6 強（強度3）— 目標の指定＋場面の固定。「お話は そのままで いいよ」は文頭
STRONG_MESSAGES = {
    "tobun": "{ref_no}ばんの お話は **そのままで いいよ**。\n"
             "おなじ ものを {divisor}人で 分けて、**1人分の 大きさ**を 求める 問題に かえられるかな？",
    "hougan": "{ref_no}ばんの お話は **そのままで いいよ**。\n"
              "おなじ ものを {divisor}こずつ まとめて、**まとまりの 数**を 求める 問題に かえられるかな？",
    "bai": "{ref_no}ばんの お話は **そのままで いいよ**。\n"
           "{divisor}こを もとに すると、{dividend}こは その **何倍**かな。それを 求める 問題に かえられるかな？",
}

# 3-7 完了
DONE_MESSAGE = "3つ とも できたね！\n1つ分の 大きさ、いくつ分、何倍——ぜんぶ ちがう ものを 求める 問題が そろったよ。"

# talk のフォールバック（LLM 不通・境界違反）。まだ1問も作れていない子には素材想起(a)、
# 1問以上作れている子には求めているものだけを変える提案(b)。休けい・終了・謝罪は入れない。
TALK_FALLBACK = "そっか。じゃあ、身近なところで、{dividend}こ あるものは何かな？"
TALK_FALLBACK_REWRITE = "そっか。じゃあ、いま作った 問題の「求めているもの」だけ、かえてみようか。"


def _fill(text: str, expression: str, **extra) -> str:
    dividend, divisor = parse_expression(expression)
    out = text.replace("{dividend}", str(dividend)).replace("{divisor}", str(divisor))
    out = out.replace("{expr}", f"{dividend} ÷ {divisor}")
    for k, v in extra.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def form_message(issue: str | None, expression: str) -> str:
    key = _FORM_ALIAS.get(issue, issue)
    return _fill(FORM_MESSAGES.get(key, FORM_MESSAGES["not_problem"]), expression)


def target_message(target: str, expression: str) -> str:
    """中・ステップ3。"""
    return _fill(TARGET_MESSAGES[target], expression)


def strong_message(target: str, expression: str, ref_no: int) -> str:
    """強。ref_no は児童の成立問題のうち最新の表示番号。"""
    return _fill(STRONG_MESSAGES[target], expression, ref_no=ref_no)


def pick_unreached_structure(history: list[str]) -> str | None:
    """まだ到達していない構造をひとつ、決定論的な固定順で選ぶ。"""
    for s in STRUCTURE_ORDER:
        if s not in history:
            return s
    return None


# ===== LLM（talk のみ） =====

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

_RAW_PROMPT = """あなたは小学4年生が「わり算のお話づくり（作問）」をするのを助ける先生です。
子どもは、式 {expression} になる問題を作っています。いまは、子どもが作問以外のこと
（つぶやき・感想・あいさつ・確認・困りやいやがる気持ちの表明など）を書いてきたところです。

# 絶対に守る境界（これを破ると研究が成立しない）
- 完成した問題文、またはその骨格を渡さない。そのまま書き写せば問題文になる一文を出してはならない。
- 「だれが・なにを・なんこ・どうする」のような穴うめの型を与えない。
- 構造の名前（等分除・包含除・倍）を子どもに見せない。問題を分類して伝えない。
- 数量関係（だれが何をどう分けるか・何と何を比べるか）を渡さない。
  「{dividend}こを{divisor}こずつ」「{divisor}人で分ける」のように、数と数の関係を含む言い方は禁止。
- 「1つ分」「いくつ分」「何倍」という言葉は使わない。
- 答え（数値 {quotient}）を教えない。
- 1〜2文で短く。やさしく、はげます口調。

# 言葉づかい（必ず守る）
- 「種類」「たずねる」「聞いていること」「ちがうことを聞く」は使わない。
- かわりに「求める」「求めているもの」「求めるものが ちがう」を使う。

# 返し方
★困り・いやがる・ネガティブな表明（「もうやだ」「続けられない」「なんだそれ」等）が来ても、
  「活動をやめたい」のサインだとは解釈しないこと。「いまの足場（手がかり）が足りない」だけだと考える。
絶対に書かないこと：
  - 休けい・休息の提案（「休けいしてもいいよ」「少し休んでから」「今日はここまで」等）
  - 活動の終了・中断をにおわせる言い方
  - 再開を子どもの意欲まかせにする言い方（「また作りたくなったら教えてね」等）
  - 「ごめんね」など謝りすぎた言い方
気持ちは一言だけ受け止めたら、必ずそのあとに具体的な手がかりを1つ出して作問にもどす
（気持ちを受け止めるだけで終わらせない）。
手がかりは次の2つのうち、状況に合う一方だけを出す（両方は出さない・新しい手がかりを作らない）：
  (a) まだ1問も作れていない子には、身近な場面（教室・きゅうしょく・体育・家・お店 等）から
      {expression} に合いそうな具体物を思い出させる問いかけ。数量関係は書かない。
  (b) すでに1問以上作れている子には、その問題の「求めているもの」だけを
      変えてみようという提案。素材を変えさせたり、新しい場面を出したりしない。
  「これまでに作れた問題の数」が0なら (a)、1以上なら (b) を選ぶ。
「答えを教えて」と言われても答えは渡さない。ただし断るだけで終わらせず、必ず上の(a)(b)いずれかの
  手がかりにつなげること（断って終わりにしない）。

# 文字づかい
{KANJI_RULE}

# 出力（JSONのみ。JSON以外の文字は出力しない）
{
  "check": "これから書く声かけが境界を破っていないかの自己確認（1文・ログ用）",
  "message": "子どもへの声かけ（1〜2文）",
  "state": "読み取った子どもの状態（ログ用・短く）"
}"""


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
_BANNED_ALWAYS = ("等分除", "包含除", "倍の話", "倍のお話", "くらべる話", "分ける話", "構造")
_BANNED_UNKNOWN_WORDS = ("1つ分", "１つ分", "一つ分", "1人分", "１人分", "一人分", "いくつ分",
                         "何倍", "なんばい", "もとの大きさ", "もとにする", "1つあたり", "１つあたり",
                         "何人分", "何こ分", "さがしているもの", "さがすもの")
# 6章 語彙の統一：児童向け文言に出してはいけない言葉
_BANNED_VOCAB = ("種類", "たずね", "聞いていること", "ちがうことを聞く")
# talk では困り・ネガティブな表明を「やめたい」と誤読して活動の終了に誘導してしまう表現を禁止する
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
    for w in _BANNED_VOCAB:
        if w in message:
            return f"banned_vocab:{w}"
    if response_type == "talk":
        for w in _BANNED_TALK:
            if w in message:
                return f"banned_talk:{w}"
        dividend, divisor = parse_expression(expression)
        if _has_both_numbers(message, dividend, divisor):
            return "both_numbers"
    if len(message) > 120:
        return "too_long"
    return None


def _fallback(expression: str, has_problem: bool) -> str:
    dividend, _divisor = parse_expression(expression)
    if has_problem:
        return TALK_FALLBACK_REWRITE
    return TALK_FALLBACK.replace("{dividend}", str(dividend))


def _llm_message(child_message: str, input_kind: str, judge_result: dict | None,
                 history: list[str], recent_turns: list[dict] | None,
                 response_type: str, expression: str,
                 user_id: str | None = None) -> dict:
    user_content = f"""子どもの発話: {child_message}

これまでに作れた問題の数: {len(history)} / 3

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

    return {"message": _fallback(expression, has_problem=bool(history)),
            "state": f"{response_type}_fallback",
            "meta": {"retry_count": retry_total, "status": "failed"}}


# ===== 入口 =====

def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             response_type: str, expression: str,
             prompt_strength: int | None = None, target: str | None = None,
             ref_no: int | None = None, user_id: str | None = None) -> dict:
    """児童向けの文言を組み立てる。

    戻り値: {"message", "state"}（LLM を呼んだ talk では "meta" も付く：retry_count / status）
    """
    jr = judge_result or {}
    if response_type == "done":
        return {"message": DONE_MESSAGE, "state": "done"}
    if response_type == "form":
        return {"message": form_message(jr.get("issue"), expression), "state": f"form_{jr.get('issue')}"}
    if response_type == "error":
        return {"message": FORM_MESSAGES["error"], "state": "judge_error"}
    if response_type == "praise":
        if jr.get("is_new"):
            return {"message": PRAISE_NEW, "state": "praise_new"}
        return {"message": PRAISE_REPEAT, "state": "praise_repeat"}
    if response_type == "prompt":
        if prompt_strength == 1:
            return {"message": PROMPT_WEAK, "state": "prompt_weak"}
        if prompt_strength == 2:
            return {"message": SELF_LABEL_STEP1, "state": "prompt_mid_step1"}
        if prompt_strength == 3 and target:
            return {"message": strong_message(target, expression, ref_no or 1), "state": f"prompt_strong_{target}"}
        return {"message": FALLBACK_MESSAGE, "state": "prompt_invalid"}
    if response_type == "talk":
        return _llm_message(child_message, input_kind, judge_result, history, recent_turns,
                            "talk", expression, user_id=user_id)
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
