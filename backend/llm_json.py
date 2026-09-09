"""LLM応答からJSONを取り出す共通処理。

「JSON以外は出力しない」と指示しても、モデルは前置きの考察を書いてから JSON を始めることが
ある。そうなると max_tokens を前置きで使い切って JSON が途中で切れ、パースに失敗する。
判定（ai_judge）で失敗すると issue="error" となり、児童には「もう一度 おくってみてね」だけが
返る＝同じ文を何度送っても先に進めない、という事故になる。

対策は2つ：
  1. structured outputs（output_config.format）でJSONスキーマを渡し、前置きを書けなくする
     （このモデルは assistant prefill を受け付けないため、prefill は使えない）
  2. extract_json：それでも前後に文字が混ざった場合に、対応の取れた最初の { } を取り出す

スキーマにはモデルが考えるためのフィールド（判定なら reasoning、声かけなら check）を
先頭に置く。structured outputs はプロパティの順に生成されるので、考える場所を先に用意しないと
即答してしまい、判定の精度が落ちる。
"""

import json


def _strip_fence(text: str) -> str:
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    return text.strip()


def _first_object(text: str) -> str | None:
    """最初の対応の取れた { … } を返す（文字列リテラル内の波かっこは数えない）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(raw: str) -> dict:
    """LLMの出力（prefill 分を含む）から dict を取り出す。取り出せなければ ValueError。"""
    text = _strip_fence((raw or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    obj = _first_object(text)
    if obj is None:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(obj)
