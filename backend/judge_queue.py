"""フェーズ1・3の「応答後の判定」キュー。

フェーズ1・3では判定結果を児童に見せないのに、判定（ai_judge。約 3 秒）を待ってから「おくったよ」を返していた。
ここでは作問行を judge_status='pending' で保存した直後に log_id をキューに入れ、応答を返したあとに判定して
同じ行を UPDATE する（database.set_judge_result。保存形式は同期判定と同じ）。フェーズ2 は従来どおり同期。

- 同時実行は config.JUDGE_BG_WORKERS（既定 8）。API 全体の同時数は llm_call のセマフォがさらに抑える。
- 児童ごとに送信順で直列に処理する（user_id ごとの待ち行列）。is_new / produced_structures を
  「その行より前の判定済み行」から計算するので、判定の完了順に関係なく送信順で決まる。
- llm_call の内部リトライを使い切って失敗したら config.JUDGE_BG_RETRY_WAITS（既定 10 秒, 30 秒）だけ待って
  再試行し、それでも失敗なら judge_status='failed'・issue='error'。管理画面の「再判定」で再投入できる。
- サーバ再起動でキューは消える（pending のまま残る）。同じく管理画面から再投入する。
- 単一プロセス前提（uvicorn 1 ワーカー）。テストは wait_idle() で完了を待つ。
"""

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import config
import database
import ai_judge

STRUCTURES = {"tobun", "hougan", "bai"}

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_queues: dict[str, deque] = {}        # user_id → 未処理の log_id（送信順）
_active: set[str] = set()             # いま executor で処理中の user_id
_idle = threading.Condition(_lock)
_inflight = 0                         # キュー投入済みで未完了の件数


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=config.JUDGE_BG_WORKERS, thread_name_prefix="judge")
    return _executor


def enqueue(user_id: str, log_id: int):
    """作問行を判定待ちに入れる。同じ児童の行は送信順に1つずつ処理する。"""
    global _inflight
    with _lock:
        _queues.setdefault(user_id, deque()).append(log_id)
        _inflight += 1
        if user_id in _active:
            return
        _active.add(user_id)
    _get_executor().submit(_drain, user_id)


def _drain(user_id: str):
    """その児童の待ち行列を空になるまで順に処理する。"""
    global _inflight
    while True:
        with _lock:
            q = _queues.get(user_id)
            if not q:
                _active.discard(user_id)
                _idle.notify_all()
                return
            log_id = q.popleft()
        try:
            _judge_one(log_id)
        except Exception as e:  # noqa: BLE001 — 1件の失敗でその児童の残りを止めない
            print(f"[judge_queue] unexpected error on log {log_id}: {type(e).__name__}: {e}")
            try:
                database.set_judge_result(log_id, valid=None, structure=None, unknown=None, issue="error",
                                          is_new=False, item=None, unit=None, produced_structures=None,
                                          judge_status="failed")
            except Exception as e2:  # noqa: BLE001
                print(f"[judge_queue] could not mark failed: {e2}")
        finally:
            with _lock:
                _inflight -= 1
                _idle.notify_all()


def _judge_one(log_id: int):
    row = database.get_log_for_judge(log_id)
    if row is None or row["judge_status"] == "done":
        return
    # user_id は渡さない（llm_call の wait_state は児童が送信中に見る表示のためのもの。裏の判定で触らない）
    jr = ai_judge.judge(row["message"], row["expression"], user_id=None)
    for wait in config.JUDGE_BG_RETRY_WAITS:
        if jr.get("issue") != "error":
            break
        print(f"[judge_queue] log {log_id} failed ({jr.get('error')}); retry after {wait}s")
        time.sleep(wait)
        jr = ai_judge.judge(row["message"], row["expression"], user_id=None)
    if jr.get("issue") == "error":
        database.set_judge_result(log_id, valid=None, structure=None, unknown=None, issue="error",
                                  is_new=False, item=None, unit=None, produced_structures=None,
                                  judge_status="failed")
        return
    valid = bool(jr["valid"])
    structure = jr["structure"] if valid else None
    produced = database.get_produced_before(row["user_id"], row["phase"], log_id)
    is_new = bool(valid and structure in STRUCTURES and structure not in produced)
    produced_after = list(produced) + ([structure] if is_new else [])
    database.set_judge_result(log_id, valid=valid, structure=structure, unknown=jr["unknown"], issue=jr["issue"],
                              is_new=is_new, item=jr.get("item"), unit=jr.get("unit"),
                              produced_structures=produced_after, judge_status="done")


def requeue_unjudged() -> int:
    """pending / failed の作問行をすべてキューに入れ直す（管理画面の再判定）。戻り値は投入件数。
    すでにキューにある行は入れない。"""
    rows = database.get_unjudged_log_ids()
    with _lock:
        queued = {lid for q in _queues.values() for lid in q}
    n = 0
    for r in rows:
        if r["log_id"] in queued:
            continue
        enqueue(r["user_id"], r["log_id"])
        n += 1
    return n


def pending_in_queue() -> int:
    with _lock:
        return _inflight


def wait_idle(timeout: float = 30.0) -> bool:
    """キューが空になるまで待つ（テスト用）。"""
    deadline = time.monotonic() + timeout
    with _idle:
        while _inflight > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _idle.wait(remaining)
    return True
