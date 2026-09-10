# 作問支援システム

小学4年生が同じ式（例: `24 ÷ 4`）から、意味構造の異なる3種類の文章題（等分除・包含除・倍）を作る活動を支援するWebアプリ。
生成AI（Claude API）が児童の作問を「成立性／構造／求める量」の3層で判定し、
その結果をサーバが決定論的に「支援水準」に変換して児童へ返す。

9/18 授業実践版では、**同じ判定機構を表示のオン／オフだけで測定（フェーズ1・3）と支援（フェーズ2）の両方に使う。**

---

## フェーズ

| フェーズ | 内容 | 式 | 児童に見えるもの |
|---|---|---|---|
| 1 | 事前測定：自由に作問 | 式A | 入力欄・送信ボタン・自分の吹き出し・「おくったよ」 |
| 2 | 支援：フィードバックを受けながら作問（新セッション。フェーズ1は引き継がない） | 式A | ＋ AIの声かけ、「作ったお話」（常設）、水準2以降 信号機（ラベルなしの3灯） |
| 3 | 事後測定：別の式で自由に作問（新セッション） | 式B | フェーズ1と同じ |

フェーズ・式は `app_config` テーブルにあり、管理画面（`/admin`）から切り替える。
児童の画面は5秒ごとに `/api/config` を確認し、変わったら自動で切り替わる。

## 支援水準（フェーズ2）

| 水準 | support_level | 内容 |
|---|---|---|
| 0 | `form` | 不成立作問への成立性フィードバック |
| 1 | `level1` | 右パネルの産出一覧を見せて「聞いていることは同じ？ちがう？」＋2択ボタン |
| 2 | `level2` | まだ聞いていない「求める量」を**1つだけ**名指す ＋ 信号機（ラベルなし3灯） |
| 3 | `level3` | 最終段。産出があれば「◯ばんと同じものを使って、こんどは『◯◯』を聞くお話に」＝**自分の産出を作り直させる**（`state=level3_rewrite`）／産出がなければ素材を想起させる（`state=level3_scene`） |
| ─ | `discover` | 新構造（称賛のみ） |
| ─ | `goal` | 3構造そろった |
| ─ | `talk` | 作問以外の入力 |
| ─ | `none` | フェーズ1・3（表示なし） |

- 遷移の単位は作問の提出と、対話での困り表明。既出構造の反復で1段階ずつ上がり（上限3）、新構造で0にリセット。
- 水準1で「ちがう」と答えた場合だけ、提出を待たず水準2へ。
- 水準1・2・goal・水準3の作り直し課題は固定文。`form` / `discover` / `level3`の場面想起 / `talk` は LLM 生成＋コード側ガード（構造名・「ずつ」・被除数と除数の同時出現などを含むと定型文へ差し替え）。
- 構造名（等分除・包含除・倍）は児童に一切見せない。図（テープ図）は `config.ENABLE_FIGURES=False` で無効。

## 処理の流れ

```
POST /api/judge {session_id, user_id, message, button_pressed}
  ├ 所有権チェック（user_id と sessions.user_id が一致）
  ├ ai_classify：作問 / 対話
  ├ 作問 → ai_judge：{valid, structure, unknown, issue}
  │        → main.py：学習者状態（S0〜S3）・支援水準を決定論的に算出
  │        → ai_dialogue：声かけ（フェーズ1・3は「おくったよ」のみ）
  └ 対話 → talk（水準2の問いへの「同じ／ちがう」だけ特別扱い）
  → chat_logs に全ターン記録（phase, support_level, learner_state, unknown, issue, button_pressed, stall_count, target_structure, expression）
```

### ai_judge の出力

```
valid:     true / false
structure: tobun / hougan / bai / invalid
unknown:   one_unit（1つ分）/ num_units（いくつ分）/ ratio（倍率）/ base（基準量）/ rate（割合）/ null
issue:     null / scene_contradiction / wrong_number / incomplete_text / wrong_operation / no_question / not_problem
```

倍は「乗法的比較の場面」全体（倍率・基準量・割合）。基準量を問う形（□×4=24）も成立・倍。逆立式は `wrong_number`。

---

## ファイル構成

```
backend/
  main.py          FastAPI・フェーズ管理・学習者状態・支援水準・管理者認証
  config.py        環境変数・式のパース・ENABLE_FIGURES
  database.py      SQLite（sessions / chat_logs / app_config / phase_changes）・マイグレーション・CSV
  ai_judge.py      3層判定（成立性→構造→求める量）
  ai_dialogue.py   水準ごとの声かけ（定型＋LLM＋ガード）
  ai_classify.py   作問／対話の分類
  kanji_rule.py    文字づかいルール
  tests/test_flow.py     遷移・認証・ログ・CSV の決定論テスト（LLMモック）
  tests/judge_cases.py   判定精度テスト（実API・35件）
frontend/
  index.html / index.js / index.css   児童画面
  admin.html / admin.js / admin.css   管理画面（フェーズ管理・児童の状態・ログ・CSV）
data/sakumon.db
```

## 起動

```
cd backend && uvicorn main:app --reload --port 8000
```

`.env` に `ANTHROPIC_API_KEY` と `ADMIN_PASSWORD` が必要（`.env.example` 参照）。
`ADMIN_PASSWORD` が無いと起動しない。Render では Environment に設定する。

## 管理画面 `/admin`

HTTP Basic 認証（ユーザー名は任意、パスワード＝`ADMIN_PASSWORD`）。

- フェーズ管理：現在のフェーズ、フェーズ1／2／3の切替、式A・式Bの編集、「新しい回を始める」（run_id を進める。リハーサルのデータを本番で拾わない）
- 児童の状態（5秒更新）：接続・提出数・成立数・到達3灯・支援水準・学習者状態。提出0問の児童を赤で強調
- 児童一覧・ログ・CSV

運用手順は `授業当日の運用手順_0918.md` を参照。

## アクセス

| URL | 説明 |
|-----|------|
| https://sakumon.onrender.com/ | 児童向け作問画面 |
| https://sakumon.onrender.com/admin | 管理者画面（要パスワード） |
