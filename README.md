# 作問支援システム

小学4年生が式（例: `12÷3`）から3種類の数量関係（等分除・包含除・倍）の文章題を作る練習を支援するWebアプリ。
生成AI（Claude API）が児童の解答を判定し、同じパターンに偏らないよう段階的ヒントで別の構造へ誘導する。
3つの構造すべての文章題を作成できたらクリア。

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

http://localhost:8000
