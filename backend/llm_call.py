"""Anthropic API 呼び出しの共通層：リトライ・タイムアウト・同時実行制御・計測。

9/10 の試用で、ai_judge の失敗が1人の児童に2分間集中し、その間「もう一度 おくってみてね」
しか返らず活動が止まった。判定ロジックには触れず、呼び出しの信頼性だけをここで底上げする。

  - 1回あたりのタイムアウトを明示（config.LLM_TIMEOUT_SECONDS）
  - 最大 config.LLM_MAX_RETRIES 回まで指数バックオフ＋ジッタで再試行
      対象：タイムアウト・接続エラー・429・5xx（529 overloaded 含む）・応答パース失敗
      対象外：429 以外の 4xx（プロンプトやスキーマの誤りなので繰り返しても直らない）
  - サーバ全体で API への同時リクエスト数を config.LLM_MAX_CONCURRENCY に制限（セマフォ）。
    待たされている児童には「じゅんばんに 見ているよ」を出せるよう、user_id ごとの待ち状態を持つ
  - 呼び出しごとに retry_count / latency_ms / status を返す（chat_logs に記録する）

SDK 自身の自動リトライ（max_retries）は 0 にして、回数・待ち時間・記録をこちらで一元管理する。
"""

import os
import random
import threading
import time

import anthropic

import config

_client = anthropic.Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    timeout=config.LLM_TIMEOUT_SECONDS,
    max_retries=0,
)

# 0.5秒 → 1.5秒 → 3秒（＋ジッタ）。4回目以降の再試行は行わない設定でも末尾値を使い回す。
_BACKOFF_SECONDS = (0.5, 1.5, 3.0)

_semaphore = threading.BoundedSemaphore(config.LLM_MAX_CONCURRENCY)

# user_id → "waiting"（セマフォ待ち）/ "running"（API 呼び出し中）。フロントが1秒間隔で見る。
_wait_state: dict[str, str] = {}
_wait_lock = threading.Lock()


class LLMUnavailable(Exception):
    """規定回数リトライしても応答が得られなかった。呼び出し側はフォールバックに進む。"""

    def __init__(self, last_error: BaseException, retry_count: int):
        super().__init__(f"{type(last_error).__name__}: {last_error}")
        self.last_error = last_error
        self.retry_count = retry_count


def _is_retryable(e: BaseException) -> bool:
    if isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    if isinstance(e, anthropic.APIStatusError):
        # 5xx（529 overloaded を含む）と 408/409 は再試行。それ以外の 4xx は再試行しない
        return e.status_code >= 500 or e.status_code in (408, 409)
    # 応答は返ったが JSON が取り出せない・途中で切れている等（ValueError / JSONDecodeError）
    return isinstance(e, ValueError)


def _sleep_before_retry(retry_index: int):
    base = _BACKOFF_SECONDS[min(retry_index, len(_BACKOFF_SECONDS) - 1)]
    time.sleep(base + random.uniform(0, base * 0.3))


def _set_wait_state(user_id: str | None, state: str | None):
    if not user_id:
        return
    with _wait_lock:
        if state is None:
            _wait_state.pop(user_id, None)
        else:
            _wait_state[user_id] = state


def wait_state(user_id: str) -> str | None:
    """その児童のいまの状態（"waiting" / "running" / None）。/api/judge/status が返す。"""
    with _wait_lock:
        return _wait_state.get(user_id)


def call(user_id: str | None, parse, **create_kwargs) -> tuple:
    """client.messages.create(**create_kwargs) を実行し、parse(response) の結果を返す。

    parse は応答を検査・変換する関数。ここで ValueError を投げれば再試行の対象になる
    （max_tokens 切れ・JSON 取り出し失敗など）。

    戻り値: (parsed, meta)
      meta = {"retry_count": int, "latency_ms": int, "status": "ok" | "retried_ok"}
    全試行が失敗したら LLMUnavailable（retry_count 付き）。
    再試行しない 4xx は即座に LLMUnavailable（retry_count は試行時点の値）。
    """
    started = time.perf_counter()
    retries = 0
    last_err: BaseException | None = None
    max_retries = config.LLM_MAX_RETRIES

    # セマフォ：空きがなければ「待ち」を記録してから並ぶ
    if not _semaphore.acquire(blocking=False):
        _set_wait_state(user_id, "waiting")
        _semaphore.acquire()
    _set_wait_state(user_id, "running")
    try:
        while True:
            try:
                response = _client.messages.create(**create_kwargs)
                parsed = parse(response)
                latency = int((time.perf_counter() - started) * 1000)
                return parsed, {"retry_count": retries, "latency_ms": latency,
                                "status": "retried_ok" if retries else "ok"}
            except Exception as e:  # noqa: BLE001 — 種別ごとの扱いは _is_retryable に集約
                last_err = e
                print(f"[llm_call] attempt {retries + 1} failed: {type(e).__name__}: {e}")
                if not _is_retryable(e) or retries >= max_retries:
                    raise LLMUnavailable(e, retries) from e
                _sleep_before_retry(retries)
                retries += 1
    finally:
        _semaphore.release()
        _set_wait_state(user_id, None)


def failed_meta(err: LLMUnavailable, started: float) -> dict:
    """LLMUnavailable から記録用 meta を作る。started は perf_counter の開始値。"""
    return {"retry_count": err.retry_count,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "status": "failed"}
