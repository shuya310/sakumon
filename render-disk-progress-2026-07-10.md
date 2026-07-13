# 本日の進捗（2026-07-10）：Renderへの永続ディスク導入

## 背景・目的
- 作問支援システムはRender（Starterプラン）上でFastAPI + SQLiteで稼働
- これまでDBファイルはコンテナ内の一時領域に置かれており、**再デプロイのたびにデータが消える**状態だった
- 恒久的にデータを残すため、Renderの永続ディスク機能を導入

## 実施内容

### 1. 事前調査
- コード（`backend/database.py`）を確認し、DBパスは環境変数 `DATABASE_PATH` で上書き可能な作りになっていることを確認
- 未設定時のデフォルトは `data/sakumon.db`（コンテナ内一時領域）
- WALモード・busy_timeout設定は既に導入済みで、単一インスタンス構成との相性にも問題なし

### 2. Render側の設定
- Mount path `/data`、サイズ1GBの永続ディスクを作成
- 環境変数 `DATABASE_PATH = /data/sakumon.db` を追加

### 3. トラブルシューティング
デプロイ後、以下のエラーで起動失敗が発生。

```
PermissionError: [Errno 13] Permission denied: '/data'
```

原因の切り分け：
1. コードが `DB_PATH.parent.mkdir(...)` を無条件に実行しており、ディスクのマウントルート自体に `mkdir` を試みていたことが一因と判明 → 存在チェックを追加する修正を実施（[e715d8f](https://github.com/shuya310/sakumon/commit/e715d8f)）
2. 修正後も同様のエラーが継続 → ログを精査した結果、`/data` が `exists()` でも「存在しない」と判定されており、**ディスクのマウント自体が正しく成立していない**状態と判明
3. ビルドキャッシュを消した完全な再デプロイでも再現 → コード側ではなく、ディスクのプロビジョニング（インフラ側）の不整合と判断
4. **ディスクを一度削除し、再作成**することで解消

### 4. 動作確認
- 再デプロイ後、アプリでセッションを作成し正常動作を確認
- Renderの Shell から `ls -la /data/` でDBファイル（`sakumon.db` 他）の存在を確認
- Shell上で直接SQLクエリを実行し、データが書き込まれていることを確認
- 再デプロイ後もデータが消えず、永続化されていることを確認

## 結論
- Render上の永続ディスク（`/data`、1GB）とアプリを正しく接続し、DBの永続化を実現
- 副次的に、`database.py` のディレクトリ作成処理をより安全な実装に修正
- 今回の障害の主因はディスクのプロビジョニング不整合であり、再作成により解消

## 今回得られた知見
- Renderの永続ディスクは「設定画面上の表示」と「実際のマウント状態」がズレることがある
- `Permission denied` や `exists() == False` のような一見矛盾する挙動が出た場合、コードよりもインフラ側（ディスクの再作成）を先に疑うと早く解決できる

---

# ゼミ指摘事項への対応まとめ

| # | 指摘内容 | 原因 | 対応 | その後の状態 |
|---|---|---|---|---|
| 1 | Render再デプロイでデータが消えるかも | 無料プランのファイルシステムはエフェメラルで、DBファイルがコンテナ内一時領域にしかなかった | Render永続ディスク（`/data`、1GB）を追加し、`DATABASE_PATH`環境変数でDB保存先をディスク側に変更 | 再デプロイ後もデータが残ることをRender Shellから確認済み。**解決** |
| 2 | ログはどこに保存？構成が伝わらない | コードからしか読み取れず、説明資料がなかった | `sessions`/`chat_logs`テーブルの構成と、ブラウザ→FastAPI→SQLite(永続ディスク)の保存経路を整理（下記詳細参照） | コード変更なし。発表用資料として整理済み |
| 3 | IME確定のEnterで誤送信される | Mac SafariはIME確定時に`compositionend`が`keydown`より先に発火するため、`isComposing`だけの判定では確定Enterが送信として処理されていた | `compositionend`直後のEnterを無視するフラグを追加（[frontend/index.js:302](frontend/index.js:302)） | Safari特有のイベント順序を再現し、誤送信されないこと・通常送信は正常動作することを確認済み。**解決** |
| 4 | ログアウトせず前回の記録が残らないことがある | 実際はDB側にログ欠落はなく、ログアウトボタンを押さずタブを閉じた際に`sessionStorage`の残り方がブラウザ依存で不安定だったことが原因 | リロードと新規アクセスをNavigation Timing APIで判別し、リロード以外（タブを閉じて開き直した場合含む）はログアウトと同じ扱いに統一（[frontend/index.js:373](frontend/index.js:373)） | リロード→復元／新規アクセス→ログイン画面、を実機で確認済み。**解決** |

## 2. ログ保存構造（詳細）

- テーブルは2つ（`backend/database.py`）
  - `sessions`：1回のログイン〜作問セッション（`session_id`, `user_id`, `expression`, `created_at`）
  - `chat_logs`：メッセージ単位のログ（`session_id`, `user_id`, `message`, `response_json`, `structure`, `is_new`, `created_at`）
- 保存経路：`ブラウザ（index.js）→ FastAPI（main.py の /api/judge）→ database.py → data/sakumon.db（Render永続ディスク上）`
- 管理画面（`/admin`）はこの2テーブルを集計・閲覧するのみで、別の保存先は持たない
- CSVエクスポート（`/admin/api/export/csv`）で全ログを一括取得可能
