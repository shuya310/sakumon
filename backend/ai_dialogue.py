"""児童に返す文言（docs/sakumon_spec_v3.md 3章。form / praise / done は v2 3章のまま）。文言は仕様の表を一字一句そのまま使う（言い換えない）。

どの種類・強さの応答を返すかは main.py の状態機械（2章）が決める。ここでは文言を組み立てるだけ。

  form     不成立作問への形式の支援（issue に応じて1点だけ。定型）             3-2
  praise   新構造の称賛／同じ構造1回目（定型）                                   3-3
  prompt   予告支援。1=弱（役割の宣言・2ターン：除数が何をあらわすかを言わせ→次の作問で何の数にするかを予告させる。
           判定と食い違えば1回だけ児童の問題文の除数の句を引用して問い返す）2=中（目標の指定）3=強（目標＋場面固定）
  done     3つそろった（定型）                                                    3-7
  talk     作問以外の入力（LLM＋コード側ガード、失敗時は定型文）。現在の強度・目標・到達構造・直前の成立作問
           （本文と、わる数が指していたもの）を渡し、「話してよいこと」の境界は強度で切り替える
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
    "reversed": "wrong_number",          # 比較の向きが逆（ログでは区別。児童向け文言は暫定で wrong_number と同じ）
}

# 3-3 なし（強度0）
PRAISE_NEW = "新しい 問題が できたね！\nほかにも、**求めるものが ちがう** 問題は 作れるかな？"
PRAISE_REPEAT = "いいね、また 一つ できたね。\n今度は **求めるものが ちがう** 問題も 作れそうかな？"

# 弱（強度1）＝役割の宣言（2ターン）。構造のラベル（1つ分の 大きさ／いくつ分／何倍）は弱では出さない。
#   ターン1：直前の成立作問の除数が何をあらわしているかを児童に言わせる（answer → ai_classify.classify_role）
#   訂正   ：答えが判定の役割と食い違うとき1回だけ、児童自身の問題文の除数の句を引用して問い返す（正解の役割名は言わない）
#   ターン2：次の作問で除数を何の数にするか（予告。→ classify_declaration）
ROLE_ASK = "{ref_no}ばんの お話で、{divisor}は 何を あらわして いるかな？"
ROLE_CORRECTION = ("本当に そうかな？ お話では『{phrase}』と 書いて あるよ。\n"
                   "{divisor}は 何の 数に なって いるかな？")
ROLE_CORRECTION_FALLBACK = ("本当に そうかな？ お話を もう一度 読んで みよう。\n"
                            "{divisor}は 何の 数に なって いるかな？")
ROLE_NEXT = "じゃあ 次は、{divisor}を 何の 数に して みたい？"

# 判定結果（structure / unknown）から見た、除数の実際の役割（classify_role の分類と同じ語彙）。
# 倍で基準量を求める問題（unknown="base"）では除数は倍率なので、この3分類では役割を確定できない → None（訂正しない）
def expected_divisor_role(structure: str | None, unknown: str | None) -> str | None:
    if structure == "tobun":
        return "people"
    if structure == "hougan":
        return "per_one"
    if structure == "bai" and unknown in ("ratio", "rate"):
        return "base"
    return None


# 中（強度2）— 役割指定＋題材固定（要件定義 4-5 の「強度2」を一字一句）。
# {item} は直前の成立作問の物の名前、{unit} はその助数詞（ai_judge の item / unit）。
# {item} が取れないときは文頭の「{item}の お話は／{item}は」を「{ref_no}ばんの お話は」に置き換える（MID_MESSAGES_NOITEM）。
MID_MESSAGES = {
    "tobun": "{item}の お話は そのままで いいよ。{divisor}を「何人で 分けるか」の 数に して みよう。",
    "hougan": "{item}の お話は そのままで いいよ。{divisor}を「1人分の 数」に して みよう。",
    "bai": "{item}は そのままで いいよ。{divisor}を、もう 1人が もっている {item}の 数に して みよう。"
           "{dividend}{unit}と くらべると、どんな ことが 求められるかな？",
}
MID_MESSAGES_NOITEM = {
    "tobun": "{ref_no}ばんの お話は そのままで いいよ。{divisor}を「何人で 分けるか」の 数に して みよう。",
    "hougan": "{ref_no}ばんの お話は そのままで いいよ。{divisor}を「1人分の 数」に して みよう。",
    "bai": "{ref_no}ばんの お話は そのままで いいよ。{divisor}を、もう 1人が もっている {item}の 数に して みよう。"
           "{dividend}{unit}と くらべると、どんな ことが 求められるかな？",
}

# 強（強度3）— 場面文提示（要件定義 4-5 の「強度3」を一字一句）。
# 倍の人物名は学級の実在児童との重複を避けるため固定名にしない：{friend_name} は FRIEND_NAMES から
# セッションごとに周期的に選び、{friend_name_alt} は「お友だち」で統一する。
STRONG_MESSAGES = {
    "tobun": "「{item}が {dividend}{unit} あります。{divisor}人で 同じ 数ずつ 分けます。」 この あとに、求める 文を 書いて みよう。",
    "hougan": "「{item}が {dividend}{unit} あります。1人に {divisor}{unit}ずつ 分けます。」 この あとに、求める 文を 書いて みよう。",
    "bai": "「{friend_name}さんは {item}を {dividend}{unit}、{friend_name_alt}は {divisor}{unit} もって います。」"
           " この あとに、「何倍」を つかって 求める 文を 書いて みよう。",
}
FRIEND_NAMES = ("たろう", "はなこ")
FRIEND_NAME_ALT = "お友だち"
ITEM_FALLBACK = "もの"      # {item} が取れないとき（文頭以外の穴）
UNIT_FALLBACK = "こ"        # {unit} が取れないとき

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


def weak_message(ref_no: int, expression: str) -> str:
    """弱・ターン1：除数が何をあらわしているかを聞く。"""
    return _fill(ROLE_ASK, expression, ref_no=ref_no)


def role_correction_message(phrase: str | None, expression: str) -> str:
    """弱・訂正（1回だけ）：児童の問題文の除数の句を引用して問い返す。句が取れなければ引用なしの定型文。"""
    if phrase:
        return _fill(ROLE_CORRECTION, expression, phrase=phrase)
    return _fill(ROLE_CORRECTION_FALLBACK, expression)


def role_next_message(expression: str) -> str:
    """弱・ターン2：次の作問で除数を何の数にするか（予告）。"""
    return _fill(ROLE_NEXT, expression)


def mid_message(target: str, expression: str, ref_no: int, item: str | None, unit: str | None) -> str:
    """中：役割指定＋題材固定。item が取れなければ文頭を「{ref_no}ばんの お話は」にする。"""
    table = MID_MESSAGES if item else MID_MESSAGES_NOITEM
    return _fill(table[target], expression, ref_no=ref_no, item=item or ITEM_FALLBACK, unit=unit or UNIT_FALLBACK)


def friend_name(session_id: int | None) -> str:
    """倍の場面文の人物名。セッションごとに周期的に選ぶ（固定名にしない）。"""
    return FRIEND_NAMES[(session_id or 0) % len(FRIEND_NAMES)]


def strong_message(target: str, expression: str, ref_no: int, item: str | None, unit: str | None,
                   session_id: int | None = None) -> str:
    """強：場面文提示。ref_no は児童の成立問題のうち最新の表示番号（文言には使わないがログ・整合のため受け取る）。"""
    return _fill(STRONG_MESSAGES[target], expression, ref_no=ref_no,
                 item=item or ITEM_FALLBACK, unit=unit or UNIT_FALLBACK,
                 friend_name=friend_name(session_id), friend_name_alt=FRIEND_NAME_ALT)


def pick_unreached_structure(history: list[str]) -> str | None:
    """まだ到達していない構造をひとつ、決定論的な固定順で選ぶ。"""
    for s in STRUCTURE_ORDER:
        if s not in history:
            return s
    return None


# ===== 問いの文の抽出（弱・中の引用用。LLM） =====

_QUESTION_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
}

_QUESTION_PROMPT = """児童（小学4年生）が作った わり算の文章題から、問いの文（最後の疑問文）だけを取り出します。

- 問いの文は、何を求めるかを聞いている文（「〜は何こですか」「〜何人に配れますか」「〜の何倍ですか」など）。
- ふつうは文の最後にある。取り出すのはその1文だけ。場面の説明の文は含めない。
- 文中の数や言葉はそのまま使う。言い換えない・足さない。
- 出力は文節ごとに半角スペースで区切る（わかち書き）。例：「1人分は 何まいに なりますか」
- 末尾の句点「。」は付けない。疑問符「？」は元の文にあれば残す。
- 文字づかい：{KANJI_RULE}
- 問いの文が見つからない（場面だけ・途中で切れている・疑問文がない）ときは question を空文字 "" にする。

JSON のみを返す：{"question": "..."}"""


def format_quote(text: str | None) -> str | None:
    """引用文の整形：前後の空白と末尾の句点を落とし、空白を半角1つにそろえる。空なら None。"""
    if not text:
        return None
    q = str(text).strip().strip("「」")
    q = re.sub(r"[　\s]+", " ", q).strip()
    q = re.sub(r"[。．.]+$", "", q).strip()
    return q or None


def _squash(s: str) -> str:
    return re.sub(r"[　\s]", "", s or "")


def extract_question(problem_text: str, user_id: str | None = None) -> str | None:
    """成立した作問から問いの文（最後の疑問文）を取り出し、わかち書きで整形して返す。取れなければ None。

    元の文に無い言い換え（空白を除いて部分一致しない）は引用しない。"""
    def parse(response) -> str:
        if response.stop_reason == "max_tokens":
            raise ValueError("response truncated (max_tokens)")
        return str(extract_json(_text_from(response)).get("question") or "")

    try:
        raw, _meta = llm_call.call(
            user_id, parse,
            model=MODEL,
            max_tokens=200,
            thinking={"type": "disabled"},
            system=_QUESTION_PROMPT.replace("{KANJI_RULE}", KANJI_RULE),
            output_config={"format": _QUESTION_SCHEMA},
            messages=[{"role": "user", "content": f"文章題: {problem_text}"}],
        )
    except llm_call.LLMUnavailable as e:
        print(f"[ai_dialogue] extract_question failed after {e.retry_count} retries: {e}")
        return None
    q = format_quote(raw)
    if not q or len(q) > 60 or _squash(q) not in _squash(problem_text):
        return None
    return q


# ===== 除数の句の抽出（弱の訂正の引用用。LLM → 取れなければ除数を含む文） =====

_PHRASE_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"phrase": {"type": "string"}},
        "required": ["phrase"],
        "additionalProperties": False,
    },
}

_PHRASE_PROMPT = """児童（小学4年生）が作った わり算の文章題から、数 {divisor} が出てくる部分（{divisor} を含む短い句）だけを取り出します。

- 取り出すのは、{divisor} とその直後の言葉（助数詞・動詞）を含む短い句。例：「8人で 分けます」「8こずつ くばります」「8本の 何倍」
- 文中の数や言葉はそのまま使う。言い換えない・足さない・省かない（元の文に無い言葉を入れない）。
- 出力は文節ごとに半角スペースで区切る（わかち書き）。
- 末尾の句点「。」は付けない。
- 文字づかい：{KANJI_RULE}
- {divisor} が文中に無いときは phrase を空文字 "" にする。

JSON のみを返す：{"phrase": "..."}"""


def _sentence_with_number(problem_text: str, n: int) -> str | None:
    """LLM に頼らない予備：除数を含む文（。で区切る）。長すぎる（40字超）なら使わない。"""
    for sent in re.split(r"[。\n]", problem_text or ""):
        if re.search(rf"(?<![0-9]){n}(?![0-9])", sent):
            q = format_quote(sent)
            return q if q and len(q) <= 40 else None
    return None


def extract_divisor_phrase(problem_text: str, divisor: int, user_id: str | None = None) -> str | None:
    """成立した作問から、除数を含む短い句を取り出す。LLM の出力は元の文にあるもの（空白を除いて部分一致）だけ採用。
    取れなければ除数を含む文を丸ごと（短ければ）。それも無理なら None。"""
    def parse(response) -> str:
        if response.stop_reason == "max_tokens":
            raise ValueError("response truncated (max_tokens)")
        return str(extract_json(_text_from(response)).get("phrase") or "")

    try:
        raw, _meta = llm_call.call(
            user_id, parse,
            model=MODEL,
            max_tokens=200,
            thinking={"type": "disabled"},
            system=_PHRASE_PROMPT.replace("{KANJI_RULE}", KANJI_RULE).replace("{divisor}", str(divisor)),
            output_config={"format": _PHRASE_SCHEMA},
            messages=[{"role": "user", "content": f"文章題: {problem_text}"}],
        )
    except llm_call.LLMUnavailable as e:
        print(f"[ai_dialogue] extract_divisor_phrase failed after {e.retry_count} retries: {e}")
        raw = ""
    q = format_quote(raw)
    if q and len(q) <= 30 and re.search(rf"(?<![0-9]){divisor}(?![0-9])", q) and _squash(q) in _squash(problem_text):
        return q
    return _sentence_with_number(problem_text, divisor)


# ===== LLM（talk のみ） =====

OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "check": {"type": "string"},
            "message": {"type": "string"},
            "state": {"type": "string"},
            # 支援要求か（フェーズE）。文言と同じ1回の呼び出しで分類させる
            "is_help_request": {"type": "boolean"},
        },
        "required": ["check", "message", "state", "is_help_request"],
        "additionalProperties": False,
    },
}

_RAW_PROMPT = """あなたは小学4年生が「わり算のお話づくり（作問）」をするのを助ける先生です。
子どもは、式 {expression} になる問題を作っています。いまは、子どもが作問以外のこと
（つぶやき・感想・あいさつ・質問・確認・困りやいやがる気持ちの表明など）を書いてきたところです。

# この活動のねらい（先生が頭に置くこと）
同じ式 {expression} でも、わる数 {divisor} が場面の中で「何の数」かを変えると、求めるものが変わる。
わる数の役割は3通り：分ける相手の数（人数など）／1人分の数／比べる相手の量。
子どもに、求めるものがちがう問題を3つ作らせたい。うまくいく声かけは、わる数 {divisor} が
子どもの問題の中で何を指しているかに触れるものである。

# いまの状況
{situation}

# 話してよいこと（支援の強さ {strength} で決まる。これを超えない）
{allowed}

# どの強さでも
- 許可：子どもがすでに作った問題どうしの違い（または同じであること）を、わる数 {divisor} の役割で説明すること。
- 禁止：答え（数値 {quotient}）を言う。構造の名前（等分除・包含除・倍）を出す。問題を分類して名前で伝える。
  問いの文まで含む完成した問題文を渡す。「だれが・なにを・なんこ・どうする」のような穴うめの型を与える。
- 子どもの「同じ」「ちがう」という主張が判定と食い違っていたら、共感のために肯定しない。
  上の「いまの状況」の判定に基づいて、わる数の役割でどこが同じ／ちがうかを短く示す。
- 内容を確かめずに褒めない。「いいね」「すごい」だけの返事にしない。
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
手がかりは次の2つのうち、状況に合う一方だけを出す（両方は出さない）：
  (a) まだ1問も作れていない子には、身近な場面（教室・きゅうしょく・体育・家・お店 等）から
      {expression} に合いそうな具体物を思い出させる問いかけ。
  (b) すでに1問以上作れている子には、直前に作れた問題のわる数 {divisor} に目を向けさせる声かけ
      （上の「話してよいこと」の範囲で）。素材を変えさせたり、新しい場面を出したりしない。
  「これまでに作れた問題の数」が0なら (a)、1以上なら (b) を選ぶ。
「答えを教えて」と言われても答えは渡さない。ただし断るだけで終わらせず、必ず上の(a)(b)いずれかの
  手がかりにつなげること（断って終わりにしない）。
子どもが「{divisor}は何の数？」のように具体的に聞いてきたら、はぐらかさず、上の「話してよいこと」の範囲で答える
（強さ 0・1 なら問い返しで、2 以上なら先生から言ってよい）。同じ言い回しをくり返さない。

# 文字づかい
{KANJI_RULE}

# 支援要求の判定（is_help_request）
子どもの発話が「支援要求」かどうかも判定する。
- true：作問を進めるための助けを求めている発話。「ヒント」「わからない」「思いつかない」「どうすればいい」
  「なにを書けばいい」「むずかしい、できない」「答え教えて」など（困りの表明も含む）。
- false：雑談・感想・あいさつ（「あーなるほど」「やった」「こんにちは」）、
  自分の考えの確認や疑問への応答依頼（「これって足し算？」「これでいいの？」「8ってなんの数？」）、
  作問とは関係ない話。
判定は発話そのものから行い、声かけの内容には影響させない。

# 出力（JSONのみ。JSON以外の文字は出力しない）
{
  "check": "これから書く声かけが「話してよいこと」の範囲を超えていないかの自己確認（1文・ログ用）",
  "message": "子どもへの声かけ（1〜2文）",
  "state": "読み取った子どもの状態（ログ用・短く）",
  "is_help_request": true or false
}"""

# 強度ごとの「話してよいこと」。強度0・1は問い返しのみ、2は役割の指定＋題材固定、3は場面文まで
_ALLOWED_BY_STRENGTH = {
    0: """- わる数 {divisor} が子どもの問題の中で何を表しているかを、子どもに問い返してよい
  （例：「お話の 中で {divisor}と 書いた ところを 見て みよう。{divisor}は 何の 数だった？」）。
- ★先生から答え（役割）を言わない。子どもが「{divisor}は何の数？」「求めているものって何？」と聞いてきても、
  「{divisor}は 人数だね」「1人に配る数」のように役割を言わず、子ども自身の問題文の {divisor} のところに目を向けさせて問い返す。
  次の問題で {divisor} を何にするかも先生から指定しない（「{divisor}人で分ける」「{divisor}こずつ」「1人に配る数にして」は言わない）。
- 例外：子どもが「2つの問題は同じ／ちがう」と主張したときだけ、判定に基づいて、それぞれの問題で {divisor} が何を表しているかを
  短く示してよい（次に何にするかは言わない）。
- 「1つ分の 大きさ」「いくつ分」「何倍」「1人分」という言葉は使わない。
- 数と数の関係（「{dividend}こを{divisor}こずつ」のような言い方）は書かない。""",
    2: """- わる数 {divisor} を何の数にしてほしいかを、先生から直接言ってよい（目標があればそれに合わせる。
  例：「{divisor}を『1人分の 数』に して みよう」「{divisor}を『何人で 分けるか』の 数に して みよう」）。
- 「1つ分の 大きさ」「いくつ分」「何倍」という言葉を使ってよい。
- 題材は変えなくてよいと伝えてよい（「{item}の お話は そのままで いいよ」）。
- 場面文（お話の文そのもの）は渡さない。""",
    3: """- わる数 {divisor} を何の数にしてほしいかを、先生から直接言ってよい（目標があればそれに合わせる）。
- 「1つ分の 大きさ」「いくつ分」「何倍」という言葉を使ってよい。
- 題材は変えなくてよいと伝えてよい（「{item}の お話は そのままで いいよ」）。
- 場面文を渡してよい（「{item}が {dividend}{unit} あります。…」のような、求める文の手前までの文）。
  ただし求める文（問い）は書かない。子どもに書かせる。""",
}
_ALLOWED_BY_STRENGTH[1] = _ALLOWED_BY_STRENGTH[0]

# 「いまの状況」に書く、到達構造の説明（先生向け。子どもに見せる語ではない）
_PRODUCED_DESC = {
    "tobun": "「1つ分の 大きさ」を求める問題（わる数＝分ける相手の数）",
    "hougan": "「いくつ分」を求める問題（わる数＝1人分の数）",
    "bai": "「何倍」を求める問題（わる数＝比べる相手の量）",
}


def divisor_role_label(structure: str | None, unknown: str | None, divisor: int) -> str | None:
    """判定結果から、直前の成立作問でわる数が指していたものを日本語で。判定できなければ None。"""
    role = expected_divisor_role(structure, unknown)
    if role == "people":
        return "分ける相手の数（人数など）"
    if role == "per_one":
        return "1人分の数"
    if role == "base":
        return "比べる相手の量"
    if structure == "bai" and unknown == "base":
        return f"倍率（{divisor}倍）"
    return None


def _build_situation(history: list[str], ctx: dict, dividend: int, divisor: int) -> str:
    produced = [s for s in STRUCTURE_ORDER if s in (history or [])]
    unreached = [s for s in STRUCTURE_ORDER if s not in produced]
    lines = [f"- これまでに作れた問題の数: {len(produced)} / 3"]
    lines.append("- 作れた問題: " + ("、".join(_PRODUCED_DESC[s] for s in produced) if produced else "まだない"))
    lines.append("- まだ作れていない: " + ("、".join(_PRODUCED_DESC[s] for s in unreached) if unreached else "なし（3つそろった）"))
    target = ctx.get("target")
    lines.append("- 今の目標: " + (f"{_PRODUCED_DESC[target]}" if target in _PRODUCED_DESC else "なし"))
    problems = ctx.get("problems") or []
    if problems:
        lines.append("- 子どもが作れた問題（番号は子どもの画面の一覧と同じ）:")
        for i, pr in enumerate(problems, 1):
            role = pr.get("divisor_role") or "（判定できていない）"
            lines.append(f"  {i}ばん: 「{pr['text']}」 → わる数 {divisor} が表しているもの: {role}")
    last = ctx.get("last_problem") or {}
    if last.get("text"):
        role = last.get("divisor_role") or "（判定できていない）"
        lines.append(f"- 直前に作れた問題: 「{last['text']}」")
        lines.append(f"  この問題で わる数 {divisor} が表しているもの: {role}"
                     + (f"。物: {last['item']}" if last.get("item") else ""))
    else:
        lines.append("- 直前に作れた問題: まだない")
    lines.append(f"- 支援の強さ: {ctx.get('strength', 0)}（0=促し／1=弱／2=中／3=強）")
    return "\n".join(lines)


def _build_system(expression: str, history: list[str] | None = None, ctx: dict | None = None) -> str:
    dividend, divisor = parse_expression(expression)
    ctx = ctx or {}
    strength = int(ctx.get("strength") or 0)
    last = ctx.get("last_problem") or {}
    item = last.get("item") or ITEM_FALLBACK
    unit = last.get("unit") or UNIT_FALLBACK
    allowed = _ALLOWED_BY_STRENGTH.get(strength, _ALLOWED_BY_STRENGTH[0])
    return (_RAW_PROMPT
            .replace("{situation}", _build_situation(history or [], ctx, dividend, divisor))
            .replace("{allowed}", allowed)
            .replace("{KANJI_RULE}", KANJI_RULE)
            .replace("{expression}", f"{dividend} ÷ {divisor}")
            .replace("{dividend}", str(dividend))
            .replace("{divisor}", str(divisor))
            .replace("{quotient}", str(dividend // divisor))
            .replace("{strength}", str(strength))
            .replace("{item}", item)
            .replace("{unit}", unit))


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
# 全強度で禁止：構造名・分類の言い方
_BANNED_ALWAYS = ("等分除", "包含除", "倍の話", "倍のお話", "くらべる話", "分ける話", "構造")
# 強度0・1でだけ禁止：求める量の語・役割の語（2以上は先生から言ってよい）
_BANNED_UNKNOWN_WORDS = ("1つ分", "１つ分", "一つ分", "1人分", "１人分", "一人分", "いくつ分",
                         "何倍", "なんばい", "もとの大きさ", "もとにする", "1つあたり", "１つあたり",
                         "何人分", "何こ分", "さがしているもの", "さがすもの")
# 6章 語彙の統一：児童向け文言に出してはいけない言葉
_BANNED_VOCAB = ("種類", "たずね", "聞いていること", "ちがうことを聞く")
# talk では困り・ネガティブな表明を「やめたい」と誤読して活動の終了に誘導してしまう表現を禁止する
_BANNED_TALK = ("休", "今日はここまで", "終わりにし", "中断", "たくなったら", "ごめん", "やめても")
# 答え（商）を言ったとみなすパターン：商の数値の直後に助数詞や断定が続く（「3ばん」「3つ」は番号・個数なので除く）
_QUOTIENT_TAIL = r"(こ|人|本|まい|枚|cm|m|L|dL|回|ふくろ|箱|はこ|倍|ばい|だよ|だね|です|に なる|になる)"
_MAX_LEN = {0: 120, 1: 120, 2: 140, 3: 180}


def _has_both_numbers(text: str, dividend: int, divisor: int) -> bool:
    def present(n: int) -> bool:
        return re.search(rf"(?<![0-9]){n}(?![0-9])", text) is not None
    return present(dividend) and present(divisor)


def _says_quotient(text: str, quotient: int) -> bool:
    return re.search(rf"(?<![0-9]){quotient}\s*{_QUOTIENT_TAIL}", text) is not None


def violates_boundary(message: str, response_type: str, expression: str, strength: int = 0) -> str | None:
    """境界を破っていれば理由を返す（None なら合格）。境界は強度に依存する。"""
    dividend, divisor = parse_expression(expression)
    for w in _BANNED_ALWAYS:
        if w in message:
            return f"banned:{w}"
    for w in _BANNED_VOCAB:
        if w in message:
            return f"banned_vocab:{w}"
    if _says_quotient(message, dividend // divisor):
        return "quotient"
    if strength <= 1:
        for w in _BANNED_UNKNOWN_WORDS:
            if w in message:
                return f"banned_unknown:{w}"
        if response_type == "talk" and _has_both_numbers(message, dividend, divisor):
            return "both_numbers"
    if response_type == "talk":
        for w in _BANNED_TALK:
            if w in message:
                return f"banned_talk:{w}"
    if len(message) > _MAX_LEN.get(strength, 120):
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
                 user_id: str | None = None, context: dict | None = None) -> dict:
    """talk の文言を LLM に作らせる。

    context（main._handle_taiwa が渡す）:
      strength      … 現在の強度 0〜3（話してよいことの範囲がこれで決まる）
      target        … 現在の目標構造（tobun / hougan / bai / None）
      last_problem  … 直前の成立作問 {text, structure, unknown, divisor_role, item, unit}（無ければ None）
      problems      … 成立作問の一覧 [{text, divisor_role}]（問題どうしの違いを役割で説明するため）
    history は到達構造名のリスト（件数だけでなく構造名もプロンプトに書く）。"""
    ctx = context or {}
    strength = int(ctx.get("strength") or 0)
    user_content = f"""子どもの発話: {child_message}

直近のやりとりの履歴:
{_build_history(recent_turns)}"""

    system = _build_system(expression, history, ctx)

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
        reason = violates_boundary(message, response_type, expression, strength)
        if reason:
            print(f"[ai_dialogue] boundary violation ({response_type}, strength={strength}, {reason}): {message}")
            continue  # リトライ（2回目も違反なら定型文へ）
        return {"message": message, "state": result.get("state") or response_type,
                "is_help_request": bool(result.get("is_help_request")),
                "meta": {"retry_count": retry_total, "status": "retried_ok" if retry_total else "ok"}}

    # LLM が応答しなかった（分類もできていない）→ is_help_request は None（未判定）
    return {"message": _fallback(expression, has_problem=bool(history)),
            "state": f"{response_type}_fallback", "is_help_request": None,
            "meta": {"retry_count": retry_total, "status": "failed"}}


# ===== 入口 =====

def dialogue(child_message: str, input_kind: str, judge_result: dict | None,
             history: list[str], recent_turns: list[dict] | None,
             response_type: str, expression: str,
             prompt_strength: int | None = None, target: str | None = None,
             ref_no: int | None = None, item: str | None = None, unit: str | None = None,
             session_id: int | None = None, user_id: str | None = None,
             context: dict | None = None) -> dict:
    """児童向けの文言を組み立てる。

    戻り値: {"message", "state"}（LLM を呼んだ talk では "is_help_request" と "meta"（retry_count / status）も付く）
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
            return {"message": weak_message(ref_no or 1, expression), "state": "prompt_weak"}
        if prompt_strength == 2 and target:
            return {"message": mid_message(target, expression, ref_no or 1, item, unit),
                    "state": f"prompt_mid_{target}" + ("" if item else "_noitem")}
        if prompt_strength == 3 and target:
            return {"message": strong_message(target, expression, ref_no or 1, item, unit, session_id=session_id),
                    "state": f"prompt_strong_{target}" + ("" if item else "_noitem")}
        return {"message": FALLBACK_MESSAGE, "state": "prompt_invalid"}
    if response_type == "talk":
        return _llm_message(child_message, input_kind, judge_result, history, recent_turns,
                            "talk", expression, user_id=user_id, context=context)
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
