// ===== 設定フラグ =====
// テープ図・構造図は今回すべて無効化（数量関係そのものを図で渡してしまうため）。
// 描画コードは末尾に残してあるが、このフラグが false の間は呼ばれない。
const ENABLE_FIGURES = false;

const state = {
  userId: null,
  sessionId: null,
  phase: null,
  expression: "",
  showSupport: false,
  history: [],
  problems: [],
  allReached: false,
  sending: false,
  dialog: null,           // null / role（弱・ターン1：除数の役割の答え待ち）/ declaration（弱・ターン2：予告待ち）
  pollTimer: null,
  pollSeconds: 5,
  switching: false,
};

// ===== Screen =====
function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

// ===== sessionStorage =====
function saveSession() {
  sessionStorage.setItem("userId", state.userId || "");
}
function clearSession() {
  sessionStorage.removeItem("userId");
  sessionStorage.removeItem("sessionId");
}

// ===== API =====
async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* noop */ }
  return { ok: res.ok, status: res.status, data };
}

// ===== Login screen =====
const ID_RE = /^[0-9]{2}$/;
const inputId = document.getElementById("input-id");
const btnLogin = document.getElementById("btn-login");
const loginError = document.getElementById("login-error");

inputId.addEventListener("input", () => { loginError.textContent = ""; });
btnLogin.addEventListener("click", doLogin);
inputId.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

// 「次へ」→ 形式チェック → 確認画面（13/31 のような桁の取り違えを本人に見せて防ぐ）
function doLogin() {
  const val = inputId.value.trim().toLowerCase();
  if (!ID_RE.test(val)) {
    loginError.textContent = "数字2文字で入力してね";
    return;
  }
  loginError.textContent = "";
  showConfirm(val);
}

// ===== 出席番号の確認画面（ID入力の直後に1回だけ。復元・フェーズ切替では出さない） =====
const screenConfirm = document.getElementById("screen-confirm");
const btnConfirmYes = document.getElementById("btn-confirm-yes");
const btnConfirmNo = document.getElementById("btn-confirm-no");
let pendingUserId = null;

function showConfirm(val) {
  pendingUserId = val;
  document.getElementById("confirm-number").textContent = val;
  document.getElementById("confirm-number-text").textContent = val;
  btnConfirmYes.disabled = false;
  btnConfirmNo.disabled = false;
  showScreen("screen-confirm");
  // どちらのボタンにもフォーカスを置かない（Enter で意図せず確定させない）
  if (document.activeElement) document.activeElement.blur();
}

// 確認画面ではボタンのクリック以外を受け付けない（Enter / Space で確定しない）
document.addEventListener("keydown", (e) => {
  if (!screenConfirm.classList.contains("active")) return;
  if (e.key === "Enter" || e.key === " ") e.preventDefault();
}, true);

btnConfirmYes.addEventListener("click", async () => {
  const val = pendingUserId;
  if (!val) return;
  btnConfirmYes.disabled = true;
  btnConfirmNo.disabled = true;
  try {
    const { ok, data } = await postJson("/api/login", { user_id: val });
    if (!ok) {
      // 失敗時は入力画面に戻して理由を見せる
      showScreen("screen-login");
      loginError.textContent = (data && data.detail) || "エラーがおきました";
      inputId.focus();
      return;
    }
    pendingUserId = null;
    enter(data);
  } catch (e) {
    showScreen("screen-login");
    loginError.textContent = "つうしんエラーがおきました";
    inputId.focus();
  } finally {
    btnConfirmYes.disabled = false;
    btnConfirmNo.disabled = false;
  }
});

// 「ちがう」→ 入力欄を空にして戻す。エラーは出さない
btnConfirmNo.addEventListener("click", () => {
  pendingUserId = null;
  inputId.value = "";
  loginError.textContent = "";
  showScreen("screen-login");
  inputId.focus();
});

// ===== 入場（ログイン／フェーズ切替後の再入場） =====
function enter(payload) {
  state.userId = payload.user_id;
  state.sessionId = payload.session_id;
  state.phase = payload.phase;
  state.expression = payload.expression;
  state.showSupport = !!payload.show_support;
  state.history = payload.history || [];
  state.allReached = !!payload.all_reached;
  state.problems = [];
  state.pollSeconds = payload.poll_seconds || 5;
  saveSession();

  resetGame();
  (payload.problems || []).forEach(p => addProblem(p.text, p.structure));
  renderConversation(payload.conversation || []);
  addNotice(kickoffText(state.phase, state.expression));
  setTarget(payload.declared, payload.target_label);
  setDialog(state.showSupport ? payload.dialog : null);
  updatePanels();
  showScreen("screen-game");
  document.getElementById("chat-input").focus();
  startPolling();
}

function kickoffText(phase, expression) {
  if (phase === 2) return `ここからは、おくった 問題に 返事が 来るよ。「${expression}」になる 問題を 作ろう。`;
  if (phase === 3) return `新しい式だよ。「${expression}」になる 問題を 作って おくってね。`;
  return `「${expression}」になる 問題を 作って おくってね。`;
}

function resetGame() {
  document.getElementById("chat-log").innerHTML = "";
  document.getElementById("problem-list").innerHTML = "";
  document.getElementById("game-user-name").textContent = state.userId ? `${state.userId} さん` : "";
  document.getElementById("game-expression").textContent = state.expression || "";
  document.getElementById("chat-input").value = "";
}

document.getElementById("btn-logout").addEventListener("click", () => {
  stopPolling();
  // 終了時刻を記録する（失敗しても画面は進める。入り直せば再開になる）
  if (state.sessionId && state.userId) {
    postJson("/api/session/end", { session_id: state.sessionId, user_id: state.userId }).catch(() => {});
  }
  clearSession();
  state.userId = null;
  state.sessionId = null;
  inputId.value = "";
  loginError.textContent = "";
  showScreen("screen-login");
});

// ===== フェーズのポーリング =====
function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollConfig, state.pollSeconds * 1000);
}
function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollConfig() {
  if (!state.userId || state.switching) return;
  try {
    const q = new URLSearchParams({ user_id: state.userId, session_id: state.sessionId || "" });
    const res = await fetch(`/api/config?${q}`);
    if (!res.ok) return;
    const cfg = await res.json();
    if (cfg.phase !== state.phase) {
      await handlePhaseChange(cfg);
    } else if (cfg.expression && cfg.expression !== state.expression) {
      // 管理画面からの個別の式の上書き
      state.expression = cfg.expression;
      document.getElementById("game-expression").textContent = cfg.expression;
      addNotice(`式が「${cfg.expression}」に かわったよ。`);
    }
  } catch (e) { /* 次の周期で再試行 */ }
}

// フェーズが変わったら：入力中のものは破棄し、「いったん おしまい」を見せてから
// 新しいフェーズのセッションに入り直す（フェーズごとに別セッション＝チャットも作り直し）。
async function handlePhaseChange(cfg) {
  state.switching = true;
  stopPolling();
  document.getElementById("chat-input").value = "";
  setDialog(null);
  showPhaseBanner(cfg);
  await new Promise(r => setTimeout(r, 2600));
  try {
    const { ok, data } = await postJson("/api/session/new", { user_id: state.userId });
    if (ok) {
      enter(data);
    } else {
      addNotice("もう一度 ログインしてね");
      startPolling();
    }
  } catch (e) {
    startPolling();
  } finally {
    hidePhaseBanner();
    state.switching = false;
  }
}

function showPhaseBanner(cfg) {
  const sub = document.getElementById("phase-banner-sub");
  sub.textContent = cfg.phase === 3 ? "つぎは 新しい 式で 作るよ" : "つぎは 新しい 画面で 作るよ";
  document.getElementById("phase-banner").hidden = false;
}
function hidePhaseBanner() {
  document.getElementById("phase-banner").hidden = true;
}

// ===== チャット描画 =====
function scrollLog() {
  const log = document.getElementById("chat-log");
  log.scrollTop = log.scrollHeight;
}

// 文言の **強調** と改行を描く（HTML は入れない：テキストノードと <strong>/<br> だけ）
function renderRich(el, text) {
  el.textContent = "";
  String(text || "").split("\n").forEach((line, li) => {
    if (li > 0) el.appendChild(document.createElement("br"));
    line.split("**").forEach((part, i) => {
      if (!part) return;
      if (i % 2 === 1) {
        const b = document.createElement("strong");
        b.textContent = part;
        el.appendChild(b);
      } else {
        el.appendChild(document.createTextNode(part));
      }
    });
  });
}

function addUserBubble(text) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "bubble bubble-user";
  el.textContent = text;
  log.appendChild(el);
  scrollLog();
}

// フェーズ1・3の最小表示（AIの吹き出しではなく、小さな確認表示）
function addAckLine(text) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "ack-line";
  el.textContent = text || "おくったよ";
  log.appendChild(el);
  scrollLog();
}

// 画面上の案内（ログには残らない）
function addNotice(text) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "notice-line";
  el.textContent = text;
  log.appendChild(el);
  scrollLog();
}

// 吹き出しの見た目：新しい問題の称賛は緑の強調、done はクリア表示、強（場面固定）は文頭を強調、それ以外は通常。
function bubbleClass(responseType, isNew, strength) {
  if (responseType === "done") return "clear";
  if (responseType === "praise" && isNew) return "new-structure";
  if (responseType === "prompt" && strength === 3) return "normal strong-prompt";
  return "normal";
}

function addAiBubble(text, responseType, isNew, strength) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = `bubble bubble-ai ${bubbleClass(responseType, isNew, strength)}`;
  renderRich(el, text);
  log.appendChild(el);
  scrollLog();
  return el;
}

function addLoadingBubble() {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "loading-bubble";
  el.innerHTML = "<span></span><span></span><span></span>";
  log.appendChild(el);
  scrollLog();
  return el;
}

// サーバ側の同時実行上限で待たされているあいだだけ、点々の横に文言を出す（エラーではない）
const WAITING_MESSAGE = "じゅんばんに 見ているよ。ちょっとまってね";
function setLoaderWaiting(loader, waiting) {
  let label = loader.querySelector(".loading-text");
  if (waiting && !label) {
    label = document.createElement("em");
    label.className = "loading-text";
    label.textContent = WAITING_MESSAGE;
    loader.appendChild(label);
    scrollLog();
  } else if (!waiting && label) {
    label.remove();
  }
}

// 送信中、1秒間隔で /api/judge/status を見て「待ち」なら文言を出す。返り値は停止関数。
function watchQueue(loader) {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/judge/status?user_id=${encodeURIComponent(state.userId)}`);
      if (!res.ok) return;
      const data = await res.json();
      setLoaderWaiting(loader, data.state === "waiting");
    } catch (e) { /* 表示だけなので無視 */ }
  }, 1000);
  return () => clearInterval(timer);
}

// 保存済みの会話を再描画。フェーズ2のターンだけAIの吹き出しを出し、それ以外は「おくったよ」。
function renderConversation(conversation) {
  if (!conversation || conversation.length === 0) return false;
  conversation.forEach((turn) => {
    if (turn.message) addUserBubble(turn.message);
    if (turn.phase === 2 && state.showSupport) {
      if (turn.ai_message) addAiBubble(turn.ai_message, turn.response_type, turn.is_new, turn.prompt_strength);
    } else {
      addAckLine("おくったよ");
    }
  });
  return true;
}

// ===== 目標の固定表示・対話の状態 =====
function setTarget(declared, label) {
  const bar = document.getElementById("target-bar");
  if (declared && label) {
    document.getElementById("target-label").textContent = label;
    bar.hidden = false;
  } else {
    bar.hidden = true;
  }
}

// dialog: null / "role"（弱・ターン1）/ "declaration"（弱・ターン2）。どちらも同じ入力欄が対話モード
// （緑枠・プレースホルダなし）になる。どのターンを待っているかはサーバが直前のログ行から決める。
const CHAT_PLACEHOLDER = "問題をここに書いてね…";
function setDialog(dialog) {
  state.dialog = dialog || null;
  const input = document.getElementById("chat-input");
  const declaring = !!state.dialog;
  document.getElementById("chat-input-area").classList.toggle("declaring", declaring);
  input.placeholder = declaring ? "" : CHAT_PLACEHOLDER;
  input.focus();
}

function applySupport(data) {
  if (data.history) state.history = data.history;
  state.allReached = !!data.all_reached;
  setTarget(data.declared, data.target_label);
  updatePanels();
}

// ===== 右パネル =====
function updatePanels() {
  const show = state.showSupport;
  // 進捗（信号機）と一覧はフェーズ2のあいだ常時表示。マークは到達数だけ（どの構造かは示さない）
  document.getElementById("lights-card").hidden = !show;
  document.getElementById("problems-card").hidden = !show;
  const n = Math.min(3, state.history.length);
  for (let i = 0; i < 3; i++) {
    const el = document.getElementById(`light-${i}`);
    el.classList.toggle("on", i < n);
    el.textContent = i < n ? "●" : "○";
  }
  document.getElementById("count-number").textContent = state.problems.length;
}

function addProblem(text, structure) {
  state.problems.push({ text, structure });
  const list = document.getElementById("problem-list");
  const li = document.createElement("li");
  const num = document.createElement("span");
  num.className = "num";
  num.textContent = `${state.problems.length}.`;
  li.appendChild(num);
  li.appendChild(document.createTextNode(text));
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

// ===== Send（作問・対話） =====
async function sendMessage() {
  if (state.sending || state.switching) return;
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  state.sending = true;
  document.getElementById("btn-send").disabled = true;
  input.value = "";
  // 対話モード（役割の宣言のターン1・2）なら、作問でない文はそのターンの答えとして扱われる（サーバが判断）。
  // 対話の途中でも作問は受け付ける（対話は打ち切り。予告は立ったまま）
  const declaring = !!state.dialog;
  setDialog(null);

  addUserBubble(text);
  const loader = addLoadingBubble();
  const stopWatch = watchQueue(loader);

  try {
    const { ok, status, data } = await postJson("/api/judge", {
      session_id: state.sessionId, user_id: state.userId, message: text, declaring,
    });
    stopWatch();
    loader.remove();
    if (!ok) {
      if (status === 403 || status === 404) {
        addNotice("もう一度 ログインしてね");
      } else {
        addNotice("エラーが起きました。もう一度送ってみてね。");
      }
      return;
    }

    // サーバが判断したフェーズに従って描く（切替の瞬間に送った場合でもログと表示がずれない）
    if (!data.show_support) {
      addAckLine("おくったよ");
      if (data.phase !== state.phase) pollConfig();
      return;
    }
    // accepted：成立した作問だけ一覧に追加（再送・判定エラーは追加しない）
    if (data.accepted) addProblem(text, data.structure);
    applySupport(data);
    // 役割の答え（role）は訂正か次のターンの問いが返る。予告（declaration）は目標を固定表示するだけ
    // （unknown なら何も出さず作問に戻る。再質問しない）
    if (data.message) addAiBubble(data.message, data.response_type, data.is_new, data.prompt_strength);
    setDialog(data.dialog);
  } catch (e) {
    stopWatch();
    loader.remove();
    addNotice("エラーが起きました。もう一度送ってみてね。");
  } finally {
    state.sending = false;
    document.getElementById("btn-send").disabled = false;
    input.focus();
  }
}

document.getElementById("btn-send").addEventListener("click", sendMessage);

let isComposing = false;
let compositionJustEnded = false;
function bindEnter(el, handler) {
  el.addEventListener("compositionstart", () => { isComposing = true; });
  el.addEventListener("compositionend", () => {
    isComposing = false;
    // SafariはEnterでのIME確定時、compositionendがkeydownより先に発火するため、
    // その直後のEnter keydownは変換確定とみなして送信しない（次のイベントループで解除）
    compositionJustEnded = true;
    setTimeout(() => { compositionJustEnded = false; }, 0);
  });
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (isComposing || compositionJustEnded || e.keyCode === 229) return;
    e.preventDefault();
    handler();
  });
}
bindEnter(document.getElementById("chat-input"), sendMessage);

// ===== Boot =====
async function showLoginExpression() {
  // 式は出席番号（奇偶）とフェーズで決まるので、ログイン前は出さない
  document.getElementById("login-expression").textContent = "";
}

async function tryRestore() {
  const savedUserId = sessionStorage.getItem("userId");
  if (!savedUserId) return false;
  try {
    const { ok, data } = await postJson("/api/login", { user_id: savedUserId });
    if (!ok) return false;
    enter(data);
    return true;
  } catch (e) {
    return false;
  }
}

function isReloadNavigation() {
  const [nav] = performance.getEntriesByType("navigation");
  return !!nav && nav.type === "reload";
}

showLoginExpression();
if (isReloadNavigation()) {
  tryRestore();
} else {
  // タブを閉じた（ログアウトせず）→ 次に開いたときはログアウトと同じ扱いにする
  clearSession();
}

// ===== 以下、図の描画（ENABLE_FIGURES=false のため呼ばれない。後から戻せるように残す） =====
const FIGURE_TITLE = {
  tobun: "分ける話：1つ分をさがす",
  hougan: "分ける話：何こ分をさがす",
  bai: "くらべる話：何ばい？",
};

function figureSvg(structure) {
  if (!ENABLE_FIGURES) return "";
  // 旧実装は式固定の SVG だった。式が可変になったため、有効化する場合は
  // tapeDiagramSvg() 系（数値を受け取る）に寄せること。
  return "";
}

function addFigureCard(structure) {
  if (!ENABLE_FIGURES) return;
  const log = document.getElementById("chat-log");
  const card = document.createElement("div");
  card.className = "figure-card";
  const title = FIGURE_TITLE[structure];
  if (!title) return;
  card.innerHTML = `<div class="figure-title">${title}</div>${figureSvg(structure)}`;
  log.appendChild(card);
  scrollLog();
}

function equalSplitTapeDiagramSvg(td) {
  const whole = td.known["全体量"];
  const n = Math.max(2, td.known["いくつ分"]);
  const x0 = 24, y0 = 28, w = 272, h = 46;
  const segW = w / n;
  let lines = "", labels = "";
  for (let i = 1; i < n; i++) {
    const x = x0 + segW * i;
    lines += `<line x1="${x}" y1="${y0}" x2="${x}" y2="${y0 + h}" stroke="#0F6E56" stroke-width="2"/>`;
  }
  for (let i = 0; i < n; i++) {
    const cx = x0 + segW * (i + 0.5);
    labels += `<text x="${cx}" y="${y0 + h / 2 + 7}" text-anchor="middle" font-size="18" font-weight="800" fill="#EF9F27">？</text>`;
  }
  return `<svg viewBox="0 0 320 126" xmlns="http://www.w3.org/2000/svg" role="img">
    <text x="160" y="18" text-anchor="middle" font-size="13" font-weight="700" fill="#064E3B">全体量 ${whole}</text>
    <rect x="${x0}" y="${y0}" width="${w}" height="${h}" rx="6" fill="#ECFDF5" stroke="#0F6E56" stroke-width="2"/>
    ${lines}${labels}
    <text x="160" y="97" text-anchor="middle" font-size="12" fill="#0F6E56">${n}つに 同じ大きさで分ける</text>
    <text x="160" y="117" text-anchor="middle" font-size="15" font-weight="700" fill="#EF9F27">${td.unknown}は？</text>
  </svg>`;
}

function repeatedUnitTapeDiagramSvg(td) {
  const whole = td.known["全体量"];
  const unit = td.known["1あたり量"];
  const x0 = 24, y0 = 28, w = 272, h = 46;
  const unitW = Math.max(30, Math.min(w * 0.4, (unit / whole) * w));
  const restX = x0 + unitW, restW = w - unitW;
  return `<svg viewBox="0 0 320 126" xmlns="http://www.w3.org/2000/svg" role="img">
    <text x="160" y="18" text-anchor="middle" font-size="13" font-weight="700" fill="#064E3B">全体量 ${whole}</text>
    <rect x="${x0}" y="${y0}" width="${unitW}" height="${h}" rx="6" fill="#D1FAE5" stroke="#0F6E56" stroke-width="2"/>
    <text x="${x0 + unitW / 2}" y="${y0 + h / 2 + 6}" text-anchor="middle" font-size="15" font-weight="700" fill="#064E3B">${unit}</text>
    <rect x="${restX}" y="${y0}" width="${restW}" height="${h}" rx="6" fill="#fff" stroke="#0F6E56" stroke-width="2" stroke-dasharray="6,5"/>
    <text x="${restX + restW / 2}" y="${y0 + h / 2 + 7}" text-anchor="middle" font-size="18" font-weight="800" fill="#EF9F27">？</text>
    <text x="160" y="117" text-anchor="middle" font-size="15" font-weight="700" fill="#EF9F27">${td.unknown}は？</text>
  </svg>`;
}

function baiTapeDiagramSvg(td) {
  const base = td.known["基準量"];
  return `<svg viewBox="0 0 320 140" xmlns="http://www.w3.org/2000/svg" role="img">
    <text x="24" y="14" text-anchor="start" font-size="11" fill="#6B7280">基準量</text>
    <rect x="24" y="20" width="90" height="32" rx="5" fill="#ECFDF5" stroke="#0F6E56" stroke-width="2"/>
    <text x="69" y="41" text-anchor="middle" font-size="15" font-weight="700" fill="#064E3B">${base}</text>
    <text x="24" y="74" text-anchor="start" font-size="11" fill="#6B7280">比較量</text>
    <rect x="24" y="80" width="272" height="32" rx="5" fill="#fff" stroke="#0F6E56" stroke-width="2" stroke-dasharray="6,5"/>
    <text x="160" y="102" text-anchor="middle" font-size="20" font-weight="800" fill="#EF9F27">？</text>
  </svg>`;
}

function tapeDiagramSvg(td) {
  if (td.structure === "倍") return baiTapeDiagramSvg(td);
  if (td.unknown === "1あたり量") return equalSplitTapeDiagramSvg(td);
  return repeatedUnitTapeDiagramSvg(td);
}

function addTapeDiagramCard(td) {
  if (!ENABLE_FIGURES) return;
  if (!td || td.type !== "tape_diagram") return;
  const log = document.getElementById("chat-log");
  const card = document.createElement("div");
  card.className = "figure-card";
  card.innerHTML = `<div class="figure-title">テープ図</div>${tapeDiagramSvg(td)}`;
  log.appendChild(card);
  scrollLog();
}
