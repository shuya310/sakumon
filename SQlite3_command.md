## 必須ドット命令集

```
-- 起動・終了
sqlite3 /data/sakumon.db   -- DBに接続して起動（シェルから）
.quit / .exit              -- SQLiteを終了
.help                      -- ドット命令の一覧を表示

-- 構造の確認
.tables                    -- テーブル名の一覧
.schema                    -- 全テーブルの定義（CREATE文）
.schema chat_logs          -- 特定テーブルの定義のみ
.fullschema                -- 定義＋統計情報
.databases                 -- 接続中のDBファイル一覧
.indexes                   -- インデックス一覧
```

`.table`（単数）でも動くが、正式名は `.tables` である。`.schemas`（複数）は存在しないので `.schema` と覚えておくとよい。

## 表示を見やすくするコマンド

```
.headers on                -- カラム名を表示（デフォルトoff）
.mode column               -- 列をそろえて表示
.mode box                  -- 罫線付きの見やすい表形式（おすすめ）
.mode table                -- box に似た表形式
.mode list                 -- 「|」区切り（デフォルト）
.width 5 20 10             -- 各列の幅を指定
.nullvalue NULL            -- NULLを「NULL」と表示（空欄だと見分けにくいため）
```

`select*from chat_logs` の結果が `1|1|01|…` と読みにくかったのは `.mode list`（初期設定）のためである。次のように設定すると劇的に見やすくなる。

```
.headers on
.mode box
SELECT session_id, user_id, expression FROM sessions;
```

## バックアップ・エクスポート

```
-- バックアップ（WALの内容も含めて安全にコピー）
.backup /data/backup.db

-- CSVで書き出し
.headers on
.mode csv
.output logs.csv           -- 出力先をファイルに切替
SELECT * FROM chat_logs;
.output stdout             -- 出力先を画面に戻す

-- SQLファイルとして丸ごと書き出し（定義＋データ）
.dump                      -- 画面に表示
.output backup.sql         -- ファイルに書き出す場合は先にoutput指定
.dump
.output stdout
```

## シェルコマンド・その他

```
.shell ls -la /data/       -- SQLite内からシェルコマンドを実行
.system ls -la /data/      -- 同上（.shellと同じ）
.read script.sql           -- 外部のSQLファイルを読み込んで実行
.once out.txt              -- 次の1回だけ結果をファイルに出力
.timer on                  -- クエリの実行時間を表示
.echo on                   -- 実行したコマンドを表示（ログ確認用）
```

## WALまわり（`sakumon-system` で役立つ）

```
PRAGMA journal_mode;               -- 現在のジャーナルモードを確認（walと出るはず）
PRAGMA wal_checkpoint(TRUNCATE);   -- WALの内容を本体に反映してWALを縮小
PRAGMA table_info(chat_logs);      -- テーブルの列情報を詳しく表示
PRAGMA foreign_key_list(chat_logs);-- 外部キーの確認
```

## よく使う流れ（テンプレート）

DBに入ったらまずこの3つを打つと、以降ずっと見やすくなる。

```
.headers on
.mode box
.nullvalue NULL
```

初学者のうちは、この設定を毎回打つのが面倒なら、ホームディレクトリに `.sqliterc` というファイルを作って上の3行を書いておくと、起動時に自動で適用される。開発中の確認作業が楽になるはずだ。