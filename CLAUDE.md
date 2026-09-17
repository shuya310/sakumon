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
- フェーズは app_config テーブル（管理画面 /admin で変更）。式は config.EXPRESSION_ASSIGNMENT（設定テーブル。児童ごとに sessions.expression に固定）
- DBは data/sakumon.db。スキーマは database.SCHEMA（列追加は _MIGRATIONS で既存 DB へ ALTER）。時刻は JST
  - sessions は UNIQUE(user_id, phase)。1児童1フェーズ1セッション。「新しい回（run）」は廃止（リハーサル分は管理画面で削除）
  - 旧スキーマが残っていれば起動時に *_legacy_日付 へ改名して退避（DROP しない）。列追加は SCHEMA 変更＋既存 DB へ ALTER TABLE
  - is_new はフェーズスコープ（user_id + phase で到達構造を引く）
- 支援は docs/sakumon_spec_v3.md（2026-09-17。v2 の 2-4・3-4〜3-6・5-3・7章中段階は v3 で置き換え）。状態は sessions に保持
  （declared / declared_by / stuck_count / miss_count / help_count / strength）
  - 4段階：0=促し／1=弱（役割の宣言・2ターン）／2=中（役割指定＋題材固定）／3=強（場面文提示）
  - stuck = 新構造に到達しなかった成立作問の**連続**回数、miss = 予告不一致の累積、help = taiwa が支援要求に分類された回数。
    不成立・役割の答え・予告・resend では動かさない。新構造到達で3カウンタと強度を全て 0
  - 強度は状態（main.decide_strength で遷移）：0→1 は stuck が 2 に達したとき、または help が増えたとき（9/17。明示的な援助要求を
    随伴的指導の原則の支援増強の契機として扱う。trigger=help）。miss だけでは 0→1 にならない。1 以降は stuck/miss/help のどれかが増えたターンごとに +1
    （上限3。同時に増えても1回）。strength_trigger は上げた原因（上がらなければ none）。閾値は Wood & Middleton の原則に基づく設計判断で先行研究由来ではない
  - help で 0→1 に上がったターンは talk の文言の後ろに ROLE_ASK を連結して役割待ち（awaiting=role・dialog=role）。成立作問が無ければ連結しない
  - response_type: form / praise / prompt / talk / done / error。定型文は仕様の表を一字一句（言い換えない）。`**…**` は強調
  - 弱＝役割の宣言：ターン1「{n}ばんの お話で、{divisor}は 何を あらわして いるかな？」→ classify_role（people/per_one/base/dont_know/unknown）
    を role_answer に必ず保存。expected_divisor_role（判定の structure/unknown）と不一致で役割が確定できるときだけ1回訂正
    （児童の文の除数の句を引用。役割名は言わない。extract_divisor_phrase → 除数を含む文 → 引用なし）。2回目は正誤にかかわらずターン2
    「じゃあ 次は、{divisor}を 何の 数に して みたい？」→ classify_declaration → declared_by=child。unknown なら立てない
  - 入力欄は1つ。対話モードの入力は /api/judge に declaring=true。どのターンを待っているかはサーバが直前のログ行で決める
    （_pending_dialog: role / declaration。talk が役割を問うた行（awaiting=role）も role）。作問なら通常処理（打ち切り）。3択・/api/self_label は廃止（self_label* 列は残存・未使用）
  - chat_logs.awaiting（role / declaration / answer / NULL）＝そのターンの後にサーバが児童の何を待つか。awaiting 中の入力は LLM 分類を使わず
    ai_classify.looks_like_problem（数量2つ以上＋問いの文で終わる）だけを作問にする。form の直後の短い断片（looks_like_fragment）は対話
  - 中・強：pick_unreached_structure を declared=system で立てて文言。{item}{unit} は ai_judge の item/unit（取れなければ「◯ばんの お話」「もの」「こ」）。
    倍の強の人物名は FRIEND_NAMES を session_id で周期選択、相手は「お友だち」
  - talk（LLM）：_talk_context で強度・目標・成立作問一覧（本文＋除数の役割＋物）＋直前のやりとり（last_turn：児童の最新入力・判定 issue・AI の返事）を渡す。
    児童の発話が直前のフィードバックへの疑問なら、まず児童の問題文に即して説明（答え・修正文は言わない）。境界は強度依存
    （0：促しのみ＝役割を問わない・言わない（ガード role_question）・3語禁止／1：問い返しのみ・3語禁止／2：役割指定・3語・題材固定OK／3：場面文OK。
    答え・構造名は常に禁止）。violates_boundary(…, strength)。違反は理由を添えて1回再生成 → なお違反なら定型文。
    出力の is_help_request が true なら help+=1 → 強度更新（文言は更新前の強度。2以上で目標が無ければ立てる）。talk が役割を問うたら awaiting=role
  - 弱に構造ラベル（1つ分の 大きさ／いくつ分／何倍）を出すのは禁止。自己ラベルの訂正禁止は3択廃止で失効
  - 式は config.EXPRESSION_ASSIGNMENT（奇偶×フェーズ。奇数 21÷3/24÷4/30÷5、偶数 30÷5/24÷4/21÷3。フェーズ2 は 9/1 の紙の調査と同じ 24÷4。
    設定はここ1箇所。管理画面の表・「式を変更」の選択肢 EXPRESSION_CHOICES・DEFAULT_EXPRESSION_A/B・テストの期待値もここから引く。main.EXPRESSION_ASSIGNMENT は別名）。セッション開始時に sessions.expression に固定。
    管理画面からセッション単位で上書き可（/admin/api/sessions/{id}/expression。UI はプルダウン3択）。app_config.expression_a/b は未使用
  - 語彙：児童向けに「種類」「たずねる」「聞いていること」「ちがうことを聞く」を出さない（LLM 出力もガード）
- ai_judge の issue に reversed（比較の向きが逆）・missing_condition（問いはあるが除数の条件が無い。9/17）。FORM_MESSAGES は全 issue 1対1（alias は wrong_operation のみ）。
  理由なしの不成立は _fallback_issue で本文から寄せる（問いあり・被除数あり・除数なし → missing_condition。not_problem は場面も数も無いときだけ）
- 称賛は「{divisor}が ちがう 数を 表す 問題」（除数の役割の軸。9/17）
- judge が全リトライ失敗 → issue='error'・response_type='error'「もう一度 おくって みてね」（一覧に載せない。送り直しは判定し直す）。判定済み本文の連続再送だけ input_type='resend'（API を呼ばない・カウンタ不動）
- テスト：backend/tests/test_flow.py（LLMモック・決定論。cd backend && ./venv/bin/python tests/test_flow.py）、tests/test_llm_call.py（リトライ・セマフォ）、
  tests/judge_cases.py（実API・判定精度）、tests/dialogue_probe.py（実API・talk の出力）
- 運用手順：授業当日の運用手順_0918.md
