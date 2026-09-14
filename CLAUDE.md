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
- DBは data/sakumon.db。スキーマは sakumon_cleanup_spec.md 3章（database.SCHEMA）。時刻は JST
  - sessions は UNIQUE(user_id, phase)。1児童1フェーズ1セッション。「新しい回（run）」は廃止（リハーサル分は管理画面で削除）
  - 旧スキーマが残っていれば起動時に *_legacy_日付 へ改名して退避（DROP しない）。列追加は SCHEMA 変更＋既存 DB へ ALTER TABLE
  - is_new はフェーズスコープ（user_id + phase で到達構造を引く）
- 支援は docs/sakumon_spec_v2.md の状態機械（2章）。状態は sessions に保持（declared / declared_by / stuck_count / miss_count）
  - stuck = 新構造に到達しなかった成立作問の**連続**回数、miss = 予告不一致の累積。不成立・taiwa・resend では動かさない。
    新構造到達で両方 0。強さは main.decide_strength（stuck 2→弱, 3→中, 4+→強／miss 1→中, 2+→強）
  - response_type: form / praise / prompt / talk / done / error。prompt の文言・中の2ステップ対話・式・フロントは v2 の3章以降（未実装）
  - 予告は POST /api/declare（ai_classify.classify_declaration。unknown なら立てない）
  - フェーズ1・3は response_type / ai_message を記録しない（判定だけ記録）
- 図（figure/tape_diagram）は config.ENABLE_FIGURES=False で無効化中
- API 呼び出しは backend/llm_call.py 経由（タイムアウト20s・最大3回リトライ・同時実行上限30・計測）。
  環境変数 LLM_TIMEOUT_SECONDS / LLM_MAX_RETRIES / LLM_MAX_CONCURRENCY で変更可
- モデルは judge/dialogue/classify すべて MODEL（sonnet）。classify だけ Haiku にする案は
  「倍」構造の誤分類12.5%が実害として出たため不採用。環境変数 ANTHROPIC_MODEL で全体を変更可
  （CLASSIFY_MODEL で classify だけ個別に上書きすることも可能。既定は MODEL と同じ）
- judge が全リトライ失敗 → 作問は issue='pending'・response_type='error' で受理（一覧に載る・構造は空）。同一本文の連続再送は input_type='resend'
- テスト：backend/tests/test_flow.py（LLMモック・決定論）、tests/test_llm_call.py（リトライ・セマフォ）、tests/judge_cases.py（実API・判定精度）
- 運用手順：授業当日の運用手順_0918.md
