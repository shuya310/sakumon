"""llm_call（リトライ・タイムアウト・同時実行制御・計測）の決定論的テスト。API は呼ばない。

実行: cd backend && ./venv/bin/python tests/test_llm_call.py
"""
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ["LLM_MAX_CONCURRENCY"] = "1"

import anthropic  # noqa: E402
import httpx  # noqa: E402

import llm_call  # noqa: E402
import ai_judge  # noqa: E402
import ai_dialogue  # noqa: E402

llm_call._sleep_before_retry = lambda i: None   # テストでは待たない

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(code: int):
    resp = httpx.Response(code, request=_REQ, json={"error": {"type": "x", "message": "boom"}})
    cls = {429: anthropic.RateLimitError, 400: anthropic.BadRequestError,
           500: anthropic.InternalServerError, 529: anthropic.InternalServerError}[code]
    return cls("boom", response=resp, body=None)


def text_response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)])


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)   # 例外 or 応答 を順に返す
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def install(script):
    fm = FakeMessages(script)
    llm_call._client = SimpleNamespace(messages=fm)
    return fm


def ident(r):
    return r.content[0].text


# ---- 成功・retried_ok ----
fm = install([text_response("ok")])
out, meta = llm_call.call("01", ident, model="m")
assert out == "ok" and meta["status"] == "ok" and meta["retry_count"] == 0 and fm.calls == 1
assert meta["latency_ms"] >= 0

fm = install([anthropic.APITimeoutError(_REQ), status_error(529), status_error(429), text_response("ok")])
out, meta = llm_call.call("01", ident, model="m")
assert out == "ok" and meta["status"] == "retried_ok" and meta["retry_count"] == 3 and fm.calls == 4
print("OK 成功: 0回で ok、タイムアウト/529/429 を3回リトライして retried_ok")

# ---- 4回すべて失敗 → LLMUnavailable（retry_count=3、5回目は呼ばない）----
fm = install([status_error(500)] * 5)
try:
    llm_call.call("01", ident, model="m")
    raise AssertionError("should raise")
except llm_call.LLMUnavailable as e:
    assert e.retry_count == 3 and fm.calls == 4
print("OK 上限: 初回＋3回で打ち切り（5回目は呼ばない）")

# ---- 接続エラーもリトライ対象 ----
fm = install([anthropic.APIConnectionError(request=_REQ), text_response("ok")])
out, meta = llm_call.call("01", ident, model="m")
assert out == "ok" and meta["retry_count"] == 1
print("OK 接続エラーはリトライ")

# ---- 400 は即時失敗（リトライしない）----
fm = install([status_error(400), text_response("never")])
try:
    llm_call.call("01", ident, model="m")
    raise AssertionError("should raise")
except llm_call.LLMUnavailable as e:
    assert e.retry_count == 0 and fm.calls == 1
print("OK 400 はリトライしない")

# ---- パース失敗（ValueError）はリトライ対象 ----
fm = install([text_response("cut", stop_reason="max_tokens"), text_response("ok")])

def strict(r):
    if r.stop_reason == "max_tokens":
        raise ValueError("truncated")
    return ident(r)

out, meta = llm_call.call("01", strict, model="m")
assert out == "ok" and meta["retry_count"] == 1
print("OK 応答のパース失敗はリトライ")

# ---- リトライしなくても待ち状態が終われば消える ----
assert llm_call.wait_state("01") is None

# ---- 同時実行制御：上限1のとき、2人目は waiting になり、順番に処理される ----
release = threading.Event()
started = threading.Event()

class SlowMessages:
    def __init__(self):
        self.calls = 0
        self.max_inflight = 0
        self.inflight = 0
        self.lock = threading.Lock()

    def create(self, **kwargs):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.calls += 1
        started.set()
        release.wait(5)
        with self.lock:
            self.inflight -= 1
        return text_response("ok")

sm = SlowMessages()
llm_call._client = SimpleNamespace(messages=sm)
results = {}

def worker(uid):
    results[uid] = llm_call.call(uid, ident, model="m")

t1 = threading.Thread(target=worker, args=("11",))
t1.start()
assert started.wait(2)
assert llm_call.wait_state("11") == "running"
t2 = threading.Thread(target=worker, args=("12",))
t2.start()
for _ in range(50):
    if llm_call.wait_state("12") == "waiting":
        break
    time.sleep(0.02)
assert llm_call.wait_state("12") == "waiting", llm_call.wait_state("12")
release.set()
t1.join(5); t2.join(5)
assert results["11"][0] == "ok" and results["12"][0] == "ok"
assert sm.max_inflight == 1, sm.max_inflight
assert llm_call.wait_state("11") is None and llm_call.wait_state("12") is None
print("OK 同時実行制御: 上限1で2人目は waiting、同時に API へ出るのは1本、終われば状態が消える")

# ---- ai_judge: 全失敗 → issue=error + meta.status=failed（受理は main が行う）----
install([status_error(529)] * 4)
jr = ai_judge.judge("あめが24こ", "24 ÷ 4", user_id="01")
assert jr["issue"] == "error" and jr["meta"]["status"] == "failed" and jr["meta"]["retry_count"] == 3
assert jr["meta"]["latency_ms"] >= 0

# ai_judge: 成功時は normalize を通った形 + meta
install([text_response('{"reasoning":"r","valid":true,"structure":"tobun","unknown":"one_unit","issue":null}')])
jr = ai_judge.judge("あめが24こを4人で。1人分は？", "24 ÷ 4", user_id="01")
assert jr["valid"] is True and jr["structure"] == "tobun" and jr["meta"]["status"] == "ok"
print("OK ai_judge: 全失敗は issue=error/meta.failed、成功は従来の形＋meta")

# ---- ai_dialogue: API 全失敗 → 定型文（再依頼はしない）----
fm = install([status_error(500)] * 8)
out = ai_dialogue.dialogue("わからない", "taiwa", None, [], [], "talk", "24 ÷ 4", user_id="01")
assert out["message"] == ai_dialogue.TALK_FALLBACK.replace("{dividend}", "24") and out["state"] == "talk_fallback"
assert out["meta"]["status"] == "failed" and out["meta"]["retry_count"] == 3
assert fm.calls == 4, fm.calls   # API 不通なら2周目の再依頼はしない

# ai_dialogue: 境界違反 → 1回だけ再依頼（従来どおり）→ 2回目 OK
fm = install([text_response('{"check":"c","message":"これは等分除だね","state":"s"}'),
              text_response('{"check":"c","message":"いいね！","state":"s"}')])
out = ai_dialogue.dialogue("T", "taiwa", None, [], [], "talk", "24 ÷ 4", user_id="01")
assert out["message"] == "いいね！" and fm.calls == 2 and out["meta"]["status"] == "ok"
print("OK ai_dialogue: API不通は定型文（再依頼なし）、境界違反は従来どおり1回だけ再依頼")

print("\nALL PASSED")
