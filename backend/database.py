"""SQLite（sessions / chat_logs / app_config / phase_changes）。

- 時刻はすべて JST の 'YYYY-MM-DD HH:MM:SS' で保存する（旧実装は UTC で、書き出し時に日付がずれていた）。
- sessions は UNIQUE(user_id, phase)。1児童1フェーズ1セッションを DB レベルで保証する
  （旧実装では別タブで並行セッションが作られ、フェーズごとの状態が分裂していた）。
- 起動時にテーブルが無ければ作る。旧スキーマ（response_json 列の chat_logs 等）が残っていた場合は
  DROP せず `*_legacy_YYYYMMDD` に改名して退避し、新スキーマで作り直す（Render の永続ディスク上の
  DB をそのまま使えるようにするため）。
- is_new は「同じ児童・同じフェーズ」で初めて出た構造かどうか（フェーズスコープ）。
"""

import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

from config import DEFAULT_EXPRESSION_A, DEFAULT_EXPRESSION_B

_env_path = os.environ.get("DATABASE_PATH")
DB_PATH = Path(_env_path) if _env_path else Path(__file__).parent.parent / "data" / "sakumon.db"
if not DB_PATH.parent.exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

STRUCTURES = ("tobun", "hougan", "bai")
JST = timezone(timedelta(hours=9))

RESPONSE_TYPES = ("form", "praise", "prompt", "talk", "done", "error")


def _now() -> str:
    """JST の 'YYYY-MM-DD HH:MM:SS'（授業時間帯と一致する）。"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


# ===== スキーマ =====

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT    NOT NULL,
    phase          INTEGER NOT NULL,
    expression     TEXT    NOT NULL,
    parity_group   TEXT    NOT NULL,
    session_start  TEXT    NOT NULL,
    session_end    TEXT,
    -- 支援の状態機械（フェーズスコープ＝セッションごと）
    declared       TEXT,
    declared_by    TEXT,
    stuck_count    INTEGER NOT NULL DEFAULT 0,
    miss_count     INTEGER NOT NULL DEFAULT 0,
    help_count     INTEGER NOT NULL DEFAULT 0,   -- taiwa が支援要求に分類された回数
    strength       INTEGER NOT NULL DEFAULT 0    -- 現在の強度 0=促し／1=弱／2=中／3=強（状態として保持）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_user_phase ON sessions(user_id, phase);

CREATE TABLE IF NOT EXISTS chat_logs (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL,
    user_id             TEXT    NOT NULL,
    phase               INTEGER NOT NULL,
    expression          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,

    input_type          TEXT    NOT NULL,
    message             TEXT,
    ai_message          TEXT,

    valid               INTEGER,
    structure           TEXT,
    unknown             TEXT,
    issue               TEXT,
    is_new              INTEGER,
    item                TEXT,       -- 判定が読み取った物の名前（中・強の文言の {物}）
    unit                TEXT,       -- 判定が読み取った助数詞（{unit}）

    response_type       TEXT,
    prompt_strength     INTEGER,

    declared_structure  TEXT,
    declared_by         TEXT,
    declaration_met     INTEGER,

    self_label          TEXT,
    self_label_text     TEXT,
    self_label_match    INTEGER,

    -- 弱・ターン1（役割の宣言）：児童の答えの分類（訂正前の生の値）と、訂正を出したか
    role_answer         TEXT,
    role_corrected      INTEGER,
    -- taiwa が支援要求（ヒント・わからない等）に分類されたか（LLM。未判定は NULL）
    is_help_request     INTEGER,

    produced_structures TEXT,
    stuck_count         INTEGER,
    miss_count          INTEGER,
    help_count          INTEGER,
    -- このターン後の強度（0〜3）と、このターンで強度を上げた原因（stuck / miss / help / none）
    strength            INTEGER,
    strength_trigger    TEXT,
    -- このターンの AI の発話が指した目標構造（無ければ NULL）
    target_structure    TEXT,

    latency_ms          INTEGER,

    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_logs_user ON chat_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_logs_sess ON chat_logs(session_id);

CREATE TABLE IF NOT EXISTS app_config (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    current_phase  INTEGER NOT NULL DEFAULT 1,
    expression_a   TEXT    NOT NULL,
    expression_b   TEXT    NOT NULL,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS phase_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phase          INTEGER NOT NULL,
    expression_a   TEXT,
    expression_b   TEXT,
    note           TEXT,
    changed_at     TEXT
);
"""

# 「旧スキーマである」と判定する目印（この列があるのは 9/14 以前のテーブルだけ）
_LEGACY_MARKERS = {
    "chat_logs": "response_json",
    "sessions": "run_id",
    "app_config": "run_id",
    "phase_changes": "run_id",
}


def _columns(con, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()]


def _archive_legacy_tables(con):
    """旧スキーマのテーブルを `*_legacy_YYYYMMDD` に改名して退避する（DROP しない）。"""
    stamp = datetime.now(JST).strftime("%Y%m%d")
    for table, marker in _LEGACY_MARKERS.items():
        cols = _columns(con, table)
        if cols and marker in cols:
            name = f"{table}_legacy_{stamp}"
            n = 1
            while con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
                n += 1
                name = f"{table}_legacy_{stamp}_{n}"
            con.execute(f"ALTER TABLE {table} RENAME TO {name}")
            print(f"[database] legacy table archived: {table} -> {name}")


_MIGRATIONS = {
    "sessions": (("declared", "TEXT"), ("declared_by", "TEXT"),
                 ("stuck_count", "INTEGER NOT NULL DEFAULT 0"), ("miss_count", "INTEGER NOT NULL DEFAULT 0"),
                 ("help_count", "INTEGER NOT NULL DEFAULT 0"), ("strength", "INTEGER NOT NULL DEFAULT 0")),
    "chat_logs": (("role_answer", "TEXT"), ("role_corrected", "INTEGER"), ("item", "TEXT"), ("unit", "TEXT"),
                  ("is_help_request", "INTEGER"), ("help_count", "INTEGER"), ("strength", "INTEGER"),
                  ("strength_trigger", "TEXT"), ("target_structure", "TEXT")),
}


def _migrate(con):
    """新スキーマ以降の列追加（既存 DB を壊さない）。SCHEMA にも同じ列を書いておくこと。"""
    for table, columns in _MIGRATIONS.items():
        cols = _columns(con, table)
        for name, ddl in columns:
            if cols and name not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with _conn() as con:
        _archive_legacy_tables(con)
        con.executescript(SCHEMA)
        _migrate(con)
        con.execute(
            """INSERT OR IGNORE INTO app_config (id, current_phase, expression_a, expression_b, updated_at)
               VALUES (1, 1, ?, ?, ?)""",
            (DEFAULT_EXPRESSION_A, DEFAULT_EXPRESSION_B, _now()),
        )


# ===== グローバル設定 =====

def get_config() -> dict:
    with _conn() as con:
        r = con.execute(
            "SELECT current_phase, expression_a, expression_b, updated_at FROM app_config WHERE id = 1"
        ).fetchone()
    return {"current_phase": r[0], "expression_a": r[1], "expression_b": r[2], "updated_at": r[3]}


def _record_phase_change(con, cfg_after: dict, note: str):
    con.execute(
        """INSERT INTO phase_changes (phase, expression_a, expression_b, note, changed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (cfg_after["current_phase"], cfg_after["expression_a"], cfg_after["expression_b"], note, _now()),
    )


def set_phase(phase: int) -> dict:
    """フェーズを切り替える。それ以外のフェーズで開いているセッションは終了時刻を打つ
    （児童が入り直せば find_or_create_session で再開＝終了時刻は消える）。"""
    with _conn() as con:
        now = _now()
        con.execute("UPDATE app_config SET current_phase = ?, updated_at = ? WHERE id = 1", (phase, now))
        con.execute("UPDATE sessions SET session_end = ? WHERE phase != ? AND session_end IS NULL", (now, phase))
    cfg = get_config()
    with _conn() as con:
        _record_phase_change(con, cfg, "set_phase")
    return cfg


def set_session_expression(session_id: int, expression: str):
    """管理画面からの個別の式の上書き（当日のトラブル対応用。仕様 v2 4章）。"""
    with _conn() as con:
        con.execute("UPDATE sessions SET expression = ? WHERE session_id = ?", (expression, session_id))


# ===== セッション =====

def parity_group_of(user_id: str) -> str:
    """出席番号の奇偶（式のカウンターバランス用）。"""
    try:
        return "odd" if int(user_id) % 2 else "even"
    except ValueError:
        return "odd"


def find_or_create_session(user_id: str, phase: int, expression: str) -> tuple[int, bool]:
    """その児童・そのフェーズのセッションを返す。無ければ作る。(session_id, created)

    UNIQUE(user_id, phase) があるので、別タブから同時に呼ばれても2つ目は作られない
    （INSERT OR IGNORE）。既存セッションの再開時は session_end を消す。
    """
    with _conn() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO sessions (user_id, phase, expression, parity_group, session_start)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, phase, expression, parity_group_of(user_id), _now()),
        )
        created = cur.rowcount == 1
        sid = con.execute(
            "SELECT session_id FROM sessions WHERE user_id = ? AND phase = ?", (user_id, phase)
        ).fetchone()[0]
        if not created:
            con.execute("UPDATE sessions SET session_end = NULL WHERE session_id = ?", (sid,))
    return sid, created


def end_session(session_id: int):
    """ログアウト時。終了時刻を打つ（入り直せば消える）。"""
    with _conn() as con:
        con.execute("UPDATE sessions SET session_end = ? WHERE session_id = ? AND session_end IS NULL",
                    (_now(), session_id))


def get_session(session_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute(
            """SELECT session_id, user_id, phase, expression, parity_group, session_start, session_end,
                      declared, declared_by, stuck_count, miss_count, help_count, strength
               FROM sessions WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"session_id": r[0], "user_id": r[1], "phase": r[2], "expression": r[3],
            "parity_group": r[4], "session_start": r[5], "session_end": r[6],
            "declared": r[7], "declared_by": r[8], "stuck_count": r[9] or 0, "miss_count": r[10] or 0,
            "help_count": r[11] or 0, "strength": r[12] or 0}


def set_state(session_id: int, *, declared: str | None, declared_by: str | None,
              stuck_count: int, miss_count: int, help_count: int, strength: int):
    """状態機械の変数を保存する（3カウンタ＋強度＋予告）。"""
    with _conn() as con:
        con.execute(
            """UPDATE sessions SET declared = ?, declared_by = ?, stuck_count = ?, miss_count = ?,
                                   help_count = ?, strength = ?
               WHERE session_id = ?""",
            (declared, declared_by, stuck_count, miss_count, help_count, strength, session_id),
        )


# ===== ログ =====

def save_log(*, session_id: int, user_id: str, phase: int, expression: str,
             input_type: str, message: str | None, ai_message: str | None,
             valid: bool | None = None, structure: str | None = None, unknown: str | None = None,
             issue: str | None = None, is_new: bool | None = None,
             item: str | None = None, unit: str | None = None,
             response_type: str | None = None, prompt_strength: int | None = None,
             declared_structure: str | None = None, declared_by: str | None = None,
             declaration_met: bool | None = None,
             self_label: str | None = None, self_label_text: str | None = None,
             self_label_match: bool | None = None,
             role_answer: str | None = None, role_corrected: bool | None = None,
             is_help_request: bool | None = None,
             produced_structures: list[str] | None = None,
             stuck_count: int | None = None, miss_count: int | None = None, help_count: int | None = None,
             strength: int | None = None, strength_trigger: str | None = None,
             target_structure: str | None = None,
             latency_ms: int | None = None) -> int:
    def b(v):
        return None if v is None else int(bool(v))
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO chat_logs
               (session_id, user_id, phase, expression, created_at,
                input_type, message, ai_message,
                valid, structure, unknown, issue, is_new, item, unit,
                response_type, prompt_strength,
                declared_structure, declared_by, declaration_met,
                self_label, self_label_text, self_label_match,
                role_answer, role_corrected, is_help_request,
                produced_structures, stuck_count, miss_count, help_count,
                strength, strength_trigger, target_structure, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, phase, expression, _now(),
             input_type, message, ai_message,
             b(valid), structure, unknown, issue, b(is_new), item, unit,
             response_type, prompt_strength,
             declared_structure, declared_by, b(declaration_met),
             self_label, self_label_text, b(self_label_match),
             role_answer, b(role_corrected), b(is_help_request),
             format_structures(produced_structures) if produced_structures is not None else None,
             stuck_count, miss_count, help_count, strength, strength_trigger, target_structure, latency_ms),
        )
        return cur.lastrowid


def format_structures(structures) -> str:
    """到達構造集合の保存形式 "tobun,hougan"（固定順）。"""
    s = set(structures or ())
    return ",".join(x for x in STRUCTURES if x in s)


def get_conversation(session_id: int) -> list[dict]:
    """全ターンを時系列で返す（再開時のチャット再描画用）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT message, ai_message, input_type, phase, response_type, is_new, prompt_strength
               FROM chat_logs WHERE session_id = ? ORDER BY log_id""",
            (session_id,),
        ).fetchall()
    return [{"message": r[0], "ai_message": r[1], "input_type": r[2], "phase": r[3],
             "response_type": r[4], "is_new": bool(r[5]), "prompt_strength": r[6]} for r in rows]


def get_recent_turns(session_id: int, limit: int = 6) -> list[dict]:
    """直近のやりとりを古い順で返す（対話の文脈用）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT message, ai_message, input_type, response_type FROM chat_logs
               WHERE session_id = ? ORDER BY log_id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    return [{"child": r[0], "ai": r[1] or "", "input_type": r[2], "response_type": r[3]}
            for r in reversed(rows)]


def get_last_turn(session_id: int) -> dict | None:
    """直近1ターン（同一本文の再送検出用）。"""
    with _conn() as con:
        r = con.execute(
            """SELECT input_type, message, ai_message, phase, valid, structure, unknown, issue,
                      response_type, prompt_strength, self_label, self_label_text, role_answer, role_corrected
               FROM chat_logs WHERE session_id = ? ORDER BY log_id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"input_type": r[0], "message": r[1], "ai_message": r[2], "phase": r[3],
            "valid": None if r[4] is None else bool(r[4]), "structure": r[5], "unknown": r[6],
            "issue": r[7], "response_type": r[8], "prompt_strength": r[9],
            "self_label": r[10], "self_label_text": r[11],
            "role_answer": r[12], "role_corrected": None if r[13] is None else bool(r[13])}


def get_produced(user_id: str, phase: int) -> list[str]:
    """到達済み構造（フェーズスコープ）。同じ児童・同じフェーズなら別セッションでも数える。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT structure FROM chat_logs
               WHERE user_id = ? AND phase = ? AND valid = 1 AND structure IS NOT NULL""",
            (user_id, phase),
        ).fetchall()
    found = {r[0] for r in rows}
    return [s for s in STRUCTURES if s in found]


def get_valid_problems(session_id: int) -> list[dict]:
    """成立した作問を時系列で返す（児童の「つくった お話」一覧用。表示番号＝この並びの 1 始まり）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT log_id, message, structure, unknown, is_new, phase, item, unit FROM chat_logs
               WHERE session_id = ? AND valid = 1
               ORDER BY log_id""",
            (session_id,),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "structure": r[2], "unknown": r[3],
             "is_new": bool(r[4]), "phase": r[5], "item": r[6], "unit": r[7]} for r in rows]


def get_max_prompt_strength(session_id: int) -> int:
    """そのセッションで出した予告支援の最大の強さ（0〜3）。フロントの表示ゲートに使う。"""
    with _conn() as con:
        r = con.execute(
            "SELECT MAX(prompt_strength) FROM chat_logs WHERE session_id = ?", (session_id,)
        ).fetchone()
    return int(r[0] or 0) if r else 0


# ===== 管理者用 =====

def admin_get_all_students() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.user_id,
                   MAX(s.session_start) as last_login,
                   COUNT(DISTINCT CASE WHEN cl.valid = 1 AND cl.structure IS NOT NULL
                         THEN cl.structure END) as structure_count,
                   COUNT(DISTINCT s.session_id) as session_count
            FROM sessions s
            LEFT JOIN chat_logs cl ON cl.session_id = s.session_id
            GROUP BY s.user_id
            ORDER BY s.user_id
        """).fetchall()
    return [{"user_id": r[0], "last_login": r[1], "structure_count": r[2] or 0, "session_count": r[3]}
            for r in rows]


def admin_get_student_sessions(user_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.session_id, s.phase, s.expression, s.parity_group, s.session_start, s.session_end,
                   s.declared, s.declared_by, s.stuck_count, s.miss_count, s.help_count, s.strength,
                   COUNT(CASE WHEN cl.is_new = 1 THEN 1 END) as new_count,
                   COUNT(CASE WHEN cl.input_type = 'sakumon' THEN 1 END) as sakumon_count,
                   COUNT(CASE WHEN cl.input_type = 'taiwa' THEN 1 END) as taiwa_count,
                   GROUP_CONCAT(DISTINCT CASE WHEN cl.valid = 1 THEN cl.structure END) as structures
            FROM sessions s
            LEFT JOIN chat_logs cl ON cl.session_id = s.session_id
            WHERE s.user_id = ?
            GROUP BY s.session_id
            ORDER BY s.phase
        """, (user_id,)).fetchall()
    return [
        {
            "session_id": r[0], "phase": r[1], "expression": r[2], "parity_group": r[3],
            "session_start": r[4], "session_end": r[5],
            "declared": r[6], "declared_by": r[7], "stuck_count": r[8] or 0, "miss_count": r[9] or 0,
            "help_count": r[10] or 0, "strength": r[11] or 0,
            "new_count": r[12] or 0, "sakumon_count": r[13] or 0, "taiwa_count": r[14] or 0,
            "structures": [s for s in (r[15] or "").split(",") if s],
        }
        for r in rows
    ]


LOG_COLUMNS = [
    "log_id", "session_id", "user_id", "phase", "expression", "created_at",
    "input_type", "message", "ai_message",
    "valid", "structure", "unknown", "issue", "is_new", "item", "unit",
    "response_type", "prompt_strength",
    "declared_structure", "declared_by", "declaration_met",
    "self_label", "self_label_text", "self_label_match",
    "role_answer", "role_corrected", "is_help_request",
    "produced_structures", "stuck_count", "miss_count", "help_count",
    "strength", "strength_trigger", "target_structure", "latency_ms",
]
_BOOL_COLUMNS = ("valid", "is_new", "declaration_met", "self_label_match", "role_corrected", "is_help_request")


def _row_to_log(r) -> dict:
    d = dict(zip(LOG_COLUMNS, r))
    for k in _BOOL_COLUMNS:
        if d[k] is not None:
            d[k] = bool(d[k])
    return d


def admin_get_session_logs(session_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            f"SELECT {', '.join(LOG_COLUMNS)} FROM chat_logs WHERE session_id = ? ORDER BY log_id",
            (session_id,),
        ).fetchall()
    return [_row_to_log(r) for r in rows]


def admin_delete_session(session_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def admin_delete_log(log_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE log_id = ?", (log_id,))


CSV_FIELDS = [
    "user_id", "session_id", "parity_group", "session_start", "session_end",
    "log_id", "created_at", "phase", "expression", "input_type",
    "message", "ai_message",
    "valid", "structure", "unknown", "issue", "is_new", "item", "unit",
    "response_type", "prompt_strength",
    "declared_structure", "declared_by", "declaration_met",
    "self_label", "self_label_text", "self_label_match",
    "role_answer", "role_corrected", "is_help_request",
    "produced_structures", "stuck_count", "miss_count", "help_count",
    "strength", "strength_trigger", "target_structure", "latency_ms",
]


def admin_get_all_logs_csv() -> list[dict]:
    """全ログを CSV 用の dict で返す。NULL は空文字、真偽値は 1/0。"""
    with _conn() as con:
        rows = con.execute(f"""
            SELECT s.parity_group, s.session_start, s.session_end,
                   {', '.join('cl.' + c for c in LOG_COLUMNS)}
            FROM chat_logs cl
            JOIN sessions s ON s.session_id = cl.session_id
            ORDER BY s.user_id, cl.session_id, cl.log_id
        """).fetchall()
    result = []
    for r in rows:
        d = dict(zip(LOG_COLUMNS, r[3:]))
        d.update({"parity_group": r[0], "session_start": r[1], "session_end": r[2]})
        result.append({k: ("" if d.get(k) is None else d[k]) for k in CSV_FIELDS})
    return result


def admin_live_status(phase: int) -> list[dict]:
    """現在のフェーズにおける児童ごとの状態（教師用フェーズ画面）。"""
    with _conn() as con:
        sess = con.execute(
            """SELECT user_id, session_id, declared, declared_by, stuck_count, miss_count, help_count, strength
               FROM sessions WHERE phase = ? ORDER BY user_id""", (phase,)
        ).fetchall()
        result = []
        for user_id, sid, declared, declared_by, stuck, miss, help_count, strength in sess:
            agg = con.execute(
                """SELECT COUNT(CASE WHEN input_type = 'sakumon' THEN 1 END),
                          COUNT(CASE WHEN valid = 1 THEN 1 END),
                          MAX(created_at)
                   FROM chat_logs WHERE session_id = ?""",
                (sid,),
            ).fetchone()
            last = con.execute(
                """SELECT response_type, prompt_strength, produced_structures
                   FROM chat_logs WHERE session_id = ? ORDER BY log_id DESC LIMIT 1""", (sid,)).fetchone()
            result.append({
                "user_id": user_id,
                "session_id": sid,
                "submitted": agg[0] or 0,
                "valid": agg[1] or 0,
                "structures": [s for s in ((last[2] if last else "") or "").split(",") if s],
                "last_response_type": last[0] if last else None,
                "last_prompt_strength": last[1] if last else None,
                "declared": declared,
                "declared_by": declared_by,
                "stuck_count": stuck or 0,
                "miss_count": miss or 0,
                "help_count": help_count or 0,
                "strength": strength or 0,
                "last_activity": agg[2],
            })
    return result
