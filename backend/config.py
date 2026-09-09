"""アプリ全体の設定（環境変数・式のパース・機能フラグ）。

式（被除数・除数）はここでパースする。ハードコードは置かず、実際の式は
database.get_config()（app_config テーブル）から取得する。
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

# ---- 管理者認証（HTTP Basic）----
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

# ---- CORS ----
_default_origins = "http://localhost:8000,http://127.0.0.1:8000,https://sakumon.onrender.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]

# ---- 式の既定値（app_config 初期化時のみ使う）----
DEFAULT_EXPRESSION_A = "24 ÷ 4"
DEFAULT_EXPRESSION_B = "18 ÷ 3"

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
