import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent / "data" / "sakumon.db"


def _conn():
    return sqlite3.connect(DB_PATH)


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def create_session(user_id: str, expression: str) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO sessions (user_id, expression, created_at) VALUES (?, ?, ?)",
            (user_id, expression, datetime.utcnow()),
        )
        return cur.lastrowid


def get_sessions(user_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT s.session_id, s.created_at,
                      COUNT(CASE WHEN cl.is_new = 1 AND cl.structure IS NOT NULL THEN 1 END) as new_count,
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
            "problem_count": r[2] or 0,
            "structures": [s for s in (r[3] or "").split(",") if s],
        }
        for r in rows
    ]


def save_log(session_id: int, user_id: str, message: str, response_json: dict, structure: str | None, is_new: bool):
    with _conn() as con:
        con.execute(
            """INSERT INTO chat_logs
               (session_id, user_id, message, response_json, structure, is_new, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, message,
             json.dumps(response_json, ensure_ascii=False),
             structure, int(is_new), datetime.utcnow()),
        )


def get_history(session_id: int) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT structure FROM chat_logs
               WHERE session_id = ? AND is_new = 1 AND structure IS NOT NULL""",
            (session_id,),
        ).fetchall()
    return [row[0] for row in rows]


def get_session_problems(session_id: int) -> list[dict]:
    """valid=true の問題を時系列で返す（is_new 問わず）"""
    with _conn() as con:
        rows = con.execute(
            """SELECT message, structure, is_new FROM chat_logs
               WHERE session_id = ? AND structure IS NOT NULL
               ORDER BY id""",
            (session_id,),
        ).fetchall()
    return [{"text": r[0], "structure": r[1], "is_new": bool(r[2])} for r in rows]


def get_current_stage(session_id: int) -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT response_json FROM chat_logs WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if not row:
        return 0
    try:
        last = json.loads(row[0])
        if not last.get("is_new", True):
            return last.get("stage", 0)
    except Exception:
        pass
    return 0


def get_session_user(session_id: int) -> str | None:
    with _conn() as con:
        row = con.execute(
            "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row[0] if row else None


# ===== 管理者用 =====

def admin_get_all_students() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.user_id,
                   MAX(s.created_at) as last_login,
                   COUNT(DISTINCT CASE WHEN cl.is_new=1 AND cl.structure IS NOT NULL
                         THEN cl.structure END) as structure_count
            FROM sessions s
            LEFT JOIN chat_logs cl ON cl.session_id = s.session_id
            GROUP BY s.user_id
            ORDER BY s.user_id
        """).fetchall()
    return [{"user_id": r[0], "last_login": r[1], "structure_count": r[2] or 0}
            for r in rows]


def admin_get_student_sessions(user_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.session_id, s.created_at,
                   COUNT(CASE WHEN cl.is_new=1 AND cl.structure IS NOT NULL THEN 1 END) as new_count,
                   COUNT(cl.id) as total_count,
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
            "new_count": r[2] or 0,
            "total_count": r[3] or 0,
            "structures": [s for s in (r[4] or "").split(",") if s],
        }
        for r in rows
    ]


def admin_get_session_logs(session_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT id, message, response_json, structure, is_new, created_at
            FROM chat_logs WHERE session_id = ? ORDER BY id
        """, (session_id,)).fetchall()
    result = []
    for r in rows:
        try:
            resp = json.loads(r[2])
        except Exception:
            resp = {}
        result.append({
            "id": r[0],
            "message": r[1],
            "ai_message": resp.get("message", ""),
            "display_type": resp.get("display_type", ""),
            "structure": r[3],
            "is_new": bool(r[4]),
            "created_at": r[5],
        })
    return result


def admin_delete_session(session_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def admin_delete_log(log_id: int):
    with _conn() as con:
        con.execute("DELETE FROM chat_logs WHERE id = ?", (log_id,))


def admin_get_all_logs_csv() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.user_id, cl.session_id, s.created_at as session_start,
                   cl.created_at, cl.message, cl.response_json,
                   cl.structure, cl.is_new,
                   (SELECT COUNT(*) FROM chat_logs c2
                    WHERE c2.session_id = cl.session_id AND c2.is_new=1
                    AND c2.structure IS NOT NULL) as session_new_count
            FROM chat_logs cl
            JOIN sessions s ON s.session_id = cl.session_id
            ORDER BY s.user_id, cl.session_id, cl.id
        """).fetchall()
    result = []
    for r in rows:
        try:
            resp = json.loads(r[5])
        except Exception:
            resp = {}
        result.append({
            "user_id": r[0],
            "session_id": r[1],
            "session_start": r[2],
            "created_at": r[3],
            "message": r[4],
            "ai_message": resp.get("message", ""),
            "structure": r[6] or "",
            "is_new": r[7],
            "session_new_count": r[8],
        })
    return result
