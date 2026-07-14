# 作問支援システム

小学4年生向け算数文章題作問支援アプリ（式「18÷3」から3構造の文章題を作る）

## 技術スタック
- FastAPI + SQLite + Anthropic API（claude-sonnet-5）
- フロントエンド：frontend/（画面ごとにhtml/css/jsを分離、FastAPIの/staticで配信）

## 構造
- tobun（等分除）/ hougan（包含除）/ bai（倍）

## 起動
cd backend && uvicorn main:app --reload --port 8000

## 注意
- APIキーは .env から読む
- DBは data/sakumon.db
