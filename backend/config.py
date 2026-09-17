"""アプリ全体の設定（環境変数・式のパース・機能フラグ）。

式の割り当て表（EXPRESSION_ASSIGNMENT。奇偶 × フェーズ）と、式のパースをここに置く。
実際にセッションで使う式は sessions.expression（開始時にこの表から決めて固定。管理画面から個別上書き可）。
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# ---- 機能フラグ ----
# テープ図・構造図は今回すべて無効化する（数量関係そのものを図で渡してしまうため）。
# コードは残し、このフラグで呼ばれない状態にする。
ENABLE_FIGURES = False

# ---- LLM ----
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# classify にも判定・声かけと同じ MODEL を使う。Haiku を試したところ「倍」構造の文
# （「〜は…の何倍ですか」）が12.5%誤分類され、対話扱いになって一覧・構造判定から漏れる
# 実害が確認されたため、既定は MODEL（sonnet）に統一した。環境変数で個別に上書きは可能。
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", MODEL)

# API 呼び出しの信頼性（llm_call.py）。いずれも環境変数で上書きできる。
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))   # 1回あたりのタイムアウト
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))               # 再試行回数（初回を除く）
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "30"))      # API への同時リクエスト上限

# ---- 管理者認証（HTTP Basic）----
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# ---- CORS ----
_default_origins = "http://localhost:8000,http://127.0.0.1:8000,https://sakumon.onrender.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]

# ---- 式のカウンターバランス（仕様 v2 4章）。出席番号の奇偶 × フェーズ。式の設定は**ここ1箇所だけ**
# （管理画面の表・「式を変更」の選択肢・テストの期待値・app_config の初期値はすべてここから引く）。
# フェーズ2（支援あり）は 9/1 の紙の調査（ai_judge の検証に使った 162 問）と同じ 24÷4。
# 事前・事後は 21÷3 と 30÷5 を奇偶で入れ替える（商は 7 / 6 / 6。24÷4 と除数・商を入れ替えた関係にならない式を選んだ。
# 当初候補の「18 と 3」は 9/1 事前テスト大問3で使われているため 21÷3 にした。README「式の設定」参照）。
EXPRESSION_ASSIGNMENT = {
    "odd":  {1: "21÷3", 2: "24÷4", 3: "30÷5"},
    "even": {1: "30÷5", 2: "24÷4", 3: "21÷3"},
}

# ---- 児童側ポーリング（秒）----
CONFIG_POLL_SECONDS = 5
ONLINE_WINDOW_SECONDS = 15  # 最終ポーリングからこの秒数以内なら「接続中」

_EXPR_RE = re.compile(r"^\s*(\d+)\s*[÷/／]\s*(\d+)\s*$")


def parse_expression(expr: str) -> tuple[int, int]:
    """'24 ÷ 4' → (24, 4)。形式不正・わり切れない式は ValueError。"""
    m = _EXPR_RE.match(expr or "")
    if not m:
        raise ValueError("式は「24 ÷ 4」のように「整数 ÷ 整数」で入力してください")
    dividend, divisor = int(m.group(1)), int(m.group(2))
    if dividend <= 0 or divisor <= 0:
        raise ValueError("被除数・除数は1以上にしてください")
    if dividend % divisor != 0:
        raise ValueError(f"{dividend} は {divisor} でわり切れません")
    return dividend, divisor


def normalize_expression(expr: str) -> str:
    """表示用に '24 ÷ 4' の形に正規化する。"""
    a, b = parse_expression(expr)
    return f"{a} ÷ {b}"


# 管理画面「式を変更」の選択肢（設定表に現れる式の集合。表示順は被除数の昇順）
EXPRESSION_CHOICES = sorted({normalize_expression(e) for d in EXPRESSION_ASSIGNMENT.values() for e in d.values()},
                            key=parse_expression)

# app_config.expression_a / expression_b の初期値（列は残っているが未使用。設定表から導出し、直書きしない）
DEFAULT_EXPRESSION_A = normalize_expression(EXPRESSION_ASSIGNMENT["odd"][1])
DEFAULT_EXPRESSION_B = normalize_expression(EXPRESSION_ASSIGNMENT["even"][1])
