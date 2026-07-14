import json
import os
from dotenv import load_dotenv
import anthropic

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ALL_STRUCTURES = {"tobun", "hougan", "bai"}

SYSTEM_PROMPT = """あなたは小学4年生の算数文章題を判定するAIです。
児童が入力した文章題を評価し、必ず以下のJSON形式だけで返答してください。
JSON以外の文字は一切出力しないでください。

## 評価する式
{expression}

## 3つの構造の定義
- tobun（等分除）: 全体を等しく分けたとき「1人あたり・1つあたりいくつ」を求める問題。例：「18このあめを3人でわけると1人なんこ？」
- hougan（包含除）: 全体から一定数ずつ分けると「なん人分・なん組」になるかを求める問題。例：「18このあめを3こずつくばるとなん人にくばれる？」
- bai（倍）: ある数が別の数の何倍かを求める問題。例：「18mは3mのなんばい？」

## valid=false（invalid）にすべき条件
以下のいずれかに該当する場合は必ず valid=false にすること。

【文章として成立していない】
- 誤字・脱字・文字化けなどで文章の意味が理解できない
- 単語の羅列や記号だけで文章になっていない
- 算数の文章題として必要な要素（誰が／何を／何こ／どうする／何を求めるか）が欠けている
  → message例：「もじがよめないよ。もう一かいかいてみてね」
              「もんだいができていないみたい。だれが・なにを・なんこ・どうする、をかいてみよう」

【式の数値・方向が合わない】
- 文章題を式にしたとき {expression} にならない（数値が違う、演算子が違うなど）
- bai（倍）構造の場合、（大きい数）÷（小さい数）の向きが逆になっている
  例：「山田さん（3こ）は佐藤さん（18こ）の何倍？」→ 3÷18 になるので invalid
  → message例：「そのおはなしだと、しきが18÷3にならないよ。どっちがおおきいかな？もう一かいかんがえてみてね」

## 判定ルール
1. valid: 上記invalid条件に該当せず、{expression} で解ける文章題として成立しているか
2. structure: tobun / hougan / bai のどれか。validがfalseなら "invalid"
3. is_new: structureが history に含まれていなければ true、含まれていれば false
4. stage と display_type の決め方:
   - validがfalse → stage=0, display_type="normal"
   - valid かつ is_new=true:
     - historyにstructureを加えると3つすべて揃う → display_type="clear", stage=0
     - そうでなければ → display_type="new_structure", stage=0
   - valid かつ is_new=false（既出）→ stageとdisplay_typeはリクエストで渡された current_stage を1上げる:
     - current_stage=0 → stage=1, display_type="hint1"
     - current_stage=1 → stage=2, display_type="hint2"
     - current_stage=2以上 → stage=3, display_type="hint3"
5. message: 児童へのやさしいメッセージ（ひらがな・カタカナ多め、小学4年生向け）
   - normal: なぜ成立していないか優しく説明し、どう書けばよいかを導く
   - new_structure: 新しい構造を発見したことを称賛する
   - clear: 3つ全部達成したことを大いに称賛する
   - hint1: 「おなじおはなしがつづいているね。ちがうわけかたを考えてみよう！」のような気づかせるメッセージ
   - hint2: 「たとえば、わけかたをかえてみたら？みたいに考えてみて」のような観点を与えるメッセージ（具体的な数字は出さない）
   - hint3: 穴埋め型（□を使った文型）だけを示す。「□にすきなかずをいれてかいてみよう」で終わる。数字・具体的な個数・人数・長さ等の数値を一切含めてはいけない。式の数字（18や3）も使用禁止。例文として具体的な問題文を出してはいけない。

## 返すJSONの形式（必ずこの形式のみ）
{
  "valid": true or false,
  "structure": "tobun" or "hougan" or "bai" or "invalid",
  "is_new": true or false,
  "stage": 0〜3の整数,
  "message": "児童へのメッセージ",
  "display_type": "normal" or "new_structure" or "clear" or "hint1" or "hint2" or "hint3"
}"""


def judge(message: str, expression: str, history: list[str], current_stage: int = 0) -> dict:
    prompt = SYSTEM_PROMPT.replace("{expression}", expression)

    user_content = f"""式: {expression}
これまでに到達した構造: {history}
現在の停滞ステージ: {current_stage}
児童の入力: {message}"""

    try:
        response = _client.messages.create(
            model="claude-sonnet-5",
            max_tokens=512,
            system=prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        # モデルがコードブロックで囲む場合を除去
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except Exception as e:
        result = {
            "valid": False,
            "structure": "invalid",
            "is_new": False,
            "stage": 0,
            "message": "判定中にエラーが起きました。もう一度ためしてみてください。",
            "display_type": "normal",
            "error": str(e),
        }

    # is_new をサーバー側で確定（モデルのハルシネーション防止）
    structure = result.get("structure", "invalid")
    if result.get("valid") and structure in ALL_STRUCTURES:
        result["is_new"] = structure not in history
        if not result["is_new"]:
            # 停滞ステージをサーバー側で確定
            next_stage = min(current_stage + 1, 3)
            result["stage"] = next_stage
            result["display_type"] = f"hint{next_stage}"
        else:
            new_history = set(history) | {structure}
            if new_history >= ALL_STRUCTURES:
                result["display_type"] = "clear"
            else:
                result["display_type"] = "new_structure"
            result["stage"] = 0
    else:
        result["is_new"] = False
        result["stage"] = 0
        result["display_type"] = "normal"

    return result
