const state = {
  userId: null,
  sessionId: null,
  sessions: [],
  selectedSessionId: null,
  history: [],
  problems: [],
  sending: false,
};

const STRUCTURE_LABEL = {
  tobun: "等分除",
  hougan: "包含除",
  bai: "倍",
};

// ===== Screen =====
function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

// ===== sessionStorage =====
function saveSession() {
  sessionStorage.setItem("userId", state.userId || "");
  sessionStorage.setItem("sessionId", state.sessionId != null ? String(state.sessionId) : "");
}

function clearSession() {
  sessionStorage.removeItem("userId");
  sessionStorage.removeItem("sessionId");
}

// ===== Validation =====
const ID_RE = /^[0-9a-z]{2}$/;

// ===== Login screen =====
const inputId = document.getElementById("input-id");
const btnLogin = document.getElementById("btn-login");
const loginError = document.getElementById("login-error");

inputId.addEventListener("input", () => {
  loginError.textContent = "";
});

btnLogin.addEventListener("click", doLogin);
inputId.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});

async function doLogin() {
  const val = inputId.value.trim().toLowerCase();
  if (!ID_RE.test(val)) {
    loginError.textContent = "半角英数字2文字で入力してね";
    return;
  }
  loginError.textContent = "";
  btnLogin.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: val }),
    });
    if (!res.ok) {
      const err = await res.json();
      loginError.textContent = err.detail || "エラーがおきました";
      return;
    }
    const data = await res.json();
    state.userId = data.user_id;
    state.sessions = data.sessions || [];

    if (state.sessions.length === 0) {
      await startNewSession();
    } else {
      document.getElementById("choose-label").textContent =
        `${val}さんの記録があるよ！`;
      showScreen("screen-choose");
    }
  } catch (e) {
    loginError.textContent = "つうしんエラーがおきました";
  } finally {
    btnLogin.disabled = false;
  }
}

// ===== Choose screen =====
document.getElementById("btn-new").addEventListener("click", async () => {
  await startNewSession();
});

document.getElementById("btn-resume-list").addEventListener("click", () => {
  renderSessionList();
  showScreen("screen-sessions");
});

// ===== Session list screen =====
function renderSessionList() {
  state.selectedSessionId = null;
  document.getElementById("btn-sessions-ok").disabled = true;
  const list = document.getElementById("session-list");
  list.innerHTML = "";
  state.sessions.forEach(s => {
    const dt = new Date(s.created_at + "Z");
    const dateStr = dt.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const structs = s.structures.map(x => STRUCTURE_LABEL[x] || x).join("・") || "なし";
    const div = document.createElement("div");
    div.className = "session-item";
    div.dataset.sessionId = s.session_id;
    div.innerHTML = `<div class="s-date">${dateStr}</div>
      <div class="s-info">問題 ${s.problem_count} 個　／　${structs}</div>`;
    div.addEventListener("click", () => {
      document.querySelectorAll(".session-item").forEach(el => el.classList.remove("selected"));
      div.classList.add("selected");
      state.selectedSessionId = s.session_id;
      document.getElementById("btn-sessions-ok").disabled = false;
    });
    list.appendChild(div);
  });
}

document.getElementById("btn-sessions-back").addEventListener("click", () => {
  showScreen("screen-choose");
});

document.getElementById("btn-sessions-ok").addEventListener("click", async () => {
  if (state.selectedSessionId == null) return;
  await resumeSession(state.selectedSessionId);
});

// ===== Start new session =====
async function startNewSession() {
  const res = await fetch("/api/session/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: state.userId }),
  });
  const data = await res.json();
  state.sessionId = data.session_id;
  saveSession();
  resetGame();
  showScreen("screen-game");
  addAiBubble("こんにちは！「18 ÷ 3」になるお話を書いてね。どんなお話かな？", "normal");
}

// ===== Resume session =====
async function resumeSession(sessionId) {
  const res = await fetch("/api/session/resume", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  const data = await res.json();
  state.sessionId = data.session_id;
  saveSession();
  resetGame();

  state.history = data.history || [];
  data.problems.forEach(p => addProblem(p.text, p.structure));
  updateMeter();

  showScreen("screen-game");
  addAiBubble(
    `おかえり！続きから始めよう。これまで${data.problems.length}個の問題を作ったね！`,
    "normal"
  );
}

// ===== Game screen =====
function resetGame() {
  state.history = [];
  state.problems = [];
  document.getElementById("chat-log").innerHTML = "";
  document.getElementById("problem-list").innerHTML = "";
  document.getElementById("game-user-name").textContent =
    state.userId ? `${state.userId} さん` : "";
  updateMeter();
}

document.getElementById("btn-logout").addEventListener("click", () => {
  clearSession();
  state.userId = null;
  state.sessionId = null;
  state.sessions = [];
  inputId.value = "";
  loginError.textContent = "";
  showScreen("screen-login");
});

function addUserBubble(text) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "bubble bubble-user";
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function addAiBubble(text, displayType) {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  const cls = {
    new_structure: "new-structure",
    hint1: "hint", hint2: "hint", hint3: "hint",
    clear: "clear",
  }[displayType] || "normal";
  el.className = `bubble bubble-ai ${cls}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function addLoadingBubble() {
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "loading-bubble";
  el.innerHTML = "<span></span><span></span><span></span>";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function updateMeter() {
  const count = state.history.length;
  for (let i = 0; i < 3; i++) {
    document.getElementById(`light-${i}`).classList.toggle("on", i < count);
  }
  const remaining = 3 - count;
  document.getElementById("lights-label").textContent =
    remaining > 0 ? `あと ${remaining} つ` : "全部できた！";

  document.getElementById("count-number").textContent = state.problems.length;
  const dots = document.getElementById("count-dots");
  dots.innerHTML = "";
  state.problems.forEach(() => {
    const d = document.createElement("div");
    d.className = "dot";
    dots.appendChild(d);
  });
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
  if (state.sending) return;
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;

  state.sending = true;
  document.getElementById("btn-send").disabled = true;
  input.value = "";

  addUserBubble(text);
  const loader = addLoadingBubble();

  try {
    const res = await fetch("/api/judge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
    });
    const data = await res.json();
    loader.remove();

    if (data.history) state.history = data.history;
    if (data.valid) addProblem(text, data.structure);
    updateMeter();

    if (data.display_type === "clear") {
      addAiBubble(data.message, "clear");
      setTimeout(() => showClear(), 1800);
    } else {
      addAiBubble(data.message, data.display_type);
    }
  } catch (e) {
    loader.remove();
    addAiBubble("エラーが起きました。もう一度送ってみてね。", "normal");
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

// ===== Clear screen =====
function showClear() {
  clearSession();
  const container = document.getElementById("clear-problems");
  container.innerHTML = "";
  const shown = new Set();
  state.problems.forEach(p => {
    if (!shown.has(p.structure)) {
      shown.add(p.structure);
      const div = document.createElement("div");
      div.className = "clear-problem-item";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = STRUCTURE_LABEL[p.structure] || p.structure;
      div.appendChild(tag);
      div.appendChild(document.createTextNode(p.text));
      container.appendChild(div);
    }
  });
  showScreen("screen-clear");
}

document.getElementById("btn-retry").addEventListener("click", () => {
  inputId.value = "";
  loginError.textContent = "";
  state.userId = null;
  state.sessionId = null;
  state.sessions = [];
  showScreen("screen-login");
});

// ===== リロード復元 =====
async function tryRestoreSession() {
  const savedUserId = sessionStorage.getItem("userId");
  const savedSessionId = sessionStorage.getItem("sessionId");
  if (!savedUserId || !savedSessionId) return false;

  try {
    const res = await fetch("/api/session/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: parseInt(savedSessionId, 10) }),
    });
    if (!res.ok) return false;
    const data = await res.json();

    state.userId = savedUserId;
    state.sessionId = data.session_id;
    resetGame();
    state.history = data.history || [];
    data.problems.forEach(p => addProblem(p.text, p.structure));
    updateMeter();
    showScreen("screen-game");
    addAiBubble("続きだよ！問題を書いてね。", "normal");
    return true;
  } catch (e) {
    return false;
  }
}

// ===== Boot =====
function isReloadNavigation() {
  const [nav] = performance.getEntriesByType("navigation");
  return !!nav && nav.type === "reload";
}

if (isReloadNavigation()) {
  tryRestoreSession();
} else {
  // タブを閉じた（ログアウトせず）→ 次に開いたときはログアウトと同じ扱いにする
  clearSession();
}
