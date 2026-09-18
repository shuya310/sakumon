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
- 支援は docs/spec/sakumon_spec_v3.md ＋ v3.1 追補 sakumon_spec_v3_1.md（2026-09-17）＋ v3.2 追補 sakumon_spec_v3_2.md（2026-09-18。文言を「求めるもの」軸に統一）。状態は sessions に保持
  （declared / declared_by / declared_text / stuck_count / miss_count / help_count / strength）
  - 児童に持たせる枠は「求めるもの」＝問いの文で答えること（何人か／1人分が何こか／何倍か）。除数の使い方は「求めるものを変える手段」で、中（2）で初めて目標と結んで言う。
    v3.1 の「4が ちがう ものの 数に なる」は 9/18 模擬で題材の変更と読まれ、強度0で意味を説明できなかった（README「支援」節に経緯）
  - 4段階＝「どこまで示すか」：0=促し（何も示さない）／1=弱（現在地＝これまでの問題は同じだった、を児童の除数の句＋問いの疑問語で示し、宣言させる。1ターン）
    ／2=中（目標＝何を求めるか＋手段＝除数をどう使うかをシステムが指定＋題材固定）／3=強（場面文。求める文だけ書かせる）。文言は遷移（分ける系の中／分ける→倍／倍→分ける）で決まる
  - stuck = 新構造に到達しなかった成立作問の**連続**回数、miss = 予告不一致の累積、help = taiwa が支援要求に分類された回数。
    不成立・宣言・resend では動かさない。新構造到達で3カウンタと強度を全て 0
  - 強度は状態（main.decide_strength で遷移）：0→1 は stuck が 2 に達したとき、help が増えたとき（trigger=help）、または強度0で作問のあと
    対話（taiwa）だけが TALK_STALL_TURNS=3 ターン続いたとき（trigger=talk。v3.1。9/17 模擬実践で強度0の対話が7ターン空回りしたため）。
    miss だけでは 0→1 にならない。1 以降は stuck/miss/help のどれかが増えたターンごとに +1（上限3。同時に増えても1回。talk では上げない）。
    help で上げたら、児童が作問（不成立でも）か宣言をするまで help ではもう上げない（v3.2。database.help_raised_since_last_sakumon。help_count は数える。
    9/18 模擬で「わかんない」「どういうことですか」の2ターンで 1→3 に飛んだため）。
    strength_trigger は上げた原因（上がらなければ none）。閾値は Wood & Middleton の原則に基づく設計判断で先行研究由来ではない
  - help / talk で 0→1 に上がったターンは talk の文言の後ろに弱の文言を連結して宣言待ち（awaiting=declaration・dialog=declaration）。成立作問が無ければ連結しない
  - help で 1→2・2→3 に上がったターンは talk の文言を捨てて TALK_LEADIN「そっか。じゃあ、こうして みよう。」＋中／強の文言（目標＝既存の宣言か pick_unreached_structure。
    題材は最新の成立作問）。据え置き（上限3）や成立作問なしでは出さない。9/18 模擬で上部ラベルだけ変わって中の指示が出なかった／旧強度の talk が「くらべる ような」と示唆して中の「分ける 人の 数」と矛盾したため
  - response_type: form / praise / prompt / talk / done / error。定型文は仕様の表を一字一句（言い換えない）。`**…**` は強調
  - 称賛（強度0）は児童自身の問いの文を引用「新しい 問題が できたね！ この お話は、『何人に くばれますか』を 求める お話だね。今度は、**求める ものが ちがう** お話は 作れるかな？」
    問いの文は extract_question_phrase（正規表現：末尾から見て最初の疑問文）で chat_logs.question_phrase に、疑問語は interrogative_of（何人か／何こか／何倍か）。
    除数の句も引き続き extract_divisor_phrase（LLM → 除数を含む文 → なし。判定と並行）で chat_logs.divisor_phrase に残す（弱で使う）。取れなければ引用の文を落とす
  - 3つそろった後の成立作問は POST_DONE「{ref_no}ばんも できたね。この お話は『{question}』を 求める お話だね。」（完了文をくり返さない。促しなし。対話は支援なし）
  - 完了文 done_message：各構造に最初に到達した問題の番号＋疑問語「3つ とも できたね！ 1ばんは『何人か』、3ばんは『何こか』、4ばんは『何倍か』。同じ 24÷4 なのに、
    求める ものが ぜんぶ ちがう お話に なったね。時間まで、もっと 作って みよう。求める ものが 同じでも、ちがう お話なら いいよ。」（疑問語が欠ける・重なるなら番号だけ）
  - 「求めるものって何？」（asks_what_motomeru。強度0・1）は LLM を呼ばず定型 MOTOMERU_WHAT_MESSAGE（最新の成立作問の問いの文を指す。行き先は言わない。help に数えない）
  - 弱＝現在地の対比＋宣言（1ターン。ai_dialogue.weak_variant で変形。何が同じかを児童の言葉2つ＝除数の句と問いの疑問語で言う）：
    same「{no1}ばんの『{phrase1}』も {no2}ばんの『{phrase2}』も、{divisor}の 使い方は 同じで、どちらも **{what}**を 求める お話だね。」
    ／wakeru（等分除・包含除を両方到達。各構造の最初の問題）「{noA}ばんは {whatA}、{noB}ばんは {whatB}を 求めたね。どちらも {dividend}{unit}を 分ける お話だね。じゃあ 次は、…分けない お話に するなら、何を 求める？」
    ／kuraberu（倍だけ2問以上）「{no1}ばんも {no2}ばんも、…くらべて、**{what}**を 求める お話だね。…くらべない お話に するなら、」
    ／one（1問）「{no}ばんは、{divisor}を『{phrase}』と 使って、**{what}**を 求める お話だったね。」（句・疑問語が取れなければその部分を落とす *_NOPHRASE/_NOWHAT/_BARE）
    → 共通「じゃあ 次は、何を 求める お話に する？」。この問いは宣言させる問い（意図性Aの測定点）で、気づかせる問いではない
  - 宣言の答えは classify_declaration（式を渡す）→ 構造なら declared_by=child・declared_text=児童の言葉「じゃあ、その お話を 作って みよう。」
    （到達済み構造の誤答も訂正しない。結果は stuck/miss で拾う）／unknown でも中身のある言葉（「折り紙の数」等）は引き取る：構造なし・declared_by=child・
    declared_text=児童の言葉（上部「つぎは」に出す。target_label は構造なしでも児童の言葉を返す）「『{言葉}』だね。じゃあ、その お話を 作って みよう。」
    ／「わからない」系（ai_dialogue.looks_like_dontknow）なら立てず「わからなくても だいじょうぶ。じゃあ、求める ものが ちがう お話を 作って みよう。」
    classify_declaration は求めるもの型（「何こか」「1人分」→tobun、「何人か」→hougan、「何倍か」→bai）と除数の使い方型（「4人で分ける」→tobun、「4こずつ」→hougan）の両方を受ける
    どちらも1回で閉じる（awaiting 解除。答えの中身を追う対話に入らない＝宙に上げる）。旧 v3 の役割の宣言（ROLE_ASK・classify_role・訂正・role_answer）は廃止（列は残存・未使用）
  - 入力欄は1つ。対話モードの入力は /api/judge に declaring=true。待っているかはサーバが直前のログ行の awaiting=declaration で決める（_pending_dialog）。
    作問なら通常処理（打ち切り）。3択・/api/self_label は廃止（self_label* 列は残存・未使用）
  - chat_logs.awaiting（declaration / answer / NULL。旧 role は残存）＝そのターンの後にサーバが児童の何を待つか。awaiting 中の入力は LLM 分類を使わず
    ai_classify.looks_like_problem（数量2つ以上＋問いの文で終わる）だけを作問にする。awaiting 中は同じ本文でも「答え」（再送にしない）。
    form の直後の短い断片（looks_like_fragment）は対話
  - 中・強：pick_unreached_structure を declared=system で立てて文言（中「1人分が 何{unit}かを 求める お話に して みよう。{divisor}を、分ける 人の 数に するよ。{item}の お話は そのままで いいよ。」
    ／「何人に 分けられるかを 求める お話に して みよう。{divisor}を、1人が もらう 数に するよ。…」／「{dividend}{unit}は {divisor}{unit}の 何倍かを 求める お話に して みよう。{divisor}を、くらべる 相手の 数に するよ。…」。
    強「「{item}が {dividend}{unit} あります。{divisor}人で 同じ 数ずつ 分けます。」つづきの、求める 文を 書いて みよう。」（倍は「つづきの、何倍かを 求める 文を」）等）。中・強では児童は宣言しない。
    {item}{unit} は ai_judge の item/unit（取れなければ「◯ばんの お話」「もの」「こ」）。倍の強の人物名は FRIEND_NAMES を session_id で周期選択、相手は「お友だち」
  - 画面上部「つぎは「…」」（target_label）は構造ラベル単独を出さない：児童の宣言はその言葉（declared_text。訂正しない）、システム指定は中の文言の目標部分と同じ文
    （TARGET_LABEL「1人分が 何{unit}かを 求める お話」「何人に 分けられるかを 求める お話」「{dividend}{unit}は {divisor}{unit}の 何倍かを 求める お話」）。
    9/17 に「何倍」単独の表示が「倍の問題を作るの？」を誘発した。表示は児童の入力では変わらず、成立作問で目標が消費されたときだけ変わる
  - talk（LLM）：_talk_context で強度・目標・成立作問一覧（本文＋問いの文＋除数の役割＋物）＋直前のやりとり（last_turn：児童の最新入力・判定 issue・AI の返事）を渡す。
    児童の発話が直前のフィードバックへの疑問なら、まず児童の問題文に即して説明（答え・修正文は言わない）。境界は強度依存
    （0：促しのみ＝役割を問わない・言わない（ガード role_question）・「分ける／くらべる お話」と分類しない（banned_classify）・行き先（求めるもの・除数の使い方）の指定禁止（role_spec）・3語禁止。
    「求めるもの」の意味は児童の問いの文を引用して示してよい
    ／1：現在地（作った問題が同じ）を児童の句と疑問語で示してよい・「何を 求める お話に する？」と問い返しOK・行き先の指定禁止（role_spec）・3語禁止／2：目標＋手段の指定・3語・題材固定OK／3：場面文OK。
    答え・構造名・次の問題の問いの文（question_form。「1人分は 何こに なるか、という お話」「〜ですか」）は常に禁止。児童自身の問いの文の引用（『…』。言い換えでも疑問語が児童の問いと同じなら）は
    ガードの対象外（own_texts）。禁止語彙は空白を除いても照合）。violates_boundary(…, strength, own_texts)。違反は理由を添えて1回再生成 → なお違反なら定型文。
    出力の is_help_request が true なら help+=1 → 強度更新（文言は更新前の強度。2以上で目標が無ければ立てる）。talk が問い返したら awaiting=answer
  - 弱・称賛に構造ラベル（1つ分の 大きさ／いくつ分／何倍）・行き先（役割名）を出すのは禁止（児童自身の疑問語「何倍か」の引用は除く）
  - 式は config.EXPRESSION_ASSIGNMENT（奇偶×フェーズ。奇数 21÷3/24÷4/30÷5、偶数 30÷5/24÷4/21÷3。フェーズ2 は 9/1 の紙の調査と同じ 24÷4。
    設定はここ1箇所。管理画面の表・「式を変更」の選択肢 EXPRESSION_CHOICES・DEFAULT_EXPRESSION_A/B・テストの期待値もここから引く。main.EXPRESSION_ASSIGNMENT は別名）。セッション開始時に sessions.expression に固定。
    管理画面からセッション単位で上書き可（/admin/api/sessions/{id}/expression。UI はプルダウン3択）。app_config.expression_a/b は未使用
  - 語彙：児童向けに「種類」「たずねる」「聞いていること」「ちがうことを聞く」を出さない（LLM 出力もガード）
- ai_judge の issue に reversed（比較の向きが逆）・missing_condition（問いはあるが除数の条件が無い。9/17）。FORM_MESSAGES は全 issue 1対1（alias は wrong_operation のみ）。
  理由なしの不成立は _fallback_issue で本文から寄せる（問いあり・被除数あり・除数なし → missing_condition。not_problem は場面も数も無いときだけ）
- judge が全リトライ失敗 → issue='error'・response_type='error'「もう一度 おくって みてね」（一覧に載せない。送り直しは判定し直す）。判定済み本文の連続再送は input_type='resend'（API を呼ばない・カウンタ不動。フェーズ2で直前の ai_message が無くても「おくったよ」は出さない）。
  フェーズ2で成立作問と同一本文（直前でなくても）は判定せず「その お話は もう ◯ばんに あるよ。」（resend）
- フェーズ1・3の作問は judge_status='pending' で保存して即「おくったよ」→ judge_queue（応答後）が ai_judge を実行して同じ行を埋める
  （児童ごと送信順で直列＝is_new は送信順。並列 JUDGE_BG_WORKERS=8。失敗は JUDGE_BG_RETRY_WAITS=10,30 秒あけて再試行 → failed）。
  フェーズ2は同期のまま（保存時に done/failed）。pending/failed は管理画面「再判定」（/admin/api/rejudge）で再投入。
  リハーサル分の片づけは管理画面「退避して全部消す」（/admin/api/reset。confirm=「消す」。CSV 保存 → DB を data/ に複製 → 5テーブルを空に。app_config は残す）
- 右パネル「作った 問題」は全フェーズ（フェーズ1・3は送った作問の全部・判定なし・番号は選択画面と同じ）。信号機はフェーズ2だけ
  フェーズ1・3で classify が失敗した入力は sakumon（pending）に倒す（フェーズ2は taiwa）
- フェーズ1・3「作った お話を 見る」：/api/selection/open（開くたび記録・作問一覧 sakumon 行のみ・判定結果は返さない）／submit（1〜min(3,n)・何度でも・全件記録。
  分析は最後の submit）。selection_events / teacher_calls（管理画面「声がけした」＝時刻のみ）。フェーズ2には無い。CSV に selected 等を追加。
  文言は KANJI_RULE（「選ぶ」「決定」）。この画面の「種類」は意図的（基準を指定しない。README「測定指標」）
- テスト：backend/tests/test_flow.py（LLMモック・決定論。cd backend && ./venv/bin/python tests/test_flow.py。judge_queue.wait_idle() で応答後の判定を待つ）、tests/test_llm_call.py（リトライ・セマフォ）、
  tests/judge_cases.py（実API・判定精度）、tests/dialogue_probe.py（実API・talk の出力）
- 運用手順：授業当日の運用手順_0918.md
