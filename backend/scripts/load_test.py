"""負荷試験：N 人の仮想児童が同時にログインし、作問を 5 回ずつ送る。

本体コードには依存しない独立スクリプト（HTTP だけで動く）。実 API を叩くので費用がかかる。

実行例:
  cd backend
  ./venv/bin/python scripts/load_test.py --url http://localhost:8000 --fast
  ./venv/bin/python scripts/load_test.py --url https://sakumon.onrender.com --n-students 33 \\
      --admin-password '...' --cleanup

児童らしい送信間隔（20〜90秒）が既定。--fast で 1〜3 秒に短縮（同時到着の負荷は --fast の方が重い）。

出力する指標と、その取り方：
  - リクエスト総数・成功/失敗・HTTP ステータス内訳・体感待ち時間 … クライアント側の計測（常に出る）
  - latency_ms（classify＋judge＋声かけの合計）の中央値/90p/最大、judge フォールバック率（判定保留）…
      サーバが chat_logs に記録した値を管理 API（/admin/api/sessions/{id}）から取る。
      → --admin-password（または .env の ADMIN_PASSWORD）が必要
      （API リトライ回数は列に残らない。サーバログの [llm_call] attempt 行で確認する）
  - classify フォールバック率 … サーバは classify の失敗を列に残していないので、
      「作問サンプルを送ったのに input_type が taiwa になった件数」で推定する（誤分類も含む上限値）
  - 429 の発生回数 … Anthropic からの 429 はサーバ内のリトライで吸収され、クライアントには見えない。
      ローカル実行時は --server-log にサーバの標準出力を保存したファイルを渡すと
      「[llm_call] attempt N failed: RateLimitError」の行を数える。渡さなければ「計測不可」と出す。
      （アプリ自身が 429 を返した場合はステータス内訳に出る）
  - 待ち行列 … 送信中は /api/judge/status を 0.5 秒間隔で見て（フロントと同じ）、
      "waiting" だった時間と、同時に waiting だった人数の最大を集計する

user_id は 2 桁固定（本体の USER_ID_PATTERN）なので本番と完全に分離した番号帯は作れない。
そのため 99 から下向きに割り当てる（35 人なら 65〜99。実学級の 01〜35 とは重ならない）うえで、
末尾に使った user_id / session_id を出力し、--cleanup で管理 API からセッションを削除できる。
"""

import argparse
import json
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

# ---- 作問サンプル（{a} ÷ {b} を /api/config の式で埋める）----
# 成立 3 構造＋不成立 2 種。同じ本文を続けて送ると本体が「再送」とみなして API を呼ばないので、
# 連続で同じものを引かないようにする。
SAMPLES = [
    ("tobun", "おりがみが{a}まいあります。{b}人で同じ数ずつ分けると、1人分は何まいですか。"),
    ("tobun", "ジュースが{a}dLあります。{b}つのコップに同じ量ずつ入れると、1つのコップは何dLですか。"),
    ("hougan", "あめが{a}こあります。1人に{b}こずつ配ると、何人に配れますか。"),
    ("hougan", "クッキーが{a}まいあります。1ふくろに{b}まいずつ入れると、ふくろは何ふくろできますか。"),
    ("bai", "赤いリボンは{a}cm、青いリボンは{b}cmです。赤いリボンは青いリボンの何倍ですか。"),
    ("bai", "お兄さんは{a}さい、いもうとは{b}さいです。お兄さんの年はいもうとの年の何倍ですか。"),
    ("invalid", "あめが{a}こあります。{b}人に分けます。"),                       # 問いなし
    ("invalid", "りんごが{a}こあります。{b}こ食べると、のこりは何こですか。"),     # ひき算
]
STATUS_POLL_SECONDS = 0.5
RATE_LIMIT_LINE = re.compile(r"\[llm_call\] attempt \d+ failed: RateLimitError")


def load_env_admin_password() -> str | None:
    """ローカル向け：../.env の ADMIN_PASSWORD を読む（dotenv 無しの簡易版）。"""
    p = Path(__file__).resolve().parent.parent.parent / ".env"
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ADMIN_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []          # {"user_id","status","wall_s","ok","input_type","accepted","message_kind","waited_s"}
        self.errors = []
        self.waiting_now = set()    # いま "waiting" の user_id（同時待ち人数の最大を取る）
        self.max_waiting = 0
        self.max_waiting_at = None

    def add(self, rec: dict):
        with self.lock:
            self.requests.append(rec)

    def set_waiting(self, user_id: str, waiting: bool):
        with self.lock:
            if waiting:
                self.waiting_now.add(user_id)
                if len(self.waiting_now) > self.max_waiting:
                    self.max_waiting = len(self.waiting_now)
                    self.max_waiting_at = time.time()
            else:
                self.waiting_now.discard(user_id)


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[k]


def fmt_ms(v):
    return "—" if v is None else f"{v / 1000:.2f}s"


def run_student(args, base: str, user_id: str, samples: list[tuple[str, str]], metrics: Metrics,
                sessions: dict, start_gate: threading.Event):
    client = httpx.Client(base_url=base, timeout=httpx.Timeout(180.0, connect=15.0))
    start_gate.wait()
    try:
        t0 = time.perf_counter()
        r = client.post("/api/login", json={"user_id": user_id})
        metrics.add({"user_id": user_id, "kind": "login", "status": r.status_code,
                     "wall_s": time.perf_counter() - t0, "ok": r.is_success, "waited_s": 0.0})
        if not r.is_success:
            metrics.errors.append(f"{user_id} login {r.status_code}: {r.text[:120]}")
            return
        session_id = r.json()["session_id"]
        sessions[user_id] = session_id

        prev_text = None
        for i in range(args.n_messages):
            lo, hi = (1.0, 3.0) if args.fast else (20.0, 90.0)
            time.sleep(random.uniform(lo, hi))
            kind, text = random.choice([s for s in samples if s[1] != prev_text])
            prev_text = text

            # 送信と並行して /api/judge/status を見る（フロントと同じ 1 リクエスト中の待ち観測）
            waited = {"s": 0.0}
            done = threading.Event()

            def watch():
                was_waiting = False
                t_wait = None
                while not done.wait(STATUS_POLL_SECONDS):
                    try:
                        st = client.get("/api/judge/status", params={"user_id": user_id}, timeout=5.0)
                        waiting = st.is_success and st.json().get("state") == "waiting"
                    except httpx.HTTPError:
                        waiting = False
                    if waiting and not was_waiting:
                        t_wait = time.perf_counter()
                    if not waiting and was_waiting and t_wait is not None:
                        waited["s"] += time.perf_counter() - t_wait
                        t_wait = None
                    was_waiting = waiting
                    metrics.set_waiting(user_id, waiting)
                if was_waiting and t_wait is not None:
                    waited["s"] += time.perf_counter() - t_wait
                metrics.set_waiting(user_id, False)

            th = threading.Thread(target=watch, daemon=True)
            th.start()
            t0 = time.perf_counter()
            try:
                r = client.post("/api/judge", json={"session_id": session_id, "user_id": user_id,
                                                    "message": text})
                status, ok, body = r.status_code, r.is_success, (r.json() if r.is_success else {})
                if not ok:
                    metrics.errors.append(f"{user_id} judge {r.status_code}: {r.text[:120]}")
            except httpx.HTTPError as e:
                status, ok, body = 0, False, {}
                metrics.errors.append(f"{user_id} judge EXC {type(e).__name__}: {e}")
            wall = time.perf_counter() - t0
            done.set()
            th.join(2)
            metrics.add({"user_id": user_id, "kind": "judge", "status": status, "wall_s": wall, "ok": ok,
                         "sample_kind": kind, "text": text,
                         "input_type": body.get("input_type"), "state": body.get("state"),
                         "accepted": body.get("accepted"), "waited_s": waited["s"]})
    finally:
        client.close()


def fetch_server_logs(client: httpx.Client, sessions: dict) -> list[dict]:
    rows = []
    for uid, sid in sessions.items():
        r = client.get(f"/admin/api/sessions/{sid}")
        if r.is_success:
            for row in r.json():
                row["_user_id"] = uid
                rows.append(row)
        else:
            print(f"  ! /admin/api/sessions/{sid} → {r.status_code}")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000", help="対象（ローカル / Render 本番）")
    ap.add_argument("--n-students", type=int, default=35, help="同時人数（既定 35。欠席があれば減らす）")
    ap.add_argument("--n-messages", type=int, default=5, help="1人あたりの作問送信回数（既定 5）")
    ap.add_argument("--fast", action="store_true", help="送信間隔を 1〜3 秒に短縮（既定は 20〜90 秒）")
    ap.add_argument("--admin-password", default=None,
                    help="管理 API 用（サーバ側指標の取得と --cleanup に必要。省略時は ../.env の ADMIN_PASSWORD）")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--cleanup", action="store_true", help="終了後に試験セッションを管理 API で削除する")
    ap.add_argument("--cleanup-only", action="store_true",
                    help="試験は行わず、現在の run で試験用 user_id 帯（--n-students 分）が持つセッションを削除する")
    ap.add_argument("--server-log", default=None, help="ローカル実行時：サーバ標準出力を保存したファイル（429 集計用）")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if not 1 <= args.n_students <= 99:
        sys.exit("--n-students は 1〜99")
    if args.seed is not None:
        random.seed(args.seed)
    base = args.url.rstrip("/")
    admin_pw = args.admin_password or load_env_admin_password()

    # ---- 事前：式・フェーズ ----
    with httpx.Client(base_url=base, timeout=30.0) as c:
        cfg = c.get("/api/config").json()
    a, b = cfg["dividend"], cfg["divisor"]
    samples = [(k, t.format(a=a, b=b)) for k, t in SAMPLES]
    print(f"対象: {base}  式: {cfg['expression']}  フェーズ: {cfg['phase']}")
    if cfg["phase"] != 2:
        print("  注: フェーズ2以外では声かけ（ai_dialogue）が呼ばれないため、API 負荷はフェーズ2より軽い")
    user_ids = [f"{99 - i:02d}" for i in range(args.n_students)]   # 99, 98, …（実学級の 01〜 と重ねない）

    if args.cleanup_only:
        if not admin_pw:
            sys.exit("--cleanup-only には管理パスワードが必要")
        deleted = 0
        with httpx.Client(base_url=base, timeout=60.0, auth=(args.admin_user, admin_pw)) as ac:
            for uid in user_ids:
                r = ac.get(f"/admin/api/students/{uid}")
                if not r.is_success:
                    continue
                for sess in r.json().get("sessions", []):
                    if ac.delete(f"/admin/api/sessions/{sess['session_id']}").is_success:
                        deleted += 1
                        print(f"  削除: user {uid} session {sess['session_id']}")
        print(f"--cleanup-only: {deleted} セッションを削除した")
        return

    print(f"人数: {args.n_students}  送信/人: {args.n_messages}  間隔: {'1〜3秒 (--fast)' if args.fast else '20〜90秒'}")
    print(f"user_id: {user_ids[-1]}〜{user_ids[0]}\n")

    # ---- 実行 ----
    metrics = Metrics()
    sessions: dict[str, int] = {}
    gate = threading.Event()
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.n_students) as ex:
        futs = [ex.submit(run_student, args, base, uid, samples, metrics, sessions, gate) for uid in user_ids]
        gate.set()   # 全員同時にログイン
        for f in futs:
            f.result()
    t_end = time.time()
    print(f"完了: {t_end - t_start:.0f} 秒\n")

    # ---- クライアント側集計 ----
    reqs = metrics.requests
    judges = [r for r in reqs if r["kind"] == "judge"]
    print("== リクエスト（クライアント側） ==")
    print(f"総数 {len(reqs)}（login {len(reqs) - len(judges)} / judge {len(judges)}）  "
          f"成功 {sum(r['ok'] for r in reqs)}  失敗 {sum(not r['ok'] for r in reqs)}")
    print("HTTP ステータス内訳:", dict(sorted(Counter(r["status"] for r in reqs).items())))
    walls = [r["wall_s"] * 1000 for r in judges]
    print(f"/api/judge 体感待ち時間: 中央値 {fmt_ms(pct(walls, 50))}  90p {fmt_ms(pct(walls, 90))}  最大 {fmt_ms(pct(walls, 100))}")
    ok_judges = [r for r in judges if r["ok"]]
    pending = [r for r in ok_judges if r.get("state") == "judge_pending"]
    resend = [r for r in ok_judges if r.get("input_type") == "resend"]
    print(f"判定保留（judge_pending）で受理: {len(pending)}  再送扱い: {len(resend)}")

    print("\n== 待ち行列（LLM_MAX_CONCURRENCY） ==")
    waited = [r["waited_s"] for r in judges if r["waited_s"] > 0]
    print(f"待たされたリクエスト: {len(waited)} / {len(judges)}  最大待機 {max(waited) if waited else 0:.1f} 秒  "
          f"同時に待っていた最大人数: {metrics.max_waiting}"
          + (f"（開始 {metrics.max_waiting_at - t_start:.0f} 秒後）" if metrics.max_waiting_at else ""))
    print(f"  ※ {STATUS_POLL_SECONDS} 秒間隔のサンプリング値。短い待ちは取りこぼす")

    # ---- サーバ側集計（管理 API） ----
    print("\n== サーバ側の記録（chat_logs） ==")
    rows = []
    if admin_pw:
        with httpx.Client(base_url=base, timeout=60.0, auth=(args.admin_user, admin_pw)) as ac:
            rows = fetch_server_logs(ac, sessions)
    if not rows:
        print("  管理 API に入れないため取得できず（--admin-password を指定）" if not admin_pw else "  行が取れなかった")
    else:
        jrows = [r for r in rows if r.get("input_type") == "sakumon"]
        lat = [r["latency_ms"] for r in jrows if r.get("latency_ms") is not None]
        print(f"judge が動いた行: {len(jrows)}（記録行 {len(rows)}）")
        print(f"latency_ms（作問ターン）: 中央値 {fmt_ms(pct(lat, 50))}  90p {fmt_ms(pct(lat, 90))}  最大 {fmt_ms(pct(lat, 100))}")
        tlat = [r["latency_ms"] for r in rows if r.get("input_type") == "taiwa" and r.get("latency_ms") is not None]
        if tlat:
            print(f"latency_ms（対話ターン）: 中央値 {fmt_ms(pct(tlat, 50))}  90p {fmt_ms(pct(tlat, 90))}  最大 {fmt_ms(pct(tlat, 100))}")
        failed = [r for r in jrows if r.get("issue") == "pending"]
        n = max(1, len(jrows))
        print(f"judge フォールバック率（判定保留）: {len(failed)}/{len(jrows)} = {100 * len(failed) / n:.1f}%")
        # classify：作問サンプルなのに taiwa になった行（フォールバック＋誤分類の上限値）
        sakumon_rows = [r for r in rows if r.get("input_type") in ("sakumon", "taiwa")]
        misrouted = [r for r in sakumon_rows if r.get("input_type") == "taiwa"]
        m = max(1, len(sakumon_rows))
        print(f"classify フォールバック率（推定上限）: {len(misrouted)}/{len(sakumon_rows)} = {100 * len(misrouted) / m:.1f}%"
              f"  ※ 誤分類も含む。失敗そのものはサーバログの [ai_classify] classify failed 行で確認")

    print("\n== 429（レート制限） ==")
    app_429 = sum(1 for r in reqs if r["status"] == 429)
    print(f"アプリが 429 を返した回数: {app_429}")
    if args.server_log:
        try:
            text = Path(args.server_log).read_text(encoding="utf-8", errors="replace")
            print(f"Anthropic からの 429（サーバログ）: {len(RATE_LIMIT_LINE.findall(text))} 回  "
                  f"※ ファイル全体を数える。試験前の行が混ざるなら空にしてから起動すること")
        except OSError as e:
            print(f"  サーバログを読めない: {e}")
    else:
        print("Anthropic からの 429: 計測不可（サーバ内でリトライ吸収。ローカルなら --server-log でサーバ出力を渡す）")

    if metrics.errors:
        print("\n== エラー（先頭 20 件） ==")
        for e in metrics.errors[:20]:
            print("  " + e)

    # ---- 後片付け ----
    print("\n== 使用した user_id / session_id ==")
    print("user_id:", " ".join(user_ids))
    print("session_id:", " ".join(str(sessions[u]) for u in user_ids if u in sessions))
    if args.cleanup:
        if not admin_pw:
            print("--cleanup: 管理パスワードが無いため削除できない")
        else:
            with httpx.Client(base_url=base, timeout=60.0, auth=(args.admin_user, admin_pw)) as ac:
                deleted = 0
                for sid in sessions.values():
                    if ac.delete(f"/admin/api/sessions/{sid}").is_success:
                        deleted += 1
            print(f"--cleanup: {deleted}/{len(sessions)} セッションを削除した")
    else:
        print(f"削除するには: scripts/load_test.py --url {base} --n-students {args.n_students} --cleanup-only")

    # 生データ（あとで見返せるように）
    out = Path(__file__).resolve().parent / f"load_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"args": vars(args) | {"admin_password": "***" if admin_pw else None},
                               "config": cfg, "user_ids": user_ids, "sessions": sessions,
                               "requests": reqs, "server_rows": rows, "errors": metrics.errors},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n生データ: {out}")


if __name__ == "__main__":
    main()
