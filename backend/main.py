import csv
import io
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import database
import ai_judge

EXPRESSION = "18 ÷ 3"
USER_ID_PATTERN = re.compile(r"^[0-9a-z]{2}$")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    database.init_db()


# --- Models ---

class LoginRequest(BaseModel):
    user_id: str

class NewSessionRequest(BaseModel):
    user_id: str

class ResumeSessionRequest(BaseModel):
    session_id: int

class JudgeRequest(BaseModel):
    session_id: int
    message: str



# --- Routes ---

@app.post("/api/login")
def login(req: LoginRequest):
    if not USER_ID_PATTERN.match(req.user_id):
        raise HTTPException(status_code=400, detail="学籍番号は半角英数字2桁で入力してください")
    sessions = database.get_sessions(req.user_id)
    return {"user_id": req.user_id, "sessions": sessions}


@app.post("/api/session/new")
def new_session(req: NewSessionRequest):
    if not USER_ID_PATTERN.match(req.user_id):
        raise HTTPException(status_code=400, detail="invalid user_id")
    session_id = database.create_session(req.user_id, EXPRESSION)
    return {"session_id": session_id, "expression": EXPRESSION}


@app.post("/api/session/resume")
def resume_session(req: ResumeSessionRequest):
    user_id = database.get_session_user(req.session_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="session not found")
    history = database.get_history(req.session_id)
    problems = database.get_session_problems(req.session_id)
    return {
        "session_id": req.session_id,
        "expression": EXPRESSION,
        "history": history,
        "problems": problems,
    }


@app.post("/api/judge")
def judge(req: JudgeRequest):
    user_id = database.get_session_user(req.session_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="session not found")

    history = database.get_history(req.session_id)
    current_stage = database.get_current_stage(req.session_id)

    result = ai_judge.judge(req.message, EXPRESSION, history, current_stage)

    database.save_log(
        session_id=req.session_id,
        user_id=user_id,
        message=req.message,
        response_json=result,
        structure=result.get("structure") if result.get("valid") else None,
        is_new=result.get("is_new", False),
    )

    result["history"] = database.get_history(req.session_id)
    return result


@app.get("/")
def index():
    html = FRONTEND_DIR / "index.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(html)


# ===== 管理者 =====

@app.get("/admin")
def admin_page():
    html = FRONTEND_DIR / "admin.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="admin.html not found")
    return FileResponse(html)


@app.get("/admin/api/students")
def admin_students():
    return database.admin_get_all_students()


@app.get("/admin/api/students/{user_id}")
def admin_student_detail(user_id: str):
    return {
        "user_id": user_id,
        "sessions": database.admin_get_student_sessions(user_id),
    }


@app.get("/admin/api/sessions/{session_id}")
def admin_session_logs(session_id: int):
    return database.admin_get_session_logs(session_id)


@app.delete("/admin/api/sessions/{session_id}")
def admin_delete_session(session_id: int):
    database.admin_delete_session(session_id)
    return {"ok": True}


@app.delete("/admin/api/logs/{log_id}")
def admin_delete_log(log_id: int):
    database.admin_delete_log(log_id)
    return {"ok": True}


@app.get("/admin/api/export/csv")
def admin_export_csv():
    rows = database.admin_get_all_logs_csv()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "user_id", "session_id", "session_start", "created_at",
        "message", "ai_message", "structure", "is_new", "session_new_count",
    ])
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sakumon_export.csv"},
    )
