# 作問支援システム

小学4年生が式（例: `18 ÷ 3`）をもとに、3種類の数量関係（等分除・包含除・倍）の文章題を作る練習を支援するWebアプリ。
生成AI（Claude API）が児童の解答を自動判定し、同じ構造に偏らないよう段階的ヒントで別の構造へ誘導する。
3つの構造すべての文章題を作成できたらクリア。

---

## システム概要

```
児童が入力
        ↓
バックエンド（FastAPI /api/judge）が受け取る
        ↓
① ai_classify が「作問」か「対話」かを分類（judge を呼ぶ前に1回）
        ↓
   ├─ 作問 → ai_judge が構造同定 → サーバが信号機/display_type を決定 → ai_dialogue が声かけ
   └─ 対話 → ai_dialogue のみ（judge は通さない）
        ↓
valid / structure / display_type / message / figure を返す
        ↓
フロントエンドがチャット形式でフィードバック表示
```

---

## 3つの構造の定義

例：
２(ひとつの量)×４（いくつ）=８（全体）
「ひとつの量」を求めるのが等分除
「いくつ」を求めるのが包含除

| 構造 | 読み | 意味 | 例（18 ÷ 3）|
|------|------|------|-------------|
| tobun | 等分除 | 全体を等しく分けたとき「1人あたりいくつ」を求める | 18このあめを3人でわけると1人何個？ |
| hougan | 包含除 | 全体から一定数ずつ分けると「何人分」になるかを求める | 18このあめを3こずつくばると何人にくばれる？ |
| bai | 倍 | ある数が別の数の何倍かを求める | 18mは3mの何倍？ |

---

## AI処理の構成（役割分担）

入力は judge を呼ぶ前に「作問／対話」に分類され、経路が分かれる。児童向けメッセージは
すべて `ai_dialogue.py` が生成し、`ai_judge.py` は構造同定に専念する。

- **ai_classify.py** … 入力が「作問（新しいお話）」か「対話（質問・つぶやき・こまった）」かを1コールで分類。対話文が構造判定に流れ込むのを防ぐ。分類失敗時は安全側の「対話」に倒す。
- **ai_judge.py** … 作問文の**構造同定のみ**（`valid` / `structure` / `issue`）。児童向けメッセージやヒントは生成しない。
- **ai_dialogue.py** … 児童向けの声かけ（称賛・気づかせ・成立不備の問いかけ・クリア）と `figure`・`target_structure` を生成。すべての児童向けメッセージを担当。
- **main.py（サーバ）** … 構造同定の結果から、信号機の状態・`is_new`・3つ達成・`display_type` を**決定論的に**計算（AIに任せない）。

### 判定フロー（作問経路）

```
作問文
    │
    ├─ 構造として成立しない（issue: not_problem / wrong_number / reversed）→ valid=false（normal）
    │
    └─ valid=true
            ├─ 新しい構造（is_new=true）
            │       ├─ 3つ揃った → display_type="clear" 🎉
            │       └─ まだ → display_type="new_structure"
            └─ 既出の構造（is_new=false・停滞）→ display_type="hint1"（別の構造へ見方を向ける）
```

### 図（figure）について — ⚠️ 未確定事項

`ai_dialogue.py` は `figure`（描くテープ図の構造名: `tobun`/`hougan`/`bai`/`all`/`null`）を返すが、
**テープ図の描画処理は未実装**（現状この値は画面に影響しない。フロントが決定論的に描く想定で別途実装予定）。

なお「新しい構造ができたとき」の figure について、仕様上は「**できたばかりの構造**」を出す想定だが、
実測ではAIが「**次に作らせたい構造**」を返す傾向がある（例: 等分除ができたとき `figure:"hougan"`）。
バグではなくプロンプト解釈の差。**テープ図描画を実装する際に、どちらの図を出すか改めて確定すること。**

### ハルシネーション対策

`is_new` / 3つ達成 / `display_type` の確定はAIに任せず、**サーバー側（`main.py`）で計算・上書き**する。
AI（judge）は構造の同定のみ、AI（dialogue）はメッセージ文のみを担当し、状態管理はPythonコードで制御する。

---

## ファイル構成

```
sakumon-system/
├── backend/
│   ├── main.py          # FastAPI エントリポイント・APIルーティング・作問/対話の分岐
│   ├── ai_classify.py   # 入力を「作問」か「対話」かに分類
│   ├── ai_judge.py      # 作問文の構造同定（valid / structure / issue）
│   ├── ai_dialogue.py   # 児童向けの声かけ・figure・target_structure を生成
│   ├── kanji_rule.py    # 文字づかい共通ルール（KANJI_RULE）
│   ├── database.py      # SQLite 操作（セッション・ログ管理）
│   └── requirements.txt
├── frontend/
│   ├── index.html       # 児童向けUI
│   ├── index.css
│   ├── index.js
│   ├── admin.html       # 管理者画面
│   ├── admin.css
│   └── admin.js
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
| AI判定 | Anthropic Claude API（claude-sonnet-5）|
| データベース | SQLite |
| フロントエンド | HTML / CSS / JavaScript（画面ごとにファイル分離、FastAPIの`/static`で配信）|
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
