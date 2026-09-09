# 作問支援システム

小学4年生向け算数文章題作問支援アプリ（同じ式から3構造の文章題を作る）。
9/18 授業実践版：フェーズ1（事前・支援なし）→ フェーズ2（支援あり）→ フェーズ3（別の式・支援なし）。

## 技術スタック
- FastAPI + SQLite + Anthropic API（claude-sonnet-5）
- フロントエンド：frontend/（画面ごとにhtml/css/jsを分離、FastAPIの/staticで配信）

## 構造
- tobun（等分除）/ hougan（包含除）/ bai（倍：倍率 ratio・基準量 base・割合 rate）
- 児童に構造名は見せない。見せるのは求める量の言葉（1つ分／いくつ分／何倍）だけ。

## 起動
cd backend && uvicorn main:app --reload --port 8000

## 注意
- APIキー・ADMIN_PASSWORD は .env から読む（ADMIN_PASSWORD 未設定だと起動しない）
- 式・フェーズは app_config テーブル（管理画面 /admin で変更）。コードにハードコードしない
- DBは data/sakumon.db（列追加は database._migrate に ALTER TABLE で追記。DROP しない）
- 図（figure/tape_diagram）は config.ENABLE_FIGURES=False で無効化中
- テスト：backend/tests/test_flow.py（LLMモック・決定論）、tests/judge_cases.py（実API・判定精度）
- 運用手順：授業当日の運用手順_0918.md
