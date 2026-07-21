"""作問文の構造同定。

責務は「作問された文章の構造同定」に限る。児童向けメッセージ・ヒント・
表示種別（display_type）・信号機の状態は生成しない（それらは main.py が
決定論的に計算し、児童向けの声かけは ai_dialogue が生成する）。
出力はJSONのみ。パース失敗時は1回リトライし、それでも失敗したら
issue="error" の構造結果を返す（メッセージ生成は呼び出し側に任せる）。
"""

import json
import os
from dotenv import load_dotenv
import anthropic

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-5"

ALL_STRUCTURES = {"tobun", "hougan", "bai"}

SYSTEM_PROMPT = """あなたは小学4年生の算数文章題の「構造」を同定するAIです。
児童が入力した文章題を読み、それが式 {expression} で解ける文章題として成立しているか、
成立しているならどの構造かだけを判定します。
児童向けのメッセージやヒントは書きません。判定結果のJSONだけを返してください。
JSON以外の文字は一切出力しないでください。

## 評価する式
{expression}

## 3つの構造の定義
- tobun（等分除）: 全体を等しく分けたとき「1人あたり・1つあたりいくつ」を求める問題。例:「18このあめを3人でわけると1人なんこ？」
- hougan（包含除）: 全体から一定数ずつ分けると「なん人分・なん組」になるかを求める問題。例:「18このあめを3こずつくばるとなん人にくばれる？」
- bai（倍）: ある数が別の数の何倍かを求める問題。例:「18mは3mのなんばい？」

## valid=false（成立していない）にすべき条件
- 誤字・脱字・文字化けなどで文章の意味が理解できない、または単語の羅列で文章になっていない
- 算数の文章題として必要な要素（誰が／何を／何こ／どうする／何を求めるか）が欠けている
- 文章を式にしたとき {expression} にならない（数値が違う・演算子が違う）
- bai（倍）で（大きい数）÷（小さい数）の向きが逆（例:「3こは18この何倍？」→ 3÷18 になる）

## issue（valid=false のときの理由コード。valid=true のときは null）
- "not_problem": 文章題として成立していない・要素が欠けている・意味が読めない
- "wrong_number": 式が {expression} にならない（数値・演算子がちがう）
- "reversed": 倍で大小の向きが逆になっている

## 返すJSONの形式（必ずこの形式のみ）
{
  "valid": true or false,
  "structure": "tobun" or "hougan" or "bai" or "invalid",
  "issue": null or "not_problem" or "wrong_number" or "reversed"
}"""


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def judge(message: str, expression: str) -> dict:
    """作問文の構造同定のみを行う。

    戻り値: {"valid": bool, "structure": str, "issue": str|None}
    パース失敗が続いた場合は {"valid": False, "structure": "invalid",
    "issue": "error", "error": ...} を返す。
    """
    system = SYSTEM_PROMPT.replace("{expression}", expression)
    user_content = f"式: {expression}\n児童の入力: {message}"

    last_err = None
    for attempt in range(2):  # 1回リトライ
        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=256,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            result = _parse(response.content[0].text)
            valid = bool(result.get("valid"))
            structure = result.get("structure", "invalid")
            if not valid or structure not in ALL_STRUCTURES:
                return {"valid": False, "structure": "invalid", "issue": result.get("issue") or "not_problem"}
            return {"valid": True, "structure": structure, "issue": None}
        except Exception as e:
            last_err = e
            print(f"[ai_judge] judge failed (attempt {attempt + 1}): {type(e).__name__}: {e}")

    return {"valid": False, "structure": "invalid", "issue": "error", "error": str(last_err)}
