import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

from config import DEFAULT_EXPRESSION_A, DEFAULT_EXPRESSION_B

_env_path = os.environ.get("DATABASE_PATH")
DB_PATH = Path(_env_path) if _env_path else Path(__file__).parent.parent / "data" / "sakumon.db"
if not DB_PATH.parent.exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

STRUCTURES = ("tobun", "hougan", "bai")
LEVEL_NAMES = ("level1", "level2", "level3", "level4")


def _now():
    """UTC ナイーブ（旧データと同じ書式 'YYYY-MM-DD HH:MM:SS.ffffff'）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                expression TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                message TEXT NOT NULL,
                response_json TEXT NOT NULL,
                structure TEXT,
                is_new INTEGER NOT NULL DEFAULT 0,
                input_type TEXT,
                stumble TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # グローバル設定（常に1行）
        con.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_phase INTEGER NOT NULL DEFAULT 1,
                expression_a TEXT NOT NULL DEFAULT '24 ÷ 4',
                expression_b TEXT NOT NULL DEFAULT '18 ÷ 3',
                run_id INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP
            )
        """)
        con.execute(
            """INSERT OR IGNORE INTO app_config (id, current_phase, expression_a, expression_b, run_id, updated_at)
               VALUES (1, 1, ?, ?, 1, ?)""",
            (DEFAULT_EXPRESSION_A, DEFAULT_EXPRESSION_B, _now()),
        )
        # フェーズ・式の変更履歴（分析時の時刻復元用）
        con.execute("""
            CREATE TABLE IF NOT EXISTS phase_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                phase INTEGER NOT NULL,
                expression_a TEXT,
                expression_b TEXT,
                note TEXT,
                changed_at TIMESTAMP
            )
        """)
        _migrate(con)


def _add_column(con, table: str, cols: list[str], name: str, ddl: str):
    if name not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate(con):
    """既存DBを壊さずにスキーマを更新する（データは保持）。"""
    cols = [row[1] for row in con.execute("PRAGMA table_info(chat_logs)").fetchall()]
    _add_column(con, "chat_logs", cols, "input_type", "TEXT")        # 作問/対話
    _add_column(con, "chat_logs", cols, "stumble", "TEXT")           # 旧つまづき（新規書き込みは停止）
    _add_column(con, "chat_logs", cols, "phase", "INTEGER")          # 1/2/3
    _add_column(con, "chat_logs", cols, "support_level", "TEXT")     # 全ターン必ず記録
    _add_column(con, "chat_logs", cols, "learner_state", "TEXT")     # S0/S1/S2/S3
    _add_column(con, "chat_logs", cols, "unknown", "TEXT")           # 求める量5区分
    _add_column(con, "chat_logs", cols, "issue", "TEXT")             # ai_judge の生 issue
    _add_column(con, "chat_logs", cols, "button_pressed", "TEXT")    # 押されたボタン値
    _add_column(con, "chat_logs", cols, "stall_count", "INTEGER")    # その時点の反復回数
    _add_column(con, "chat_logs", cols, "target_structure", "TEXT")  # 専用カラム化
    _add_column(con, "chat_logs", cols, "expression", "TEXT")        # そのターンの式

    scols = [row[1] for row in con.execute("PRAGMA table_info(sessions)").fetchall()]
    _add_column(con, "sessions", scols, "phase", "INTEGER NOT NULL DEFAULT 1")   # 作成時のフェーズ
    _add_column(con, "sessions", scols, "run_id", "INTEGER NOT NULL DEFAULT 0")  # 旧データは run 0（当日の run とは一致しない）


# ===== グローバル設定 =====

def get_config() -> dict:
    with _conn() as con:
        r = con.execute(
            "SELECT current_phase, expression_a, expression_b, run_id, updated_at FROM app_config WHERE id = 1"
        ).fetchone()
    return {
        "current_phase": r[0],
        "expression_a": r[1],
        "expression_b": r[2],
        "run_id": r[3],
        "updated_at": r[4],
    }


def expression_for_phase(cfg: dict, phase: int) -> str:
    return cfg["expression_b"] if phase == 3 else cfg["expression_a"]


def _record_phase_change(con, cfg_after: dict, note: str):
    con.execute(
        """INSERT INTO phase_changes (run_id, phase, expression_a, expression_b, note, changed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cfg_after["run_id"], cfg_after["current_phase"], cfg_after["expression_a"],
         cfg_after["expression_b"], note, _now()),
    )


def set_phase(phase: int) -> dict:
    with _conn() as con:
        con.execute("UPDATE app_config SET current_phase = ?, updated_at = ? WHERE id = 1", (phase, _now()))
    cfg = get_config()
    with _conn() as con:
        _record_phase_change(con, cfg, "set_phase")
    return cfg


def set_expressions(expression_a: str, expression_b: str) -> dict:
    with _conn() as con:
        con.execute(
            "UPDATE app_config SET expression_a = ?, expression_b = ?, updated_at = ? WHERE id = 1",
            (expression_a, expression_b, _now()),
        )
    cfg = get_config()
    with _conn() as con:
        _record_phase_change(con, cfg, "set_expressions")
    return cfg


def start_new_run() -> dict:
    """新しい回（run）を始める。run_id を進め、フェーズ1に戻す。データは消さない。"""
    with _conn() as con:
        con.execute(
            "UPDATE app_config SET run_id = run_id + 1, current_phase = 1, updated_at = ? WHERE id = 1",
            (_now(),),
        )
    cfg = get_config()
    with _conn() as con:
        _record_phase_change(con, cfg, "new_run")
    return cfg


# ===== セッション =====

def _phase_group(phase: int) -> tuple[int, ...]:
    """フェーズごとに別セッション。

    フェーズ1（事前・支援なし）で作った話はフェーズ1で完結させ、フェーズ2には引き継がない
    （＝フェーズ2の支援は、フェーズ2で打った内容だけを根拠にする）。
    """
    return (phase,)


def find_session(user_id: str, run_id: int, phase: int) -> int | None:
    group = _phase_group(phase)
    ph = ",".join("?" * len(group))
    with _conn() as con:
        row = con.execute(
            f"""SELECT session_id FROM sessions
                WHERE user_id = ? AND run_id = ? AND phase IN ({ph})
                ORDER BY session_id DESC LIMIT 1""",
            (user_id, run_id, *group),
        ).fetchone()
    return row[0] if row else None


def create_session(user_id: str, expression: str, phase: int = 1, run_id: int = 1) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO sessions (user_id, expression, phase, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, expression, phase, run_id, _now()),
        )
        return cur.lastrowid


def find_or_create_session(user_id: str, run_id: int, phase: int, expression: str) -> tuple[int, bool]:
    """現在の run・フェーズ群に対応するセッションを返す。無ければ作る。(session_id, created)"""
    sid = find_session(user_id, run_id, phase)
    if sid is not None:
        return sid, False
    return create_session(user_id, expression, phase, run_id), True


def get_session(session_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute(
            "SELECT session_id, user_id, expression, phase, run_id, created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"session_id": r[0], "user_id": r[1], "expression": r[2], "phase": r[3], "run_id": r[4], "created_at": r[5]}


def get_session_user(session_id: int) -> str | None:
    s = get_session(session_id)
    return s["user_id"] if s else None


def get_sessions(user_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT s.session_id, s.created_at, s.phase, s.run_id,
                      COUNT(CASE WHEN cl.structure IS NOT NULL THEN 1 END) as problem_count,
                      GROUP_CONCAT(DISTINCT CASE WHEN cl.is_new = 1 AND cl.structure IS NOT NULL THEN cl.structure END) as structures
               FROM sessions s
               LEFT JOIN chat_logs cl ON cl.session_id = s.session_id
               WHERE s.user_id = ?
               GROUP BY s.session_id
               ORDER BY s.created_at DESC""",
            (user_id,),
        ).fetchall()
    return [
        {
            "session_id": r[0],
            "created_at": r[1],
            "phase": r[2],
            "run_id": r[3],
            "problem_count": r[4] or 0,
            "structures": [s for s in (r[5] or "").split(",") if s],
        }
        for r in rows
    ]


# ===== ログ =====

def save_log(session_id: int, user_id: str, message: str, response_json: dict,
             structure: str | None, is_new: bool, input_type: str | None = None,
             phase: int | None = None, support_level: str | None = None,
             learner_state: str | None = None, unknown: str | None = None,
             issue: str | None = None, button_pressed: str | None = None,
             stall_count: int | None = None, target_structure: str | None = None,
             expression: str | None = None) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO chat_logs
               (session_id, user_id, message, response_json, structure, is_new, input_type, stumble,
                phase, support_level, learner_state, unknown, issue, button_pressed, stall_count,
                target_structure, expression, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, message,
             json.dumps(response_json, ensure_ascii=False),
             structure, int(is_new), input_type,
             phase, support_level, learner_state, unknown, issue, button_pressed, stall_count,
             target_structure, expression, _now()),
        )
        return cur.lastrowid


def _loads(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        return {}


def get_conversation(session_id: int) -> list[dict]:
    """全ターンを時系列で返す（再開時のチャット再描画用）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT message, response_json, input_type, phase, support_level FROM chat_logs
               WHERE session_id = ? ORDER BY id""",
            (session_id,),
        ).fetchall()
    turns = []
    for r in rows:
        resp = _loads(r[1])
        turns.append({
            "message": r[0],
            "ai_message": resp.get("message", ""),
            "display_type": resp.get("display_type", "normal"),
            "figure": resp.get("figure"),
            "tape_diagram": resp.get("tape_diagram"),
            "buttons": resp.get("buttons"),
            "input_type": r[2],
            "phase": r[3],
            "support_level": r[4],
        })
    return turns


def get_recent_turns(session_id: int, limit: int = 6) -> list[dict]:
    """直近のやりとりを古い順で返す（対話の文脈用）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT message, response_json, input_type, support_level FROM chat_logs
               WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    turns = []
    for r in reversed(rows):
        resp = _loads(r[1])
        turns.append({
            "child": r[0],
            "ai": resp.get("message", ""),
            "input_type": r[2],
            "support_level": r[3],
        })
    return turns


def get_last_turn(session_id: int) -> dict | None:
    """直近1ターン（水準2の問いへの応答かどうかの判定用）。"""
    with _conn() as con:
        r = con.execute(
            """SELECT support_level, input_type, response_json, phase FROM chat_logs
               WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    if not r:
        return None
    return {"support_level": r[0], "input_type": r[1], "response": _loads(r[2]), "phase": r[3]}


def get_history(session_id: int) -> list[str]:
    """到達済み構造（信号機）。セッションはフェーズごとなので、そのフェーズの到達だけを数える。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT structure FROM chat_logs
               WHERE session_id = ? AND is_new = 1 AND structure IS NOT NULL""",
            (session_id,),
        ).fetchall()
    return [row[0] for row in rows]


def get_valid_problems(session_id: int) -> list[dict]:
    """成立した作問を時系列で返す（is_new 問わず。水準1・2の産出一覧に使う）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT id, message, structure, unknown, is_new, phase FROM chat_logs
               WHERE session_id = ? AND structure IS NOT NULL
               ORDER BY id""",
            (session_id,),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "structure": r[2], "unknown": r[3],
             "is_new": bool(r[4]), "phase": r[5]} for r in rows]


def get_session_problems(session_id: int) -> list[dict]:
    return get_valid_problems(session_id)


def get_stall_count(session_id: int) -> int:
    """直近の新構造到達（is_new=1）より後に、成立作問で同じ構造をくり返した回数。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT is_new FROM chat_logs
               WHERE session_id = ? AND structure IS NOT NULL
               ORDER BY id DESC""",
            (session_id,),
        ).fetchall()
    count = 0
    for (is_new,) in rows:
        if is_new:
            break
        count += 1
    return count


def get_current_level(session_id: int, phase: int = 2) -> int:
    """現在の支援水準（0〜4）。

    指定フェーズのターンだけを新しい順に見て、最後に記録された level1〜4 の数字を返す。
    新構造の到達（is_new=1）が見つかったらそこでリセット＝0。フェーズが違う行に
    達したら（＝フェーズ2の開始より前）0。水準の遷移はフェーズ2の提出だけで数える。
    """
    with _conn() as con:
        rows = con.execute(
            """SELECT is_new, support_level, phase FROM chat_logs
               WHERE session_id = ? ORDER BY id DESC""",
            (session_id,),
        ).fetchall()
    for is_new, level, ph in rows:
        if ph != phase:
            return 0
        if is_new:
            return 0
        if level in LEVEL_NAMES:
            return int(level[-1])
    return 0


def get_recent_sakumon_validity(session_id: int, limit: int = 2) -> list[bool]:
    """直近の作問提出の成立/不成立を新しい順で返す（S0判定用。judgeエラー行は除く）。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT structure, issue FROM chat_logs
               WHERE session_id = ? AND input_type = 'sakumon'
                 AND (issue IS NULL OR issue != 'error')
               ORDER BY id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    return [r[0] is not None for r in rows]


def has_intentional_evidence(session_id: int) -> bool:
    """S3の暫定判定：フェーズ2で、水準1以上の声かけを受けた直後の作問提出で新構造が出たことがあるか。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT is_new, support_level, phase, input_type FROM chat_logs
               WHERE session_id = ? ORDER BY id""",
            (session_id,),
        ).fetchall()
    prev_level = None
    for is_new, level, ph, input_type in rows:
        if ph == 2 and is_new and prev_level in LEVEL_NAMES:
            return True
        prev_level = level
    return False


# ===== 管理者用 =====

def admin_get_all_students() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.user_id,
                   MAX(s.created_at) as last_login,
                   COUNT(DISTINCT CASE WHEN cl.is_new=1 AND cl.structure IS NOT NULL
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
            SELECT s.session_id, s.created_at, s.phase, s.run_id, s.expression,
                   COUNT(CASE WHEN cl.is_new=1 AND cl.structure IS NOT NULL THEN 1 END) as new_count,
                   COUNT(CASE WHEN cl.input_type='sakumon' OR (cl.input_type IS NULL AND cl.id IS NOT NULL)
                         THEN 1 END) as sakumon_count,
                   COUNT(CASE WHEN cl.input_type='taiwa' THEN 1 END) as taiwa_count,
                   GROUP_CONCAT(DISTINCT CASE WHEN cl.is_new=1 AND cl.structure IS NOT NULL
                         THEN cl.structure END) as structures
            FROM sessions s
            LEFT JOIN chat_logs cl ON cl.session_id = s.session_id
            WHERE s.user_id = ?
            GROUP BY s.session_id
            ORDER BY s.created_at DESC
        """, (user_id,)).fetchall()
    return [
        {
            "session_id": r[0],
            "created_at": r[1],
            "phase": r[2],
            "run_id": r[3],
            "expression": r[4],
            "new_count": r[5] or 0,
            "sakumon_count": r[6] or 0,
            "taiwa_count": r[7] or 0,
            "structures": [s for s in (r[8] or "").split(",") if s],
        }
        for r in rows
    ]


def admin_get_session_logs(session_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT id, message, response_json, structure, is_new, input_type, stumble, created_at,
                   phase, support_level, learner_state, unknown, issue, button_pressed, stall_count,
                   target_structure, expression
            FROM chat_logs WHERE session_id = ? ORDER BY id
        """, (session_id,)).fetchall()
    result = []
    for r in rows:
        resp = _loads(r[2])
        result.append({
            "id": r[0],
            "message": r[1],
            "ai_message": resp.get("message", ""),
            "display_type": resp.get("display_type", ""),
            "valid": resp.get("valid"),
            "structure": r[3],
            "is_new": bool(r[4]),
            "input_type": r[5],
            "stumble": r[6],
            "figure": resp.get("figure"),
            "tape_diagram": resp.get("tape_diagram"),
            "state": resp.get("state", ""),
            "created_at": r[7],
            "phase": r[8],
            "support_level": r[9],
            "learner_state": r[10],
            "unknown": r[11],
            "issue": r[12],
            "button_pressed": r[13],
            "stall_count": r[14],
            "target_structure": r[15],
            "expression": r[16],
        })
    return result


def admin_delete_session(session_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def admin_delete_log(log_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE id = ?", (log_id,))


CSV_FIELDS = [
    "user_id", "session_id", "run_id", "session_phase", "session_start",
    "log_id", "created_at", "phase", "expression", "input_type",
    "message", "ai_message", "display_type",
    "valid", "structure", "unknown", "issue", "is_new",
    "support_level", "learner_state", "stall_count", "button_pressed", "target_structure",
    "state", "stumble", "session_new_count",
]


def admin_get_all_logs_csv() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.user_id, cl.session_id, s.run_id, s.phase as session_phase, s.created_at as session_start,
                   cl.id, cl.created_at, cl.phase, cl.expression, cl.input_type,
                   cl.message, cl.response_json,
                   cl.structure, cl.unknown, cl.issue, cl.is_new,
                   cl.support_level, cl.learner_state, cl.stall_count, cl.button_pressed, cl.target_structure,
                   cl.stumble,
                   (SELECT COUNT(*) FROM chat_logs c2
                    WHERE c2.session_id = cl.session_id AND c2.is_new=1
                    AND c2.structure IS NOT NULL) as session_new_count
            FROM chat_logs cl
            JOIN sessions s ON s.session_id = cl.session_id
            ORDER BY s.user_id, cl.session_id, cl.id
        """).fetchall()
    result = []
    for r in rows:
        resp = _loads(r[11])
        valid = resp.get("valid")
        result.append({
            "user_id": r[0],
            "session_id": r[1],
            "run_id": r[2],
            "session_phase": r[3],
            "session_start": r[4],
            "log_id": r[5],
            "created_at": r[6],
            "phase": r[7] if r[7] is not None else "",
            "expression": r[8] or "",
            "input_type": r[9] or "",
            "message": r[10],
            "ai_message": resp.get("message", ""),
            "display_type": resp.get("display_type", ""),
            "valid": "" if valid is None else int(bool(valid)),
            "structure": r[12] or "",
            "unknown": r[13] or "",
            "issue": r[14] or "",
            "is_new": r[15],
            "support_level": r[16] or "",
            "learner_state": r[17] or "",
            "stall_count": "" if r[18] is None else r[18],
            "button_pressed": r[19] or "",
            "target_structure": r[20] or "",
            "state": resp.get("state", "") or "",
            "stumble": r[21] or "",
            "session_new_count": r[22],
        })
    return result


def admin_live_status(run_id: int, phase: int) -> list[dict]:
    """現在の run・フェーズにおける児童ごとの状態（教師用フェーズ画面）。"""
    group = _phase_group(phase)
    ph = ",".join("?" * len(group))
    with _conn() as con:
        sess = con.execute(
            f"""SELECT user_id, MAX(session_id) FROM sessions
                WHERE run_id = ? AND phase IN ({ph})
                GROUP BY user_id ORDER BY user_id""",
            (run_id, *group),
        ).fetchall()
        result = []
        for user_id, sid in sess:
            agg = con.execute(
                """SELECT
                     COUNT(CASE WHEN input_type='sakumon' AND phase = ? THEN 1 END),
                     COUNT(CASE WHEN structure IS NOT NULL AND phase = ? THEN 1 END),
                     MAX(created_at)
                   FROM chat_logs WHERE session_id = ?""",
                (phase, phase, sid),
            ).fetchone()
            structs = [r[0] for r in con.execute(
                """SELECT DISTINCT structure FROM chat_logs
                   WHERE session_id = ? AND is_new = 1 AND structure IS NOT NULL""", (sid,)).fetchall()]
            last = con.execute(
                """SELECT learner_state, support_level FROM chat_logs
                   WHERE session_id = ? ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
            result.append({
                "user_id": user_id,
                "session_id": sid,
                "submitted": agg[0] or 0,
                "valid": agg[1] or 0,
                "structures": structs,
                "learner_state": last[0] if last else None,
                "last_support_level": last[1] if last else None,
                "last_activity": agg[2],
            })
    for r in result:
        r["current_level"] = get_current_level(r["session_id"], 2)
    return result


def get_max_level(session_id: int, phase: int = 2) -> int:
    """そのセッション（指定フェーズ）で到達した最大の支援水準（0〜4）。単調増加。
    フロントの表示ゲート（水準1以上で産出一覧、水準3以上で信号機）に使う。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT support_level FROM chat_logs
               WHERE session_id = ? AND phase = ? AND support_level IN ('level1','level2','level3','level4')""",
            (session_id, phase),
        ).fetchall()
    return max((int(r[0][-1]) for r in rows), default=0)
