"""入力の分岐判定（作問か対話か）。

judge を呼ぶ前に1回だけ、児童の入力が「作問（新しいお話）」か
「対話（質問・つぶやき・こまった）」かを軽量な1コールで分類する。
構造同定は行わない。失敗時は安全側（対話）に倒す。
API 呼び出し（タイムアウト・リトライ・同時実行制御）は judge / dialogue と同じ llm_call を通す。

作問か対話かの二択だけの軽いタスクなので、判定（ai_judge）・声かけ（ai_dialogue）より
求められる精度が低い。体感速度を優先し、既定では判定・声かけとは別モデル
（config.CLASSIFY_MODEL、既定 Haiku）を使う。分類の定義・プロンプトはモデルを変えても触らない。
"""

import json
import re

from config import CLASSIFY_MODEL, parse_expression
import llm_call

# AI が児童に問いを出した直後（awaiting）の入力を作問として扱う軽量ルール（9/17 修正①）：
# 数量（数字）が2つ以上あり、かつ問いの文で終わる完全な問題文だけを作問にする。それ以外は対話（LLM 分類は使わない）。
_NUMBER = re.compile(r"[0-9０-９]+|[一二三四五六七八九十百千]+")
_QUESTION_END = re.compile(r"(ですか|でしょうか|でしょう|ますか|なさい|かな|か|？|\?)[。．\s]*$")


_SCENE_VERB = re.compile(r"(あります|います|ました|ます|配|くば|分け|わけ|入れ|もって|持って)")
FRAGMENT_MAX_LEN = 12


def looks_like_problem(message: str) -> bool:
    """awaiting 中の入力が「完全な問題文」か：異なる数量が2つ以上 ＋ 問いの文で終わる。"""
    text = (message or "").strip()
    numbers = {n.translate(str.maketrans("０１２３４５６７８９", "0123456789")) for n in _NUMBER.findall(text)}
    return len(numbers) >= 2 and _QUESTION_END.search(text) is not None


def looks_like_fragment(message: str) -> bool:
    """form（不成立への形式支援）の直後の入力が、作問ではなく「指摘への返事」の断片か（例「一人4こ」「ハコの数」）：
    短く（FRAGMENT_MAX_LEN 字以下）、問いの文で終わらず、場面の動詞も無い。これに当たれば LLM 分類を通さず対話にする。"""
    text = (message or "").strip()
    return (len(text) <= FRAGMENT_MAX_LEN and _QUESTION_END.search(text) is None
            and _SCENE_VERB.search(text) is None)


SYSTEM_PROMPT = """あなたは、小学4年生が「わり算のお話づくり（作問）」をするアプリの入力仕分け係です。
児童が今おくった入力を、「作問」か「対話」かに分類することだけが仕事です。
構造の同定や正誤判定はしません。判定結果のJSONだけを返してください。
JSON以外の文字は一切出力しないでください。

## 分類の定義
- "sakumon"（作問）: 新しい文章題（お話）を作ろうとしている入力。
  例:「{dividend}このあめを{divisor}人でわけると1人なんこ？」「りんごが{dividend}こある。{divisor}こずつくばると何人にくばれる？」
  ※文章題として成立していなくても（数がちがう・問いがない・途中で切れている）、お話を作ろうとしていれば "sakumon"。
- "taiwa"（対話）: 質問・つぶやき・こまった・あいさつなど、お話づくりそのものではない入力。
  例:「これでいいの？」「同じ話じゃないの？」「わからない」「図で見たい」「むずかしい」「つぎどうするの？」

## 迷ったときの目安
- 数量と問い（〜は何こ？ など）がそろった文章題の形なら "sakumon"。
- 短い質問・感想・困りごと・あいさつは "taiwa"。

## 返すJSON（この形式のみ）
{ "type": "sakumon" or "taiwa" }"""


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


def _system(expression: str) -> str:
    dividend, divisor = parse_expression(expression)
    return SYSTEM_PROMPT.replace("{dividend}", str(dividend)).replace("{divisor}", str(divisor))


def classify(message: str, recent_turns: list[dict] | None = None, expression: str = "24 ÷ 4",
             user_id: str | None = None, fallback: str = "taiwa") -> str:
    """作問(sakumon) か 対話(taiwa) かを返す。失敗時は fallback（既定 'taiwa'）。

    フェーズ1・3は fallback='sakumon' で呼ぶ：判定は応答後（judge_queue）なので、分類できなくても作問として
    pending で残しておけば、API 復旧後に管理画面の「再判定」で判定できる（taiwa に倒すと判定されないまま失われる）。"""
    context = ""
    if recent_turns:
        lines = []
        for t in recent_turns[-4:]:
            if t.get("child"):
                lines.append(f"子ども: {t['child']}")
            if t.get("ai"):
                lines.append(f"先生: {t['ai']}")
        if lines:
            context = "これまでのやりとり:\n" + "\n".join(lines) + "\n\n"
    user_content = f"{context}今の入力: {message}"

    def parse(response) -> str:
        t = _parse(_text_from(response)).get("type")
        if t not in ("sakumon", "taiwa"):
            raise ValueError(f"unexpected type: {t!r}")
        return t

    try:
        kind, _meta = llm_call.call(
            user_id, parse,
            model=CLASSIFY_MODEL,
            max_tokens=64,
            thinking={"type": "disabled"},  # 分類に思考は不要（Haiku は既定offだが明示しておく）
            system=_system(expression),
            messages=[{"role": "user", "content": user_content}],
        )
        return kind
    except llm_call.LLMUnavailable as e:
        print(f"[ai_classify] classify failed after {e.retry_count} retries: {e} → {fallback}")
        return fallback  # フェーズ2は安全側（対話）に倒す（judge に対話文が流れ込むのを防ぐ）


# ===== 予告（自由記述）の分類 =====
# 仕様 v3.1 3-4：弱の問い「じゃあ 次は、{divisor}を どんな ふうに 使った お話に する？」への児童の答えを
# tobun / hougan / bai / unknown に分ける。判断基準は「わる数をどう使う（何を求める）と書いているか」だけ。
# 題材のみ（「りんごの問題」）・「わからない」・質問で返した・無関係・判断不能は unknown。

DECLARATION_PROMPT = """あなたは、小学4年生が「わり算のお話づくり（作問）」をするアプリの仕分け係です。
児童は、式 {dividend} ÷ {divisor} になる文章題を作っています。
「じゃあ 次は、{divisor}を どんな ふうに 使った お話に する？」と聞かれ、それに答えました。
その答えを次の4つに分類します。分類結果のJSONだけを返してください。JSON以外の文字は一切出力しないでください。

- "tobun"：{divisor} を「分ける相手の数」（{divisor}人で分ける・{divisor}つの班・{divisor}チーム・{divisor}ふくろに分ける）として使う、
  または 1人分・1つ分の大きさを求めると書いている
- "hougan"：{divisor} を「1人分・1つ分の数」（{divisor}こずつ・1人に{divisor}こ・{divisor}まいずつ配る）として使う、
  または 何人に配れるか・いくつ分・まとまりの数を求めると書いている
- "bai"：{divisor} を「くらべる相手の量」（{divisor}こと くらべる・{divisor}この何倍）として使う、または 何倍かを求めると書いている
- "unknown"：題材だけ（「りんごの問題」「クラスの話」）・「わからない」・質問で返している（「どういうこと？」）・無関係・判断できない

## 返すJSON（この形式のみ）
{ "structure": "tobun" or "hougan" or "bai" or "unknown" }"""

DECLARATION_TYPES = ("tobun", "hougan", "bai", "unknown")


def classify_declaration(text: str, user_id: str | None = None, expression: str = "24 ÷ 4") -> str:
    """予告の文を tobun / hougan / bai / unknown に分類する。失敗時は 'unknown'。"""
    dividend, divisor = parse_expression(expression)
    def parse(response) -> str:
        t = _parse(_text_from(response)).get("structure")
        if t not in DECLARATION_TYPES:
            raise ValueError(f"unexpected structure: {t!r}")
        return t

    try:
        kind, _meta = llm_call.call(
            user_id, parse,
            model=CLASSIFY_MODEL,
            max_tokens=64,
            thinking={"type": "disabled"},
            system=DECLARATION_PROMPT.replace("{dividend}", str(dividend)).replace("{divisor}", str(divisor)),
            messages=[{"role": "user", "content": f"児童の答え: {text}"}],
        )
        return kind
    except llm_call.LLMUnavailable as e:
        print(f"[ai_classify] classify_declaration failed after {e.retry_count} retries: {e}")
        return "unknown"  # 分類できなければ予告なし扱い（再質問もしない：仕様 3-4）
