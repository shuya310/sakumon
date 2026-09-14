"""入力の分岐判定（作問か対話か）。

judge を呼ぶ前に1回だけ、児童の入力が「作問（新しいお話）」か
「対話（質問・つぶやき・こまった）」かを軽量な1コールで分類する。
構造同定は行わない。失敗時は安全側（対話）に倒す。
API 呼び出し（タイムアウト・リトライ・同時実行制御）は judge / dialogue と同じ llm_call を通す。
"""

import json

from config import MODEL, parse_expression
import llm_call

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
             user_id: str | None = None) -> str:
    """作問(sakumon) か 対話(taiwa) かを返す。失敗時は 'taiwa'。"""
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
            model=MODEL,
            max_tokens=64,
            thinking={"type": "disabled"},  # 分類に思考は不要（sonnet-5は既定でonのため明示off）
            system=_system(expression),
            messages=[{"role": "user", "content": user_content}],
        )
        return kind
    except llm_call.LLMUnavailable as e:
        print(f"[ai_classify] classify failed after {e.retry_count} retries: {e}")
        return "taiwa"  # 分類失敗時は安全側（対話）に倒す（judge に対話文が流れ込むのを防ぐ）
