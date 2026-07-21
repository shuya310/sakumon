"""入力の分岐判定（作問か対話か）。

judge を呼ぶ前に1回だけ、児童の入力が「作問（新しいお話）」か
「対話（質問・つぶやき・こまった）」かを軽量な1コールで分類する。
構造同定は行わない。失敗時は安全側（対話）に倒す。
"""

import json
import os
from dotenv import load_dotenv
import anthropic

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """あなたは、小学4年生が「わり算のお話づくり（作問）」をするアプリの入力仕分け係です。
児童が今おくった入力を、「作問」か「対話」かに分類することだけが仕事です。
構造の同定や正誤判定はしません。判定結果のJSONだけを返してください。
JSON以外の文字は一切出力しないでください。

## 分類の定義
- "sakumon"（作問）: 新しい文章題（お話）を作ろうとしている入力。
  例:「18このあめを3人でわけると1人なんこ？」「りんごが18こある。3こずつくばると何人にくばれる？」
- "taiwa"（対話）: 質問・つぶやき・こまった・あいさつなど、お話づくりそのものではない入力。
  例:「これでいいの？」「同じ話じゃないの？」「わからない」「図で見たい」「むずかしい」「つぎどうするの？」

## 迷ったときの目安
- 数量と問い（〜は何こ？ など）がそろった文章題の形なら "sakumon"。
- 短い質問・感想・困りごと・あいさつは "taiwa"。

## 返すJSON（この形式のみ）
{ "type": "sakumon" or "taiwa" }"""


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def classify(message: str, recent_turns: list[dict] | None = None) -> str:
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

    for attempt in range(2):  # 1回リトライ
        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=64,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            result = _parse(response.content[0].text)
            t = result.get("type")
            if t in ("sakumon", "taiwa"):
                return t
        except Exception as e:
            print(f"[ai_classify] classify failed (attempt {attempt + 1}): {type(e).__name__}: {e}")
    return "taiwa"  # 分類失敗時は安全側（対話）に倒す（judge に対話文が流れ込むのを防ぐ）
