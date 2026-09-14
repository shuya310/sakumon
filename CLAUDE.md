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
- フェーズは app_config テーブル（管理画面 /admin で変更）。式は main.EXPRESSION_ASSIGNMENT（設定テーブル。児童ごとに sessions.expression に固定）
- DBは data/sakumon.db。スキーマは sakumon_cleanup_spec.md 3章（database.SCHEMA）。時刻は JST
  - sessions は UNIQUE(user_id, phase)。1児童1フェーズ1セッション。「新しい回（run）」は廃止（リハーサル分は管理画面で削除）
  - 旧スキーマが残っていれば起動時に *_legacy_日付 へ改名して退避（DROP しない）。列追加は SCHEMA 変更＋既存 DB へ ALTER TABLE
  - is_new はフェーズスコープ（user_id + phase で到達構造を引く）
- 支援は docs/sakumon_spec_v2.md（2〜6章 実装済み・2026-09-15）。状態は sessions に保持（declared / declared_by / stuck_count / miss_count）
  - stuck = 新構造に到達しなかった成立作問の**連続**回数、miss = 予告不一致の累積。不成立・taiwa・resend では動かさない。
    新構造到達で両方 0。強さは main.decide_strength（stuck 2→弱, 3→中, 4+→強／miss 1→中, 2+→強）
  - response_type: form / praise / prompt / talk / done / error。文言は ai_dialogue の表（仕様3章を一字一句。言い換えない）。
    LLM を呼ぶのは talk だけ。`**…**` は強調、フロントで太字にする
  - 予告は POST /api/declare（classify_declaration。unknown なら立てない）。中は POST /api/self_label（text→choice の2ステップ）。
    中・強の declared=system は prompt 発行と同時に立てる。自己ラベルが判定と違っても訂正しない（禁止）。
    児童がステップに答えず作問を送ったら対話は打ち切り、通常処理（予告は立ったまま）
  - 式は main.EXPRESSION_ASSIGNMENT（奇偶×フェーズ。24÷4 は使わない）。セッション開始時に sessions.expression に固定。
    管理画面からセッション単位で上書き可（/admin/api/sessions/{id}/expression）。app_config.expression_a/b は未使用
  - 語彙：児童向けに「種類」「たずねる」「聞いていること」「ちがうことを聞く」を出さない（LLM 出力もガード）
- judge が全リトライ失敗 → issue='error'・response_type='error'「もう一度 おくって みてね」（一覧に載せない。送り直しは判定し直す）。判定済み本文の連続再送だけ input_type='resend'（API を呼ばない・カウンタ不動）
- テスト：backend/tests/test_flow.py（LLMモック・決定論）、tests/test_llm_call.py（リトライ・セマフォ）、tests/judge_cases.py（実API・判定精度）
- 運用手順：授業当日の運用手順_0918.md
