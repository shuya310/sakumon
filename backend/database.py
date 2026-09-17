"""SQLite（sessions / chat_logs / app_config / phase_changes / selection_events / teacher_calls）。

- 時刻はすべて JST の 'YYYY-MM-DD HH:MM:SS' で保存する（旧実装は UTC で、書き出し時に日付がずれていた）。
- sessions は UNIQUE(user_id, phase)。1児童1フェーズ1セッションを DB レベルで保証する
  （旧実装では別タブで並行セッションが作られ、フェーズごとの状態が分裂していた）。
- 起動時にテーブルが無ければ作る。旧スキーマ（response_json 列の chat_logs 等）が残っていた場合は
  DROP せず `*_legacy_YYYYMMDD` に改名して退避し、新スキーマで作り直す（Render の永続ディスク上の
  DB をそのまま使えるようにするため）。
- is_new は「同じ児童・同じフェーズ」で初めて出た構造かどうか（フェーズスコープ）。
"""

import json
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
    declared_text  TEXT,                        -- 児童が宣言したときの言葉（画面上部にそのまま出す。システム指定なら NULL）
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
    divisor_phrase      TEXT,       -- 成立作問の除数を含む句（フェーズ2。称賛・弱の対比で引用する。extract_divisor_phrase）

    response_type       TEXT,
    prompt_strength     INTEGER,

    declared_structure  TEXT,
    declared_by         TEXT,
    declaration_met     INTEGER,

    self_label          TEXT,
    self_label_text     TEXT,
    self_label_match    INTEGER,

    -- 旧 v3 の弱ターン1（役割の宣言）。v3.1 で廃止。列は残すが書き込まない
    role_answer         TEXT,
    role_corrected      INTEGER,
    -- taiwa が支援要求（ヒント・わからない等）に分類されたか（LLM。未判定は NULL）
    is_help_request     INTEGER,

    produced_structures TEXT,
    stuck_count         INTEGER,
    miss_count          INTEGER,
    help_count          INTEGER,
    -- このターン後の強度（0〜3）と、このターンで強度を上げた原因（stuck / miss / help / talk / none）
    strength            INTEGER,
    strength_trigger    TEXT,
    -- このターンの AI の発話が指した目標構造（無ければ NULL）
    target_structure    TEXT,
    -- このターンの AI の発話のあと、サーバが児童の何を待つか（declaration / answer。待たないなら NULL。旧 v3 の role は残存）
    awaiting            TEXT,

    latency_ms          INTEGER,
    -- 判定の状態（作問行のみ。pending=未判定／done=完了／failed=規定回数リトライしても失敗）。
    -- フェーズ1・3は応答後に判定するので pending で保存してから埋める。フェーズ2は同期判定なので保存時に done/failed
    judge_status        TEXT,
    judged_at           TEXT,       -- 判定が終わった時刻（JST）

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

-- フェーズ1・3「作った お話を 見る」（3つえらぶ）のイベント。開くたび・送るたびに1行（分析では最後の submit を採用）
CREATE TABLE IF NOT EXISTS selection_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL,
    user_id        TEXT    NOT NULL,
    phase          INTEGER NOT NULL,
    kind           TEXT    NOT NULL,   -- open（選択画面を開いた）／ submit（選択を送った）
    log_ids        TEXT,               -- submit のとき、選んだ作問の chat_logs.log_id の JSON 配列 "[12,15,18]"
    created_at     TEXT    NOT NULL,   -- JST
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_selection_events_sess ON selection_events(session_id);

-- 教師が「声がけした」を押した時刻（児童画面には何も反映しない。同じフェーズで複数回押せる）
CREATE TABLE IF NOT EXISTS teacher_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phase          INTEGER NOT NULL,
    called_at      TEXT    NOT NULL    -- JST
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
                 ("help_count", "INTEGER NOT NULL DEFAULT 0"), ("strength", "INTEGER NOT NULL DEFAULT 0"),
                 ("declared_text", "TEXT")),
    "chat_logs": (("role_answer", "TEXT"), ("role_corrected", "INTEGER"), ("item", "TEXT"), ("unit", "TEXT"),
                  ("is_help_request", "INTEGER"), ("help_count", "INTEGER"), ("strength", "INTEGER"),
                  ("strength_trigger", "TEXT"), ("target_structure", "TEXT"), ("awaiting", "TEXT"),
                  ("judge_status", "TEXT"), ("judged_at", "TEXT"), ("divisor_phrase", "TEXT")),
}

JUDGE_STATUSES = ("pending", "done", "failed")


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
                      declared, declared_by, stuck_count, miss_count, help_count, strength, declared_text
               FROM sessions WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"session_id": r[0], "user_id": r[1], "phase": r[2], "expression": r[3],
            "parity_group": r[4], "session_start": r[5], "session_end": r[6],
            "declared": r[7], "declared_by": r[8], "stuck_count": r[9] or 0, "miss_count": r[10] or 0,
            "help_count": r[11] or 0, "strength": r[12] or 0, "declared_text": r[13]}


def set_state(session_id: int, *, declared: str | None, declared_by: str | None,
              stuck_count: int, miss_count: int, help_count: int, strength: int,
              declared_text: str | None = None):
    """状態機械の変数を保存する（3カウンタ＋強度＋予告）。declared_text は児童の宣言の言葉（システム指定・目標なしなら NULL）。"""
    with _conn() as con:
        con.execute(
            """UPDATE sessions SET declared = ?, declared_by = ?, declared_text = ?, stuck_count = ?, miss_count = ?,
                                   help_count = ?, strength = ?
               WHERE session_id = ?""",
            (declared, declared_by, declared_text if declared_by == "child" else None,
             stuck_count, miss_count, help_count, strength, session_id),
        )


# ===== ログ =====

def save_log(*, session_id: int, user_id: str, phase: int, expression: str,
             input_type: str, message: str | None, ai_message: str | None,
             valid: bool | None = None, structure: str | None = None, unknown: str | None = None,
             issue: str | None = None, is_new: bool | None = None,
             item: str | None = None, unit: str | None = None, divisor_phrase: str | None = None,
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
             target_structure: str | None = None, awaiting: str | None = None,
             latency_ms: int | None = None, judge_status: str | None = None) -> int:
    def b(v):
        return None if v is None else int(bool(v))
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO chat_logs
               (session_id, user_id, phase, expression, created_at,
                input_type, message, ai_message,
                valid, structure, unknown, issue, is_new, item, unit, divisor_phrase,
                response_type, prompt_strength,
                declared_structure, declared_by, declaration_met,
                self_label, self_label_text, self_label_match,
                role_answer, role_corrected, is_help_request,
                produced_structures, stuck_count, miss_count, help_count,
                strength, strength_trigger, target_structure, awaiting, latency_ms, judge_status, judged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, phase, expression, _now(),
             input_type, message, ai_message,
             b(valid), structure, unknown, issue, b(is_new), item, unit, divisor_phrase,
             response_type, prompt_strength,
             declared_structure, declared_by, b(declaration_met),
             self_label, self_label_text, b(self_label_match),
             role_answer, b(role_corrected), b(is_help_request),
             format_structures(produced_structures) if produced_structures is not None else None,
             stuck_count, miss_count, help_count, strength, strength_trigger, target_structure, awaiting, latency_ms,
             judge_status, _now() if judge_status in ("done", "failed") else None),
        )
        return cur.lastrowid


# ===== 応答後の判定（フェーズ1・3。judge_queue から呼ばれる） =====

def get_log_for_judge(log_id: int) -> dict | None:
    """判定キューが使う最小限の行情報。"""
    with _conn() as con:
        r = con.execute(
            "SELECT log_id, session_id, user_id, phase, expression, message, judge_status FROM chat_logs WHERE log_id = ?",
            (log_id,),
        ).fetchone()
    if not r:
        return None
    return {"log_id": r[0], "session_id": r[1], "user_id": r[2], "phase": r[3], "expression": r[4],
            "message": r[5], "judge_status": r[6]}


def get_produced_before(user_id: str, phase: int, log_id: int) -> list[str]:
    """その行より前に送られた（log_id が小さい）判定済み行から見た到達構造。
    応答後の判定で is_new を「送信順」で決めるために使う（判定の完了順ではなく）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT structure FROM chat_logs
               WHERE user_id = ? AND phase = ? AND valid = 1 AND structure IS NOT NULL AND log_id < ?""",
            (user_id, phase, log_id),
        ).fetchall()
    found = {r[0] for r in rows}
    return [s for s in STRUCTURES if s in found]


def set_judge_result(log_id: int, *, valid: bool | None, structure: str | None, unknown: str | None,
                     issue: str | None, is_new: bool | None, item: str | None, unit: str | None,
                     produced_structures: list[str] | None, judge_status: str):
    """応答後の判定の結果を書き込む（保存形式は同期判定と同じ列・同じ値）。"""
    def b(v):
        return None if v is None else int(bool(v))
    with _conn() as con:
        con.execute(
            """UPDATE chat_logs SET valid = ?, structure = ?, unknown = ?, issue = ?, is_new = ?, item = ?, unit = ?,
                                    produced_structures = ?, judge_status = ?, judged_at = ?
               WHERE log_id = ?""",
            (b(valid), structure, unknown, issue, b(is_new), item, unit,
             format_structures(produced_structures) if produced_structures is not None else None,
             judge_status, _now(), log_id),
        )


def get_unjudged_log_ids(statuses: tuple[str, ...] = ("pending", "failed")) -> list[dict]:
    """未判定・失敗の作問行（管理画面の再判定用）。送信順。"""
    marks = ",".join("?" * len(statuses))
    with _conn() as con:
        rows = con.execute(
            f"SELECT log_id, user_id FROM chat_logs WHERE judge_status IN ({marks}) ORDER BY log_id", statuses
        ).fetchall()
    return [{"log_id": r[0], "user_id": r[1]} for r in rows]


def count_judge_status() -> dict:
    """判定状態ごとの件数（管理画面の表示用）。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT judge_status, COUNT(*) FROM chat_logs WHERE judge_status IS NOT NULL GROUP BY judge_status"
        ).fetchall()
    out = {k: 0 for k in JUDGE_STATUSES}
    for k, n in rows:
        out[k] = n
    return out


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
                      response_type, prompt_strength, self_label, self_label_text, role_answer, role_corrected,
                      awaiting
               FROM chat_logs WHERE session_id = ? ORDER BY log_id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"input_type": r[0], "message": r[1], "ai_message": r[2], "phase": r[3],
            "valid": None if r[4] is None else bool(r[4]), "structure": r[5], "unknown": r[6],
            "issue": r[7], "response_type": r[8], "prompt_strength": r[9],
            "self_label": r[10], "self_label_text": r[11],
            "role_answer": r[12], "role_corrected": None if r[13] is None else bool(r[13]),
            "awaiting": r[14]}


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
            """SELECT log_id, message, structure, unknown, is_new, phase, item, unit, divisor_phrase FROM chat_logs
               WHERE session_id = ? AND valid = 1
               ORDER BY log_id""",
            (session_id,),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "structure": r[2], "unknown": r[3],
             "is_new": bool(r[4]), "phase": r[5], "item": r[6], "unit": r[7], "divisor_phrase": r[8]} for r in rows]


def count_taiwa_since_last_sakumon(session_id: int) -> int:
    """直前の作問行（input_type='sakumon'）より後の対話（taiwa）行の数。再送・予告は数えない。
    強度0で対話だけが続いて作問が来ない状態（trigger=talk）の検出に使う。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT input_type FROM chat_logs WHERE session_id = ? ORDER BY log_id DESC", (session_id,)
        ).fetchall()
    n = 0
    for (kind,) in rows:
        if kind == "sakumon":
            break
        if kind == "taiwa":
            n += 1
    return n


def get_max_prompt_strength(session_id: int) -> int:
    """そのセッションで出した予告支援の最大の強さ（0〜3）。フロントの表示ゲートに使う。"""
    with _conn() as con:
        r = con.execute(
            "SELECT MAX(prompt_strength) FROM chat_logs WHERE session_id = ?", (session_id,)
        ).fetchone()
    return int(r[0] or 0) if r else 0


# ===== 「作った お話を 見る」（フェーズ1・3の選択） =====

def get_sakumon_rows(session_id: int) -> list[dict]:
    """そのセッションで児童が送った作問（input_type='sakumon'）を送信順に返す。不成立・未判定・判定失敗も含む。
    対話（taiwa）・再送（resend）は含めない。表示番号は 1 始まり。判定結果は返さない。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT log_id, message FROM chat_logs WHERE session_id = ? AND input_type = 'sakumon' ORDER BY log_id",
            (session_id,),
        ).fetchall()
    return [{"no": i + 1, "log_id": r[0], "text": r[1]} for i, r in enumerate(rows)]


def add_selection_event(session_id: int, user_id: str, phase: int, kind: str, log_ids: list[int] | None = None) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO selection_events (session_id, user_id, phase, kind, log_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, phase, kind, json.dumps(list(log_ids)) if log_ids is not None else None, _now()),
        )
        return cur.lastrowid


def get_selection_state(session_id: int) -> dict:
    """そのセッションの、選択画面を初めて開いた時刻・最後の選択（log_ids と時刻）・送信回数。"""
    with _conn() as con:
        first_open = con.execute(
            "SELECT MIN(created_at) FROM selection_events WHERE session_id = ? AND kind = 'open'", (session_id,)
        ).fetchone()[0]
        last = con.execute(
            """SELECT log_ids, created_at FROM selection_events WHERE session_id = ? AND kind = 'submit'
               ORDER BY id DESC LIMIT 1""", (session_id,)
        ).fetchone()
        n_submit = con.execute(
            "SELECT COUNT(*) FROM selection_events WHERE session_id = ? AND kind = 'submit'", (session_id,)
        ).fetchone()[0]
    return {"first_open_at": first_open,
            "last_log_ids": json.loads(last[0]) if last and last[0] else None,
            "last_at": last[1] if last else None,
            "submit_count": n_submit}


def get_selection_events(session_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, kind, log_ids, created_at FROM selection_events WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [{"id": r[0], "kind": r[1], "log_ids": json.loads(r[2]) if r[2] else None, "created_at": r[3]} for r in rows]


def add_teacher_call(phase: int) -> dict:
    with _conn() as con:
        now = _now()
        con.execute("INSERT INTO teacher_calls (phase, called_at) VALUES (?, ?)", (phase, now))
    return {"phase": phase, "called_at": now}


def get_teacher_calls() -> dict:
    """フェーズごとの声がけ（最初・最後の時刻と回数）。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT phase, MIN(called_at), MAX(called_at), COUNT(*) FROM teacher_calls GROUP BY phase"
        ).fetchall()
    return {r[0]: {"first_at": r[1], "last_at": r[2], "count": r[3]} for r in rows}


def count_selection_submitted() -> dict:
    """フェーズごとの、選択を1回以上送信した児童の人数。"""
    with _conn() as con:
        rows = con.execute(
            "SELECT phase, COUNT(DISTINCT user_id) FROM selection_events WHERE kind = 'submit' GROUP BY phase"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


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
            "selection": get_selection_state(r[0]) if r[1] != 2 else None,
        }
        for r in rows
    ]


LOG_COLUMNS = [
    "log_id", "session_id", "user_id", "phase", "expression", "created_at",
    "input_type", "message", "ai_message",
    "valid", "structure", "unknown", "issue", "is_new", "item", "unit", "divisor_phrase",
    "response_type", "prompt_strength",
    "declared_structure", "declared_by", "declaration_met",
    "self_label", "self_label_text", "self_label_match",
    "role_answer", "role_corrected", "is_help_request",
    "produced_structures", "stuck_count", "miss_count", "help_count",
    "strength", "strength_trigger", "target_structure", "awaiting", "latency_ms",
    "judge_status", "judged_at",
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
        con.execute("DELETE FROM selection_events WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def admin_delete_log(log_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE log_id = ?", (log_id,))


CSV_FIELDS = [
    "user_id", "session_id", "parity_group", "session_start", "session_end",
    "log_id", "created_at", "phase", "expression", "input_type",
    "message", "ai_message",
    "valid", "structure", "unknown", "issue", "is_new", "item", "unit", "divisor_phrase",
    "response_type", "prompt_strength",
    "declared_structure", "declared_by", "declaration_met",
    "self_label", "self_label_text", "self_label_match",
    "role_answer", "role_corrected", "is_help_request",
    "produced_structures", "stuck_count", "miss_count", "help_count",
    "strength", "strength_trigger", "target_structure", "awaiting", "latency_ms",
    "judge_status", "judged_at",
    # フェーズ1・3の選択（セッション単位の値を各行に繰り返す）。selected は「この作問行が最終選択に含まれる」（作問行のみ 1/0）
    "selected", "selection_first_open_at", "selection_last_at", "selection_last_log_ids", "teacher_call_first_at",
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
    calls = get_teacher_calls()
    selections: dict[int, dict] = {}
    result = []
    for r in rows:
        d = dict(zip(LOG_COLUMNS, r[3:]))
        d.update({"parity_group": r[0], "session_start": r[1], "session_end": r[2]})
        sid = d["session_id"]
        if sid not in selections:
            selections[sid] = get_selection_state(sid) if d["phase"] != 2 else {}
        sel = selections[sid]
        last_ids = sel.get("last_log_ids")
        d["selected"] = ((1 if d["log_id"] in last_ids else 0) if (last_ids is not None and d["input_type"] == "sakumon")
                         else None)
        d["selection_first_open_at"] = sel.get("first_open_at")
        d["selection_last_at"] = sel.get("last_at")
        d["selection_last_log_ids"] = json.dumps(last_ids) if last_ids is not None else None
        d["teacher_call_first_at"] = (calls.get(d["phase"]) or {}).get("first_at") if d["phase"] != 2 else None
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
