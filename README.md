# 作問支援システム

小学4年生が同じ式（例: `24 ÷ 4`）から、意味構造の異なる3種類の文章題（等分除・包含除・倍）を作る活動を支援するWebアプリ。
生成AI（Claude API）が児童の作問を「成立性／構造／求める量」の3層で判定し、
その結果をサーバが決定論的に「応答の種類（response_type）」に変換して児童へ返す。

9/18 授業実践版では、**同じ判定機構を表示のオン／オフだけで測定（フェーズ1・3）と支援（フェーズ2）の両方に使う。**

---

## フェーズ

| フェーズ | 内容 | 式 | 児童に見えるもの |
|---|---|---|---|
| 1 | 事前測定：自由に作問 | 式A | 入力欄・送信ボタン・自分の吹き出し・「おくったよ」 |
| 2 | 支援：フィードバックを受けながら作問（新セッション。フェーズ1は引き継がない） | 式A | ＋ AIの声かけ、「作ったお話」（常設）、信号機（ラベルなしの3灯。予告支援「中」以降／3つそろった後） |
| 3 | 事後測定：別の式で自由に作問（新セッション） | 式B | フェーズ1と同じ |

フェーズ・式は `app_config` テーブルにあり、管理画面（`/admin`）から切り替える。
児童の画面は5秒ごとに `/api/config` を確認し、変わったら自動で切り替わる。

## 応答の種類（フェーズ2）

| response_type | 内容 | 生成 |
|---|---|---|
| `form` | 不成立作問への成立性フィードバック | LLM＋ガード（失敗時は定型文） |
| `praise` | 成立作問への称賛。新しい聞き方なら LLM、既出のくり返しなら定型文 | LLM／定型 |
| `prompt` | 予告支援（`prompt_strength` 1=弱／2=中／3=強）。**予告支援仕様で実装**（未実装） | ─ |
| `talk` | 作問以外の入力。困り表明も「足場不足」と解釈し、手がかりを1つ出して作問にもどす | LLM＋ガード |
| `done` | 3構造そろった | 定型 |
| `error` | judge が API 不通（判定保留 `issue=pending`）。受理して一覧に載せる | 定型 |

- フェーズ1・3は表示しないので `response_type` / `ai_message` は記録しない（判定だけ記録）。
- 制御変数は `produced_structures`（到達構造の集合）・`stuck_count`（困り表明の回数）・`miss_count`（不成立の回数）。
  旧 S0〜S3・水準1〜4・「同じ／ちがう」ボタンは廃止（`sakumon_cleanup_spec.md`）。
- LLM 生成にはコード側ガード（構造名・「1つ分」等の求める量の語・被除数と除数の同時出現などを含むと定型文へ差し替え）。
- 構造名（等分除・包含除・倍）は児童に一切見せない。図（テープ図）は `config.ENABLE_FIGURES=False` で無効。

## 処理の流れ

```
POST /api/judge {session_id, user_id, message}
  ├ 所有権チェック（user_id と sessions.user_id が一致）。フェーズはセッションのもの
  ├ 同じ本文の連続再送 → API を呼ばず直前の結果（input_type=resend）
  ├ ai_classify：作問 / 対話
  ├ 作問 → ai_judge：{valid, structure, unknown, issue}
  │        → main.py：response_type（form / praise / done）を決定論的に決める
  │        → ai_dialogue：声かけ（フェーズ1・3は「おくったよ」のみ）
  └ 対話 → talk（困り表明は stuck_count に数える）
  → chat_logs に全ターン記録（phase, expression, input_type, valid, structure, unknown, issue, is_new,
     response_type, prompt_strength, produced_structures, stuck_count, miss_count, latency_ms ほか）
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
  main.py          FastAPI・フェーズ管理・応答の種類の決定・管理者認証
  config.py        環境変数・式のパース・ENABLE_FIGURES
  database.py      SQLite（sessions / chat_logs / app_config / phase_changes）・旧スキーマの退避・CSV
  ai_judge.py      3層判定（成立性→構造→求める量）
  ai_dialogue.py   応答の種類ごとの声かけ（定型＋LLM＋ガード）
  ai_classify.py   作問／対話の分類
  kanji_rule.py    文字づかいルール
  tests/test_flow.py     遷移・認証・ログ・CSV の決定論テスト（LLMモック）
  tests/judge_cases.py   判定精度テスト（実API・35件）
frontend/
  index.html / index.js / index.css   児童画面
  admin.html / admin.js / admin.css   管理画面（フェーズ管理・児童の状態・ログ・CSV）
data/sakumon.db
```

## DB

- `data/sakumon.db`（Render は `DATABASE_PATH=/data/sakumon.db`）。時刻は JST。
- 起動時にテーブルが無ければ作る。旧スキーマ（9/14 以前）が残っていれば `*_legacy_日付` に改名して退避し（DROP しない）、新スキーマで作り直す。
- スキーマは `sakumon_cleanup_spec.md` 3章。列追加が必要になったら `database.SCHEMA` を変更し、既存 DB には ALTER TABLE を当てる。

## 起動

```
cd backend && uvicorn main:app --reload --port 8000
```

`.env` に `ANTHROPIC_API_KEY` と `ADMIN_PASSWORD` が必要（`.env.example` 参照）。
`ADMIN_PASSWORD` が無いと起動しない。Render では Environment に設定する。

## 管理画面 `/admin`

HTTP Basic 認証（ユーザー名は任意、パスワード＝`ADMIN_PASSWORD`）。

- フェーズ管理：現在のフェーズ、フェーズ1／2／3の切替、式A・式Bの編集
- 児童の状態（5秒更新）：接続・提出数・成立数・到達3灯・困り／不成立の回数・直近の応答。提出0問の児童を赤で強調
- 1児童1フェーズ1セッション（`UNIQUE(user_id, phase)`）。リハーサルのデータは本番前に「児童一覧・ログ」から削除する
- 児童一覧・ログ・CSV

運用手順は `授業当日の運用手順_0918.md` を参照。

## アクセス

| URL | 説明 |
|-----|------|
| https://sakumon.onrender.com/ | 児童向け作問画面 |
| https://sakumon.onrender.com/admin | 管理者画面（要パスワード） |
