// ===== 設定フラグ =====
// テープ図・構造図は今回すべて無効化（数量関係そのものを図で渡してしまうため）。
// 描画コードは末尾に残してあるが、このフラグが false の間は呼ばれない。
const ENABLE_FIGURES = false;

const LIGHT_INDEX = { tobun: 0, hougan: 1, bai: 2 };   // 到達構造 → 灯の位置（ラベルは出さない）

const state = {
  userId: null,
  sessionId: null,
  phase: null,
  runId: null,
  expression: "",
  showSupport: false,
  history: [],
  problems: [],
  uiLevel: 0,
  allReached: false,
  sending: false,
  buttonPressed: [],      // 直近の送信までに押された2択ボタンの値（順に）
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
const ID_RE = /^[0-9a-z]{2}$/;
const inputId = document.getElementById("input-id");
const btnLogin = document.getElementById("btn-login");
const loginError = document.getElementById("login-error");

inputId.addEventListener("input", () => { loginError.textContent = ""; });
btnLogin.addEventListener("click", doLogin);
inputId.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

async function doLogin() {
  const val = inputId.value.trim().toLowerCase();
  if (!ID_RE.test(val)) {
    loginError.textContent = "半角英数字2文字で入力してね";
    return;
  }
  loginError.textContent = "";
  btnLogin.disabled = true;
  try {
    const { ok, data } = await postJson("/api/login", { user_id: val });
    if (!ok) {
      loginError.textContent = (data && data.detail) || "エラーがおきました";
      return;
    }
    enter(data);
  } catch (e) {
    loginError.textContent = "つうしんエラーがおきました";
  } finally {
    btnLogin.disabled = false;
  }
}

// ===== 入場（ログイン／フェーズ切替後の再入場） =====
function enter(payload) {
  state.userId = payload.user_id;
  state.sessionId = payload.session_id;
  state.phase = payload.phase;
  state.runId = payload.run_id;
  state.expression = payload.expression;
  state.showSupport = !!payload.show_support;
  state.history = payload.history || [];
  state.uiLevel = payload.ui_level || 0;
  state.allReached = !!payload.all_reached;
  state.problems = [];
  state.buttonPressed = [];
  state.pollSeconds = payload.poll_seconds || 5;
  saveSession();

  resetGame();
  (payload.problems || []).forEach(p => addProblem(p.text, p.structure));
  renderConversation(payload.conversation || []);
  addNotice(kickoffText(state.phase, state.expression));
  updatePanels();
  showScreen("screen-game");
  document.getElementById("chat-input").focus();
  startPolling();
}

function kickoffText(phase, expression) {
  if (phase === 2) return `ここからは、おくった お話に 返事が 来るよ。「${expression}」になる お話を 作ろう。`;
  if (phase === 3) return `新しい式だよ。「${expression}」になる お話を 作って おくってね。`;
  return `「${expression}」になる お話を 作って おくってね。`;
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
    if (cfg.phase !== state.phase || cfg.run_id !== state.runId) {
      await handlePhaseChange(cfg);
    } else if (cfg.expression !== state.expression) {
      state.expression = cfg.expression;
      document.getElementById("game-expression").textContent = cfg.expression;
    }
  } catch (e) { /* 次の周期で再試行 */ }
}

// フェーズが変わったら：入力中のものは破棄し、「いったん おしまい」を見せてから
// 新しいフェーズのセッションに入り直す（フェーズごとに別セッション＝チャットも作り直し）。
async function handlePhaseChange(cfg) {
  state.switching = true;
  stopPolling();
  document.getElementById("chat-input").value = "";
  state.buttonPressed = [];
  disableChoiceButtons();
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
  sub.textContent = cfg.phase === 3
    ? `つぎは「${cfg.expression}」で 作るよ`
    : `つぎは 新しい 画面で 作るよ`;
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

const BUBBLE_CLASS = {
  new_structure: "new-structure",
  level1: "hint", level2: "hint", level3: "hint", level4: "hint",
  hint1: "hint", hint2: "hint", hint3: "hint",
  goal: "clear", clear: "clear",
};

function addAiBubble(text, displayType) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = `bubble bubble-ai ${BUBBLE_CLASS[displayType] || "normal"}`;
  el.textContent = text;
  log.appendChild(el);
  scrollLog();
  return el;
}

// 水準2の2択ボタン。押すと入力欄にプリフィルされるだけで、送信はしない。
function addChoiceButtons(labels) {
  disableChoiceButtons();
  const log = document.getElementById("chat-log");
  const wrap = document.createElement("div");
  wrap.className = "choice-buttons";
  labels.forEach(label => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn-choice";
    b.textContent = label;
    b.addEventListener("click", () => {
      const input = document.getElementById("chat-input");
      input.value = label;
      state.buttonPressed.push(label);
      wrap.querySelectorAll(".btn-choice").forEach(x => x.classList.toggle("selected", x === b));
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
    wrap.appendChild(b);
  });
  log.appendChild(wrap);
  scrollLog();
}
function disableChoiceButtons() {
  document.querySelectorAll(".choice-buttons .btn-choice").forEach(b => { b.disabled = true; });
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

// 保存済みの会話を再描画。フェーズ2のターンだけAIの吹き出しを出し、それ以外は「おくったよ」。
function renderConversation(conversation) {
  if (!conversation || conversation.length === 0) return false;
  const lastIdx = conversation.length - 1;
  conversation.forEach((turn, i) => {
    if (turn.message) addUserBubble(turn.message);
    if (turn.phase === 2 && state.showSupport) {
      if (turn.ai_message) addAiBubble(turn.ai_message, turn.display_type || "normal");
      if (ENABLE_FIGURES && turn.figure) addFigureCard(turn.figure);
      if (ENABLE_FIGURES && turn.tape_diagram) addTapeDiagramCard(turn.tape_diagram);
      if (i === lastIdx && Array.isArray(turn.buttons) && turn.buttons.length) addChoiceButtons(turn.buttons);
    } else {
      addAckLine("おくったよ");
    }
  });
  return true;
}

// ===== 右パネル =====
function updatePanels() {
  const show = state.showSupport;
  // 信号機は水準3（求める量の明示）到達後、または3つそろった後だけ見せる。
  // 水準3の声かけが「聞けることは3つある」と伝える回で、空いた枠を見せる意味が生まれる。
  document.getElementById("lights-card").hidden = !(show && (state.uiLevel >= 3 || state.allReached));
  // 作った問題リストはフェーズ2のあいだ常に見せる（自分の産出を読み返せる状態を保つ）。
  // 水準1の声かけは「右を読みかえしてみよう」と視線を送る役割になる。
  document.getElementById("problems-card").hidden = !show;

  for (let i = 0; i < 3; i++) document.getElementById(`light-${i}`).classList.remove("on");
  state.history.forEach(s => {
    const idx = LIGHT_INDEX[s];
    if (idx !== undefined) document.getElementById(`light-${idx}`).classList.add("on");
  });
  const remaining = 3 - state.history.filter(s => LIGHT_INDEX[s] !== undefined).length;
  document.getElementById("lights-label").textContent =
    remaining > 0 ? `あと ${remaining} つ` : "3つとも できた！";
  document.getElementById("count-number").textContent = state.problems.length;
}

// 水準1・2は産出一覧を右パネルで読ませる。チャットに列挙せず、カードを光らせて視線を送る。
function flashProblems() {
  const card = document.getElementById("problems-card");
  if (!card || card.hidden) return;
  card.classList.remove("flash");
  void card.offsetWidth;          // アニメーションを再生し直すためのリフロー
  card.classList.add("flash");
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

// ===== Send =====
async function sendMessage() {
  if (state.sending || state.switching) return;
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  state.sending = true;
  document.getElementById("btn-send").disabled = true;
  input.value = "";
  const pressed = state.buttonPressed.length ? state.buttonPressed.join(",") : null;
  state.buttonPressed = [];
  disableChoiceButtons();

  addUserBubble(text);
  const loader = addLoadingBubble();

  try {
    const { ok, status, data } = await postJson("/api/judge", {
      session_id: state.sessionId, user_id: state.userId, message: text, button_pressed: pressed,
    });
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
    if (data.history) state.history = data.history;
    if (typeof data.ui_level === "number") state.uiLevel = data.ui_level;
    state.allReached = !!data.all_reached;
    if (data.valid) addProblem(text, data.structure);
    updatePanels();

    addAiBubble(data.message, data.display_type);
    if (data.highlight_problems) flashProblems();
    if (Array.isArray(data.buttons) && data.buttons.length) addChoiceButtons(data.buttons);
    if (ENABLE_FIGURES && data.figure) addFigureCard(data.figure);
    if (ENABLE_FIGURES && data.tape_diagram) addTapeDiagramCard(data.tape_diagram);
  } catch (e) {
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
const chatInput = document.getElementById("chat-input");
chatInput.addEventListener("compositionstart", () => { isComposing = true; });
chatInput.addEventListener("compositionend", () => {
  isComposing = false;
  // SafariはEnterでのIME確定時、compositionendがkeydownより先に発火するため、
  // その直後のEnter keydownは変換確定とみなして送信しない（次のイベントループで解除）
  compositionJustEnded = true;
  setTimeout(() => { compositionJustEnded = false; }, 0);
});
chatInput.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey) return;
  if (isComposing || compositionJustEnded || e.keyCode === 229) return;
  e.preventDefault();
  sendMessage();
});

// ===== Boot =====
async function showLoginExpression() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) {
      const cfg = await res.json();
      document.getElementById("login-expression").textContent = cfg.expression;
    }
  } catch (e) { /* noop */ }
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
  // 旧実装は 18÷3 固定の SVG だった。式が可変になったため、有効化する場合は
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
