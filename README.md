# 作問支援システム

小学4年生が式（例: `18 ÷ 3`）をもとに、3種類の数量関係（等分除・包含除・倍）の文章題を作る練習を支援するWebアプリ。
生成AI（Claude API）が児童の解答を自動判定し、同じ構造に偏らないよう段階的ヒントで別の構造へ誘導する。
3つの構造すべての文章題を作成できたらクリア。

---

## システム概要

```
児童が文章題を入力
        ↓
バックエンド（FastAPI）が受け取る
        ↓
Claude API（claude-sonnet-4-6）が判定
        ↓
valid / structure / display_type を返す
        ↓
フロントエンドがチャット形式でフィードバック表示
```

---

## 3つの構造の定義

例：
　　　２(ひとつの量)×４（いくつ）=８（全体）
　　　「ひとつの量」を求めるのが等分除
　　　「いくつ」を求めるのが包含除
<img width="442" height="86" alt="image" src="https://github.com/user-attachments/assets/18a2d545-619c-4ad6-9119-ad89a0a2d426" />

| 構造 | 読み | 意味 | 例（18 ÷ 3）|
|------|------|------|-------------|
| tobun | 等分除 | 全体を等しく分けたとき「1人あたりいくつ」を求める | 18このあめを3人でわけると1人何個？ |
| hougan | 包含除 | 全体から一定数ずつ分けると「何人分」になるかを求める | 18このあめを3こずつくばると何人にくばれる？ |
| bai | 倍 | ある数が別の数の何倍かを求める | 18mは3mのなんばい？ |

---

## AI判定ロジック

### 判定フロー

```
入力文章題
    │
    ├─ 文章として成立していない → valid=false（normal）
    ├─ 式（18÷3）が成立しない → valid=false（normal）
    │
    └─ valid=true
            │
            ├─ 新しい構造（is_new=true）
            │       ├─ 3つ揃った → display_type="clear" 🎉
            │       └─ まだ揃っていない → display_type="new_structure"
            │
            └─ 既出の構造（is_new=false）→ ヒント段階を1つ進める
                    ├─ stage 1 → hint1（気づかせる）
                    ├─ stage 2 → hint2（観点を与える）
                    └─ stage 3 → hint3（穴埋め文型を提示）
```

### ヒント設計（段階的誘導）

| stage | display_type | 内容 |
|-------|-------------|------|
| 1 | hint1 | 「おなじおはなしがつづいているね」と気づかせる |
| 2 | hint2 | わけかたを変える観点を与える（数字は出さない）|
| 3 | hint3 | □を使った穴埋め文型のみ提示（数値禁止）|

### ハルシネーション対策

`is_new` および `stage` の確定はAIに任せず、**サーバー側（`ai_judge.py`）で上書き**している。
AIはメッセージ文のみ生成し、判定ロジックはPythonコードで制御する。

---

## ファイル構成

```
sakumon-system/
├── backend/
│   ├── main.py          # FastAPI エントリポイント・APIルーティング
│   ├── ai_judge.py      # Claude API 呼び出し・判定ロジック
│   ├── database.py      # SQLite 操作（セッション・ログ管理）
│   └── requirements.txt
├── frontend/
│   ├── index.html       # 児童向けUI（CSS・JS込み1ファイル）
│   └── admin.html       # 管理者画面
├── data/
│   └── sakumon.db       # SQLite データベース（自動生成）
├── .env                 # APIキー（Git管理外）
├── .env.example
└── README.md
```

---

## 技術スタック

| 役割 | 技術 |
|------|------|
| バックエンド | Python / FastAPI |
| AI判定 | Anthropic Claude API（claude-sonnet-4-6）|
| データベース | SQLite |
| フロントエンド | HTML / CSS / JavaScript（単一ファイル）|
| 仮想環境 | venv |

---

## 画面構成

| 画面 | 説明 |
|------|------|
| ログイン画面 | 学籍番号（半角英数2桁）でログイン |
| 選択画面 | 新規セッション or 続きから再開 |
| 作問画面 | チャット形式で文章題を入力・フィードバック表示 |
| クリア画面 | 3構造達成時に作った文章題を一覧表示 |
| 管理者画面 | 全ユーザーのログ閲覧・CSV出力・削除 |

---

## セットアップ

```bash
# 仮想環境の作成と有効化
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存パッケージのインストール
pip install -r backend/requirements.txt

# 環境変数の設定
cp .env.example .env
# .env を編集して ANTHROPIC_API_KEY を設定する
```

## 起動

```bash
cd backend && uvicorn main:app --reload --port 8000
```

## アクセス

| URL | 説明 |
|-----|------|
| https://sakumon.onrender.com/ | 児童向け作問画面 |
| https://sakumon.onrender.com/admin | 管理者画面 |
