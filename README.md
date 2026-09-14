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

## 予告支援（フェーズ2）— `docs/sakumon_spec_v2.md`

作問が成立するたびに、状態機械（`stuck`=同構造の連続回数、`miss`=予告不一致の累積）で強さを決める。

| response_type | 強さ | 内容 |
|---|---|---|
| `form` | ─ | 不成立作問への形式の支援（issue に応じて1点だけ。定型） |
| `praise` | 0 | 新しい問題の称賛／同じ構造1回目（定型） |
| `prompt` | 1 弱 | 「つぎは何を求める問題にする？」→ 自由記述の予告（`/api/declare`、LLM で分類、`declared_by=child`） |
| `prompt` | 2 中 | 自己ラベル（自由記述 → 3択、`/api/self_label`）→ システムが未到達構造を目標に指定（`declared_by=system`）。自己ラベルが判定と違っても訂正しない |
| `prompt` | 3 強 | 目標の指定＋場面の固定「◯ばんの お話は そのままで いいよ」 |
| `done` | ─ | 3構造そろった |
| `talk` | ─ | 作問以外の入力（LLM＋ガード。休けい・終了は提案しない） |
| `error` | ─ | judge が API 不通「もう一度 おくって みてね」（一覧に載せない） |

- 強さ：stuck 2→弱、3→中、4以上→強／miss 1→中、2以上→強（大きい方）。新構造到達で両方 0。不成立・対話・再送では動かさない。
- 文言は仕様3章の表を一字一句（`ai_dialogue.py`）。児童向けに「種類」「たずねる」「聞いていること」・構造名は出さない。
- 式は出席番号の奇偶×フェーズ（`main.EXPRESSION_ASSIGNMENT`：奇数 24÷6/24÷8/24÷3、偶数 24÷3/24÷8/24÷6）。管理画面からセッション単位で上書き可。
- 図（テープ図）は `config.ENABLE_FIGURES=False` で無効。

## 処理の流れ

```
POST /api/judge {session_id, user_id, message}
  ├ 所有権チェック（user_id と sessions.user_id が一致）。フェーズ・式はセッションのもの
  ├ 判定済み本文の連続再送 → API を呼ばず直前の結果（input_type=resend）
  ├ ai_classify：作問 / 対話
  ├ 作問 → ai_judge：{valid, structure, unknown, issue}
  │        → main.py：状態機械で response_type / prompt_strength / declared を決める
  │        → ai_dialogue：文言（フェーズ1・3は「おくったよ」のみ）
  └ 対話 → talk
POST /api/declare {session_id, user_id, text}        予告（弱）→ input_type=declaration
POST /api/self_label {session_id, user_id, text|choice}  自己ラベル（中）→ input_type=self_label
  → chat_logs に全ターン記録（phase, expression, input_type, valid, structure, unknown, issue, is_new,
     response_type, prompt_strength, declared_*, self_label*, produced_structures, stuck_count, miss_count, latency_ms）
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

- フェーズ管理：現在のフェーズ、フェーズ1／2／3の切替、式の割り当て表（奇偶×フェーズ）
- 児童の状態（5秒更新）：接続・提出数・成立数・到達数・予告（誰が・何を）・反復／不一致・直近の応答。提出0問の児童を赤で強調
- 児童詳細：セッションごとの式の上書き、予告と産出の一致／不一致、自己ラベルと判定の一致
- 1児童1フェーズ1セッション（`UNIQUE(user_id, phase)`）。リハーサルのデータは本番前に「児童一覧・ログ」から削除する
- 児童一覧・ログ・CSV

運用手順は `授業当日の運用手順_0918.md` を参照。

## アクセス

| URL | 説明 |
|-----|------|
| https://sakumon.onrender.com/ | 児童向け作問画面 |
| https://sakumon.onrender.com/admin | 管理者画面（要パスワード） |
