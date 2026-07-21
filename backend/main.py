import csv
import io
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database
import ai_judge
import ai_classify
import ai_dialogue

EXPRESSION = "18 ÷ 3"
STRUCTURES = {"tobun", "hougan", "bai"}
USER_ID_PATTERN = re.compile(r"^[0-9a-z]{2}$")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


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

    # 到達済み構造（信号機）と直近のやりとりを取得
    history = database.get_history(req.session_id)
    recent = database.get_recent_turns(req.session_id)

    # judge を呼ぶ前に「作問か対話か」を1回だけ分類（対話文が judge に流れ込むのを防ぐ）
    kind = ai_classify.classify(req.message, recent)

    if kind == "sakumon":
        result = _handle_sakumon(req, user_id, history, recent)
    else:
        result = _handle_taiwa(req, user_id, history, recent)

    result["history"] = database.get_history(req.session_id)
    return result


def _handle_taiwa(req: JudgeRequest, user_id: str, history: list[str], recent: list[dict]) -> dict:
    """対話経路: judge は通さず ai_dialogue のみ。信号機は変化しない。"""
    dlg = ai_dialogue.dialogue(req.message, "taiwa", None, history, recent)
    result = {
        "valid": False,
        "structure": None,
        "is_new": False,
        "display_type": "normal",
        "message": dlg["message"],
        "figure": dlg.get("figure"),
        "target_structure": dlg.get("target_structure"),
        "state": dlg.get("state"),
        "input_type": "taiwa",
    }
    database.save_log(
        session_id=req.session_id, user_id=user_id, message=req.message,
        response_json=result, structure=None, is_new=False, input_type="taiwa",
    )
    return result


def _handle_sakumon(req: JudgeRequest, user_id: str, history: list[str], recent: list[dict]) -> dict:
    """作問経路: ai_judge で構造同定 → サーバで信号機/表示種別を決定 → ai_dialogue で声かけ。"""
    jr = ai_judge.judge(req.message, EXPRESSION)

    # 構造同定が技術的に失敗（リトライ後もパース不可）→ 対話に回さず児童向けフォールバック
    if jr.get("issue") == "error":
        result = {
            "valid": False, "structure": None, "is_new": False,
            "display_type": "normal", "message": ai_dialogue.FALLBACK_MESSAGE,
            "figure": None, "target_structure": None, "state": "judge_error",
            "input_type": "sakumon",
        }
        database.save_log(
            session_id=req.session_id, user_id=user_id, message=req.message,
            response_json=result, structure=None, is_new=False, input_type="sakumon",
        )
        return result

    # 信号機の状態・表示種別はサーバが決定論的に計算する（AIに任せない）
    valid = jr.get("valid", False)
    structure = jr.get("structure", "invalid")
    is_new = valid and structure in STRUCTURES and structure not in history
    completes_all = is_new and (set(history) | {structure}) >= STRUCTURES

    if not valid:
        display_type = "normal"
    elif completes_all:
        display_type = "clear"
    elif is_new:
        display_type = "new_structure"
    else:
        display_type = "hint1"  # 既出構造のくり返し（停滞）。フロントの hint スタイルに対応

    # 児童向けの声かけ・図・次の目標は ai_dialogue が生成
    jr_for_dialogue = {**jr, "is_new": is_new, "completes_all": completes_all}
    dlg = ai_dialogue.dialogue(req.message, "sakumon", jr_for_dialogue, history, recent)

    result = {
        "valid": valid,
        "structure": structure if valid else None,
        "is_new": is_new,
        "display_type": display_type,
        "message": dlg["message"],
        "figure": dlg.get("figure"),
        "target_structure": dlg.get("target_structure"),
        "state": dlg.get("state"),
        "input_type": "sakumon",
    }
    database.save_log(
        session_id=req.session_id, user_id=user_id, message=req.message,
        response_json=result, structure=structure if valid else None,
        is_new=is_new, input_type="sakumon",
    )
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
        "message", "ai_message", "structure", "is_new", "input_type", "session_new_count",
    ])
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sakumon_export.csv"},
    )
