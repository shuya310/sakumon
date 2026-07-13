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

前回ゼミで指摘された4項目について、原因・対応・確認結果を記録する。

## 2. 学習者ログの保存場所の可視化

### 状況
- 「ログがどこにどう保存されているか」がコードから読み取れる形になっていなかった

### 調査結果
- テーブルは2つ（`backend/database.py`）
  - `sessions`：1回のログイン〜作問セッション（`session_id`, `user_id`, `expression`, `created_at`）
  - `chat_logs`：メッセージ単位のログ（`session_id`, `user_id`, `message`, `response_json`, `structure`, `is_new`, `created_at`）
- 保存経路：`ブラウザ（index.js）→ FastAPI（main.py の /api/judge）→ database.py → data/sakumon.db（Render永続ディスク上）`
- 管理画面（`/admin`）はこの2テーブルを集計・閲覧するのみで、別の保存先は持たない
- CSVエクスポート（`/admin/api/export/csv`）で全ログを一括取得可能

### 対応
- コード変更なし。構成図とテーブル定義を整理し、ゼミ発表用の説明材料として提供

## 3. IME確定のEnterキーで誤送信される

### 原因
- `frontend/index.js` の送信処理は `isComposing` フラグのみで日本語入力中のEnterを判定していた
- Mac Safariでは、IME変換確定時に **`compositionend` が `keydown` より先に発火する**（Chromeとは順序が逆）。そのため確定Enterのkeydownが処理される時点で既に`isComposing`が`false`に戻っており、変換確定のつもりのEnterでメッセージが送信されてしまっていた

### 対応（[frontend/index.js:302-315](frontend/index.js:302)）
- `compositionend`発生直後（次のイベントループまで）のEnterを無視する`compositionJustEnded`フラグを追加
- 保険として旧ブラウザ向けの`e.keyCode === 229`判定も追加

### 動作確認
- ブラウザ上でSafari特有のイベント順序（`compositionstart → input → compositionend → keydown(Enter)`）を再現し、誤送信されないことを確認
- 通常のEnterキーによる送信（IME非経由）が引き続き正常に動作することも確認

## 4. ログアウトすると前回の記録が残らないことがある

### 調査結果
- ログ書き込み（`save_log`）は`/api/judge`のレスポンスを返す前に同期的に実行されており、ログイン状態やログアウトとは無関係にメッセージ単位で確実に保存される設計だった
- ログアウトボタンの処理はブラウザの`sessionStorage`をクリアするのみで、DBへの削除処理は一切呼ばれていない（削除は管理画面からの明示操作のみ）
- 実際の原因は「データ消失」ではなく、**小学生がログアウトボタンを押さずにタブを閉じてしまうケース**で、`sessionStorage`がブラウザの実装によりタブ再訪問時に残ってしまう／残らないことがあり、続き再開の挙動が不安定になっていたこと

### 対応（[frontend/index.js:373-383](frontend/index.js:373)）
- Navigation Timing APIで「F5リロード」か「新規に開いた（タブを閉じて再度開いた場合を含む）」かを判定
- リロード時のみ`sessionStorage`から前回の続きを復元し、それ以外（タブを閉じて開き直した場合含む）はセッション情報をクリアしてログイン画面から開始する（＝明示的なログアウトと同じ挙動に統一）

### 動作確認
- ブラウザで実際にF5リロード（`navigation.type === "reload"`）→続きから復元されることを確認
- 新規ナビゲーション（`navigation.type === "navigate"`、タブを閉じて再度開いた場合に相当）→ログイン画面に戻ることを確認
- いずれのケースでもDB上のログ自体は保持されており、データ消失は発生しない
