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
| 2 | 支援：フィードバックを受けながら作問（新セッション。フェーズ1は引き継がない） | 式A | ＋ AIの声かけ、「作ったお話」（常設）、信号機（ラベルなしの3灯。フェーズ2のあいだ常時）、目標の固定表示（予告があるあいだ） |
| 3 | 事後測定：別の式で自由に作問（新セッション） | 式B | フェーズ1と同じ |

フェーズ・式は `app_config` テーブルにあり、管理画面（`/admin`）から切り替える。
児童の画面は5秒ごとに `/api/config` を確認し、変わったら自動で切り替わる。

## 支援（フェーズ2）— `docs/sakumon_spec_v3.md`

支援は **4段階**。中は「役割指定＋題材固定」、強は「場面文提示」を意味する。

| response_type | 強度 | 内容 |
|---|---|---|
| `form` | ─ | 不成立作問への形式の支援（issue に応じて1点だけ。定型。全 issue に専用文言：`missing_condition`（問いはあるが除数の条件が無い）・`incomplete_text`・`reversed`・`scene_contradiction` を分離） |
| `praise` | 0 促し | 新しい問題の称賛／同じ構造1回目（定型。「**4が ちがう 数を 表す** 問題は 作れるかな？」＝除数の役割の軸） |
| `prompt` | 1 弱 | **役割の宣言（2ターン）**。ターン1「◯ばんの お話で、4は 何を あらわして いるかな？」→ 答えを分類（`role_answer`）。判定の役割と食い違えば**1回だけ**児童の問題文の除数の句を引用して問い返す（`role_corrected`。正解の役割名は言わない）→ ターン2「じゃあ 次は、4を 何の 数に して みたい？」→ 予告（`declared_by=child`） |
| `prompt` | 2 中 | **役割指定＋題材固定**「えんぴつの お話は そのままで いいよ。4を「1人分の 数」に して みよう。」システムが未到達構造を目標に指定（`declared_by=system`） |
| `prompt` | 3 強 | **場面文提示**「「えんぴつが 24本 あります。1人に 4本ずつ 分けます。」 この あとに、求める 文を 書いて みよう。」 |
| `done` | ─ | 3構造そろった |
| `talk` | ─ | 作問以外の入力。LLM が**現在の強度・目標・到達構造・成立問題（本文と、除数が指していたもの）・直前のやりとり（児童の最新入力＋判定理由＋AI の返事）**を参照し、「現在の強度が許す情報だけを話す」境界で声かけを作る（0：促しのみ＝直前のフィードバックの説明・励まし。除数の役割を問わない・言わない／1：除数が何かを問い返す／2：除数をどうしてほしいか直接言う・3語OK・題材固定OK／3：場面文も渡す。答え・構造名は常に禁止）。児童の発話が直前のフィードバックへの疑問なら、まずそれを児童の問題文に即して説明する。境界違反はコード側ガード（`violates_boundary`。強度0の役割の問いは `role_question`）→ 理由を添えて1回再生成 → なお違反なら定型文。同時に支援要求かどうかを判定（`is_help_request`） |
| `error` | ─ | judge が API 不通「もう一度 おくって みてね」（一覧に載せない） |

### カウンタと強度の規則

3つのカウンタ（`sessions` に保持、フェーズスコープ）：

- `stuck`：新しい構造に到達しなかった成立作問の**連続**回数
- `miss`：予告（宣言）と産出の不一致の累積回数
- `help`：`talk` が「支援要求」（ヒント・わからない・思いつかない・どうすればいい）に分類された回数

強度（`sessions.strength`）は状態として保持し、`main.decide_strength` で遷移させる：

> **強度0→1 は stuck が2に達したとき、または help が1以上になったとき。強度1以降は stuck・miss・help のいずれか1件でも増えるたびに+1（上限3）。新構造到達で全カウンタと強度を0に戻す。**

同じターンで stuck と miss が両方増えても +1 は1回。不成立・役割の答え・予告・再送では動かない。3つそろった後は動かない。miss だけでは 0→1 にならない。

この規則は Wood & Middleton の随伴的指導の原則（失敗で1段強め、成功で1段弱める）に基づく設計判断であり、**閾値の具体的な数値（0→1 は stuck=2、上限3）は先行研究から直接導かれたものではない**。強度0での help による増強（9/17 追加）は、明示的な援助要求を随伴的指導の原則（Wood & Middleton 1975）における支援増強の契機として扱うもの。`strength_trigger` は `help`。

help で 0→1 に上がったターンは、talk の文言（強度0で生成）の後ろに弱のターン1（「◯ばんの お話で、4は 何を あらわして いるかな？」）を連結し、役割の宣言をそこから始める（成立作問が無ければ連結しない）。

- 文言は仕様の表を一字一句（`ai_dialogue.py`）。児童向けに「種類」「たずねる」「聞いていること」・構造名は出さない。弱では構造ラベル（1つ分の 大きさ／いくつ分／何倍）も出さない
- 中・強の {物}{unit} は `ai_judge` の判定出力（`item` / `unit`）から。取れなければ「◯ばんの お話」「もの」「こ」
- 倍の強の人物名は固定名にしない（「たろう」「はなこ」をセッションごとに周期選択、相手は「お友だち」）
- 式は出席番号の奇偶×フェーズ（`config.EXPRESSION_ASSIGNMENT`：奇数 21÷3/24÷4/30÷5、偶数 30÷5/24÷4/21÷3。設定はここ1箇所で、管理画面の表・「式を変更」の選択肢・テストの期待値はすべてここから引く）。フェーズ2 は 9/1 の紙の調査と同じ 24÷4（理由は「式の設定」の節）。管理画面からセッション単位で上書き可（選択肢は設定表に現れる式）
- 図（テープ図）は `config.ENABLE_FIGURES=False` で無効

## 処理の流れ

```
POST /api/judge {session_id, user_id, message, declaring}
  ├ 所有権チェック（user_id と sessions.user_id が一致）。フェーズ・式はセッションのもの
  ├ 判定済み本文の連続再送 → API を呼ばず直前の結果（input_type=resend）
  ├ 作問 / 対話の振り分け
  │    直前の AI の発話が児童への問い（chat_logs.awaiting：弱のターン1・2、talk の問い返し）
  │        → LLM 分類を使わず、数量2つ以上＋問いの文で終わる完全な問題文だけ作問。それ以外は対話
  │    直前が form（不成立の指摘）で、入力が短い断片（12字以下・問いで終わらない・場面の動詞なし）→ 対話
  │    それ以外 → ai_classify（Haiku）
  ├ 作問（フェーズ2）→ ai_judge：{valid, structure, unknown, issue, item, unit}
  │        → main.py：状態機械で response_type / strength / declared を決める
  │        → ai_dialogue：文言
  ├ 作問（フェーズ1・3）→ 本文を judge_status=pending で保存して即「おくったよ」
  │        → judge_queue が応答後に ai_judge を実行し同じ行を埋める（児童ごとに送信順で直列・児童をまたいで並列 8。
  │          llm_call の内部リトライのあと 10 秒・30 秒あけて再試行 → なお失敗なら judge_status=failed・issue=error）
  │        → 管理画面「未判定・失敗を再判定」で pending / failed を再投入（サーバ再起動・API エラーの復旧）
  │        → 分類（ai_classify）に失敗した入力は、フェーズ1・3では作問（pending）に倒す（フェーズ2は対話）
  ├ 対話モード（declaring=true）で作問以外 → 直前のログ行から待っているターンを決める
  │        role        → 役割の答え（classify_role → 訂正 or ターン2の問い）  input_type=role
  │                      （弱のターン1の直後、訂正の直後、talk が役割を問うた直後（awaiting=role）のどれでも同じ）
  │        declaration → 予告（classify_declaration → declared）              input_type=declaration
  └ それ以外 → talk（LLM に強度・目標・到達構造・成立作問・直前のやりとりを渡す。is_help_request なら help+=1 → 強度更新。
                     0→1 なら ROLE_ASK を連結して役割待ち。talk が役割を問うたら awaiting=role）
  → chat_logs に全ターン記録（phase, expression, input_type, valid, structure, unknown, issue, is_new, item, unit,
     response_type, prompt_strength, declared_*, role_answer, role_corrected, is_help_request,
     produced_structures, stuck_count, miss_count, help_count, strength, strength_trigger, target_structure, awaiting, latency_ms,
     judge_status（pending / done / failed。作問行のみ）, judged_at）
```

### ai_judge の出力

```
valid:     true / false
structure: tobun / hougan / bai / invalid
unknown:   one_unit（1つ分）/ num_units（いくつ分）/ ratio（倍率）/ base（基準量）/ rate（割合）/ null
issue:     null / scene_contradiction / wrong_number / reversed / missing_condition / incomplete_text / wrong_operation / no_question / not_problem
item:      被除数が数えている物の名前（中・強の文言の {物}。読み取れなければ null）
unit:      被除数に付く助数詞（{unit}。読み取れなければ null）
```

倍は「乗法的比較の場面」全体（倍率・基準量・割合）。基準量を問う形（□×4=24）も成立・倍。
比較の向きが逆（4÷24 になる）は `reversed`（使う数がちがう `wrong_number` と区別。専用文言あり）。
問いはあるが除数にあたる条件（4まいずつ・4人で）が場面に無いものは `missing_condition`（「途中で切れ」「問いなし」と分ける）。
`valid=false` なのに理由が無いときは本文の形から寄せる（`_fallback_issue`：問いがあり被除数はあるが除数が無い → `missing_condition`）。`not_problem` は場面の文も数も無いときだけ。

---

## ファイル構成

```
backend/
  main.py          FastAPI・フェーズ管理・応答の種類の決定・管理者認証
  config.py        環境変数・式のパース・ENABLE_FIGURES
  database.py      SQLite（sessions / chat_logs / app_config / phase_changes）・旧スキーマの退避・CSV
  ai_judge.py      3層判定（成立性→構造→求める量）
  ai_dialogue.py   応答の種類ごとの声かけ（定型＋LLM＋ガード）
  ai_classify.py   作問／対話の分類・予告の分類・除数の役割の答えの分類
  judge_queue.py   フェーズ1・3の応答後の判定キュー（同時実行の上限・児童ごとの直列・失敗の再試行・再投入）
  kanji_rule.py    文字づかいルール
  tests/test_flow.py       遷移・認証・ログ・CSV・応答後の判定の決定論テスト（LLMモック）
  tests/test_llm_call.py   llm_call のリトライ・セマフォ
  tests/judge_cases.py     判定精度テスト（実API・35件）
  tests/dialogue_probe.py  talk の実出力の確認（実API・強度0〜3）
docs/
  sakumon_spec_v3.md   現行の支援仕様（4段階・3カウンタ・役割の宣言・強度依存の対話）
  sakumon_spec_v2.md   9/15 時点の仕様（履歴。冒頭の注記を参照）
frontend/
  index.html / index.js / index.css   児童画面
  admin.html / admin.js / admin.css   管理画面（フェーズ管理・児童の状態・ログ・CSV）
data/sakumon.db
```

## DB

- `data/sakumon.db`（Render は `DATABASE_PATH=/data/sakumon.db`）。時刻は JST。
- 起動時にテーブルが無ければ作る。旧スキーマ（9/14 以前）が残っていれば `*_legacy_日付` に改名して退避し（DROP しない）、新スキーマで作り直す。
- スキーマは `database.SCHEMA`。列追加が必要になったら `SCHEMA` を変更し、`database._MIGRATIONS` に載せて既存 DB には ALTER TABLE を当てる（起動時に自動）。

## 起動

```
cd backend && uvicorn main:app --reload --port 8000
```

`.env` に `ANTHROPIC_API_KEY` と `ADMIN_PASSWORD` が必要（`.env.example` 参照）。
`ADMIN_PASSWORD` が無いと起動しない。Render では Environment に設定する。

## 管理画面 `/admin`

HTTP Basic 認証（ユーザー名は任意、パスワード＝`ADMIN_PASSWORD`）。

- フェーズ管理：現在のフェーズ、フェーズ1／2／3の切替、式の割り当て表（奇偶×フェーズ）、判定の状態（未判定／失敗／完了）と「未判定・失敗を再判定」
- 児童の状態（5秒更新）：接続・提出数・成立数・到達数・予告（誰が・何を）・反復／不一致／支援要求・強度・直近の応答。提出0問の児童を赤で強調
- 児童詳細：セッションごとの式の上書き、予告と産出の一致／不一致、役割の答えと訂正、支援要求、強度・trigger・目標
- 1児童1フェーズ1セッション（`UNIQUE(user_id, phase)`）。リハーサルのデータは本番前に「児童一覧・ログ」から削除する
- 児童一覧・ログ・CSV

運用手順は `授業当日の運用手順_0918.md` を参照。

## アクセス

| URL | 説明 |
|-----|------|
| https://sakumon.onrender.com/ | 児童向け作問画面 |
| https://sakumon.onrender.com/admin | 管理者画面（要パスワード） |
