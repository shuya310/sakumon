let currentUserId = null;
let liveTimer = null;

// ===== API helper（Basic認証はブラウザが /admin で入力した資格情報を同一オリジンの fetch に付ける） =====
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) {
    location.reload();  // 資格情報が切れた → ブラウザの認証ダイアログを出し直す
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (e) { detail = await res.text(); }
    throw new Error(detail || res.statusText);
  }
  if (options.method === "DELETE") return {};
  return res.json();
}

// ===== タブ / ビュー =====
function showView(id) {
  ["view-phase", "view-students", "view-student-detail"].forEach(v => {
    document.getElementById(v).style.display = "none";
  });
  document.getElementById(id).style.display = "block";
  document.getElementById("tab-phase").classList.toggle("active", id === "view-phase");
  document.getElementById("tab-students").classList.toggle("active", id !== "view-phase");
  if (id === "view-phase") startLive(); else stopLive();
}

document.getElementById("tab-phase").addEventListener("click", () => showView("view-phase"));
document.getElementById("tab-students").addEventListener("click", loadStudents);

// ===== フェーズ管理 =====
const PHASE_DESC = {
  1: "事前測定：式Aで自由に作問。判定は裏で動くが児童には何も表示しない。",
  2: "支援：同じ式Aで、フィードバック（水準1〜4）を受けながら作問。",
  3: "事後測定：式Bで自由に作問。新しいセッション。支援なし。",
};

function renderConfig(cfg) {
  document.getElementById("phase-big").textContent = `フェーズ ${cfg.current_phase}`;
  document.getElementById("phase-desc").textContent = PHASE_DESC[cfg.current_phase] || "";
  document.getElementById("run-id").textContent = cfg.run_id;
  document.getElementById("cfg-updated").textContent = fmtDate(cfg.updated_at);
  document.querySelectorAll(".btn-phase").forEach(b => {
    b.classList.toggle("active", Number(b.dataset.phase) === cfg.current_phase);
  });
  const a = document.getElementById("expr-a"), b = document.getElementById("expr-b");
  if (document.activeElement !== a) a.value = cfg.expression_a;
  if (document.activeElement !== b) b.value = cfg.expression_b;
}

async function loadConfig() {
  try {
    const cfg = await api("/admin/api/config");
    renderConfig(cfg);
  } catch (e) { /* live で再取得される */ }
}

document.querySelectorAll(".btn-phase").forEach(btn => {
  btn.addEventListener("click", async () => {
    const phase = Number(btn.dataset.phase);
    const msg = {
      1: "フェーズ1（事前・支援なし）に切り替えますか？",
      2: "フェーズ2（支援あり）に切り替えますか？\n児童の画面に「ここまでで いったん おしまい」が出て、同じセッションで支援が始まります。",
      3: "フェーズ3（事後・式B）に切り替えますか？\n全員に新しいセッションが作られ、信号機と停滞カウントがリセットされます。",
    }[phase];
    if (!confirm(msg)) return;
    try {
      const cfg = await api("/admin/api/phase", { method: "POST", body: JSON.stringify({ phase }) });
      renderConfig(cfg);
      refreshLive();
    } catch (e) {
      alert("切り替えに失敗しました: " + e.message);
    }
  });
});

document.getElementById("btn-save-expr").addEventListener("click", async () => {
  const msg = document.getElementById("expr-msg");
  msg.textContent = "";
  try {
    const cfg = await api("/admin/api/expressions", {
      method: "POST",
      body: JSON.stringify({
        expression_a: document.getElementById("expr-a").value,
        expression_b: document.getElementById("expr-b").value,
      }),
    });
    renderConfig(cfg);
    msg.textContent = "保存しました";
    msg.className = "expr-msg ok";
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "expr-msg err";
  }
});

document.getElementById("btn-new-run").addEventListener("click", async () => {
  if (!confirm("新しい回を始めますか？\nrun 番号が進み、フェーズ1に戻ります。児童は次のログインから新しいセッションになります。\n（過去のデータは消えません）")) return;
  try {
    const cfg = await api("/admin/api/new_run", { method: "POST" });
    renderConfig(cfg);
    refreshLive();
  } catch (e) {
    alert("失敗しました: " + e.message);
  }
});

// ===== 児童の状態（ライブ） =====
const STATE_CLS = { S0: "badge-red", S1: "badge-orange", S2: "badge-blue", S3: "badge-green" };
const LEVEL_LABEL = {
  none: "—", form: "0 成立性", level1: "1 同じ？", level2: "2 求める量", level3: "3 場面",
  discover: "新構造", goal: "3つ達成", talk: "対話", error: "エラー",
};

function lightsHtml(structures) {
  const set = new Set(structures || []);
  return `<span class="lamps">
    <span class="lamp ${set.has("tobun") ? "on" : ""}" title="1つ分">●</span>
    <span class="lamp ${set.has("hougan") ? "on" : ""}" title="いくつ分">●</span>
    <span class="lamp ${set.has("bai") ? "on" : ""}" title="何倍">●</span>
  </span>`;
}

function renderLive(data) {
  const tbody = document.getElementById("live-tbody");
  const students = data.students || [];
  const online = students.filter(s => s.online).length;
  const zero = students.filter(s => s.submitted === 0).length;
  document.getElementById("live-summary").textContent =
    `（${students.length}人 ／ 接続中 ${online}人 ／ 提出0問 ${zero}人）`;
  if (students.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">この回・このフェーズでログインした児童はまだいません</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  students.forEach(s => {
    const tr = document.createElement("tr");
    if (s.submitted === 0) tr.className = "row-zero";
    const level = data.config.phase === 2 ? (s.current_level ? `水準 ${s.current_level}` : "—") : "—";
    const lastLevel = s.last_support_level ? `<div class="muted small">直近: ${LEVEL_LABEL[s.last_support_level] || s.last_support_level}</div>` : "";
    tr.innerHTML = `
      <td><strong>${esc(s.user_id)}</strong></td>
      <td>${s.online ? '<span class="dot-on"></span> 接続中' : `<span class="dot-off"></span> <span class="muted small">${s.last_seen_seconds == null ? "未接続" : Math.round(s.last_seen_seconds / 60) + "分前"}</span>`}</td>
      <td class="${s.submitted === 0 ? "zero" : ""}"><strong>${s.submitted}</strong></td>
      <td>${s.valid}</td>
      <td>${lightsHtml(s.structures)}</td>
      <td>${level}${lastLevel}</td>
      <td>${s.learner_state ? `<span class="badge ${STATE_CLS[s.learner_state] || "badge-gray"}">${s.learner_state}</span>` : '<span class="muted">—</span>'}</td>
      <td class="muted small">${s.last_activity ? fmtTime(s.last_activity) : "—"}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function refreshLive() {
  try {
    const data = await api("/admin/api/live");
    renderLive(data);
    // フェーズ表示も同期（別タブ・別端末からの変更に追随）
    const cfg = await api("/admin/api/config");
    renderConfig(cfg);
  } catch (e) { /* 次回 */ }
}

function startLive() {
  stopLive();
  refreshLive();
  liveTimer = setInterval(refreshLive, 5000);
}
function stopLive() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = null;
}

// ===== 児童一覧 =====
async function loadStudents() {
  showView("view-students");
  setBreadcrumb("breadcrumb", [{ label: "児童一覧" }]);
  const tbody = document.getElementById("students-tbody");
  tbody.innerHTML = `<tr><td colspan="4" class="empty">読み込み中…</td></tr>`;
  try {
    const students = await api("/admin/api/students");
    if (students.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">データがありません</td></tr>`;
      return;
    }
    tbody.innerHTML = "";
    students.forEach(s => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `
        <td><strong>${esc(s.user_id)}</strong></td>
        <td>${fmtDate(s.last_login)}</td>
        <td>${s.session_count}</td>
        <td>${structureBadges(s.structure_count)}</td>
      `;
      tr.addEventListener("click", () => loadStudentDetail(s.user_id));
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">読み込みエラー</td></tr>`;
  }
}

function structureBadges(count) {
  const cls = count === 3 ? "badge-green" : count > 0 ? "badge-orange" : "badge-gray";
  return `<span class="badge ${cls}">${count} / 3</span>`;
}

// ===== 児童詳細 =====
async function loadStudentDetail(userId) {
  currentUserId = userId;
  showView("view-student-detail");
  setBreadcrumb("breadcrumb-detail", [
    { label: "児童一覧", action: loadStudents },
    { label: `${userId} さん` },
  ]);
  document.getElementById("detail-title").textContent = `${userId} さんのセッション一覧`;
  const container = document.getElementById("sessions-container");
  container.innerHTML = `<div class="spinner">読み込み中…</div>`;
  try {
    const data = await api(`/admin/api/students/${encodeURIComponent(userId)}`);
    container.innerHTML = "";
    if (data.sessions.length === 0) {
      container.innerHTML = `<div class="empty">セッションがありません</div>`;
      return;
    }
    data.sessions.forEach(s => container.appendChild(buildSessionBlock(s, userId)));
  } catch (e) {
    container.innerHTML = `<div class="empty">読み込みエラー</div>`;
  }
}

function buildSessionBlock(session, userId) {
  const block = document.createElement("div");
  block.className = "session-block";
  block.dataset.sessionId = session.session_id;

  const structs = (session.structures || []).map(x => STRUCT_LABEL[x] || x).join("・") || "なし";
  const head = document.createElement("div");
  head.className = "session-head";
  head.innerHTML = `
    <div class="session-head-left">
      <div>
        <div class="s-date">#${session.session_id}　${fmtDate(session.created_at)}　
          <span class="badge badge-gray">run ${session.run_id}</span>
          <span class="badge badge-blue">フェーズ${session.phase}${session.phase === 1 ? "→2" : ""} 開始</span>
          <span class="badge badge-gray">${esc(session.expression)}</span>
        </div>
        <div class="s-stat">違う構造 ${session.new_count} ／ 作問 ${session.sakumon_count ?? 0}回・対話 ${session.taiwa_count ?? 0}回　${structs}</div>
      </div>
    </div>
    <div class="session-head-right">
      <button class="btn btn-danger btn-sm btn-del-session" data-id="${session.session_id}">削除</button>
      <span class="toggle-arrow">▼</span>
    </div>
  `;

  const body = document.createElement("div");
  body.className = "session-body";
  body.dataset.loaded = "false";

  head.querySelector(".btn-del-session").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm(`セッション #${session.session_id} を削除しますか？（元に戻せません）`)) return;
    await api(`/admin/api/sessions/${session.session_id}`, { method: "DELETE" });
    block.remove();
  });

  head.addEventListener("click", async () => {
    const isOpen = body.classList.toggle("open");
    head.querySelector(".toggle-arrow").textContent = isOpen ? "▲" : "▼";
    if (isOpen && body.dataset.loaded === "false") {
      body.dataset.loaded = "true";
      body.innerHTML = `<div class="spinner">チャット履歴を読み込み中…</div>`;
      try {
        const logs = await api(`/admin/api/sessions/${session.session_id}`);
        body.innerHTML = "";
        if (logs.length === 0) {
          body.innerHTML = `<div class="empty">チャット履歴がありません</div>`;
          return;
        }
        body.appendChild(buildLogsTable(logs));
      } catch (e) {
        body.innerHTML = `<div class="empty">読み込みエラー</div>`;
      }
    }
  });

  block.appendChild(head);
  block.appendChild(body);
  return block;
}

function buildLogsTable(logs) {
  const tbl = document.createElement("table");
  tbl.className = "logs-table";
  tbl.innerHTML = `
    <thead>
      <tr>
        <th style="width:96px">日時</th>
        <th style="width:44px">Ph</th>
        <th style="width:56px">種別</th>
        <th>入力 / AIの返答</th>
        <th style="width:110px">判定</th>
        <th style="width:90px">水準</th>
        <th style="width:52px">状態</th>
        <th style="width:52px"></th>
      </tr>
    </thead>
  `;
  const tbody = document.createElement("tbody");
  logs.forEach(log => tbody.appendChild(buildLogRow(log)));
  tbl.appendChild(tbody);
  return tbl;
}

const UNKNOWN_LABEL = { one_unit: "1つ分", num_units: "いくつ分", ratio: "倍率", base: "基準量", rate: "割合" };
const ISSUE_LABEL = {
  scene_contradiction: "場面矛盾", wrong_number: "式ちがい", wrong_operation: "演算ちがい",
  incomplete_text: "途中で切れ", no_question: "問いなし", not_problem: "文章題でない", error: "判定エラー",
  reversed: "向き逆(旧)",
};

function buildLogRow(log) {
  const tr = document.createElement("tr");
  tr.className = "log-row";
  tr.dataset.logId = log.id;

  const aiCls = {
    new_structure: "new-structure",
    level1: "hint", level2: "hint", level3: "hint",
    hint1: "hint", hint2: "hint", hint3: "hint",
    goal: "clear", clear: "clear",
  }[log.display_type] || "";

  const isTaiwa = log.input_type === "taiwa";
  const inputBadge = isTaiwa
    ? '<span class="badge badge-purple">対話</span>'
    : '<span class="badge badge-blue">作問</span>';

  // 判定：成立なら 構造＋求める量、不成立なら issue
  let judgeCell = '<span style="color:#ccc">—</span>';
  if (log.structure) {
    judgeCell = `<span class="badge badge-blue">${STRUCT_LABEL[log.structure] || log.structure}</span>`
      + (log.unknown ? `<div class="muted small">${UNKNOWN_LABEL[log.unknown] || log.unknown}</div>` : "")
      + (log.is_new ? '<div><span class="badge badge-green">新規</span></div>' : "");
  } else if (log.issue) {
    judgeCell = `<span class="badge badge-orange">${ISSUE_LABEL[log.issue] || log.issue}</span>`;
  } else if (!isTaiwa && log.valid === false) {
    judgeCell = `<span class="badge badge-orange">不成立</span>`;
  }

  // 水準（新）／ 旧データは stumble を表示
  const lvl = log.support_level
    ? `<span class="badge ${LEVEL_CLS[log.support_level] || "badge-gray"}">${LEVEL_LABEL[log.support_level] || log.support_level}</span>`
    : (log.stumble ? `<span class="badge badge-gray" title="旧つまづき">${STUMBLE_LABEL[log.stumble] || log.stumble}</span>` : '<span style="color:#ccc">—</span>');
  const button = log.button_pressed ? `<div class="muted small">ボタン: ${esc(log.button_pressed)}</div>` : "";
  const target = log.target_structure ? `<div class="muted small">対象: ${STRUCT_LABEL[log.target_structure] || log.target_structure}</div>` : "";
  const stall = (log.stall_count != null && log.stall_count > 0) ? `<div class="muted small">反復 ${log.stall_count}</div>` : "";

  tr.innerHTML = `
    <td style="font-size:.78rem;color:#888;white-space:nowrap">${fmtDate(log.created_at)}</td>
    <td>${log.phase != null ? `<span class="badge badge-gray">${log.phase}</span>` : '<span style="color:#ccc">—</span>'}</td>
    <td>${inputBadge}</td>
    <td>
      <div class="msg-user">${esc(log.message)}</div>
      <div class="msg-ai ${aiCls}">${esc(log.ai_message)}</div>
      ${button}
    </td>
    <td>${judgeCell}</td>
    <td>${lvl}${target}${stall}</td>
    <td>${log.learner_state ? `<span class="badge ${STATE_CLS[log.learner_state] || "badge-gray"}">${log.learner_state}</span>` : '<span style="color:#ccc">—</span>'}</td>
    <td><button class="btn btn-danger btn-sm">削除</button></td>
  `;

  tr.querySelector(".btn-danger").addEventListener("click", async () => {
    if (!confirm("このチャットを削除しますか？（元に戻せません）")) return;
    await api(`/admin/api/logs/${log.id}`, { method: "DELETE" });
    tr.remove();
  });
  return tr;
}

// ===== CSV エクスポート =====
document.getElementById("btn-export-csv").addEventListener("click", async () => {
  const res = await fetch("/admin/api/export/csv", { credentials: "same-origin" });
  if (!res.ok) { alert("エクスポートに失敗しました"); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "sakumon_export.csv";
  a.click();
  URL.revokeObjectURL(url);
});

// ===== パンくず =====
function setBreadcrumb(elId, items) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  items.forEach((item, i) => {
    if (i < items.length - 1 && item.action) {
      const a = document.createElement("a");
      a.textContent = item.label;
      a.style.cursor = "pointer";
      a.addEventListener("click", item.action);
      el.appendChild(a);
      const sep = document.createElement("span");
      sep.textContent = " › ";
      el.appendChild(sep);
    } else {
      const span = document.createElement("span");
      span.textContent = item.label;
      el.appendChild(span);
    }
  });
}

// ===== Helpers =====
const STRUCT_LABEL = { tobun: "等分除", hougan: "包含除", bai: "倍" };
const LEVEL_CLS = {
  none: "badge-gray", form: "badge-orange", level1: "badge-orange",
  level2: "badge-red", level3: "badge-red", discover: "badge-green", goal: "badge-green",
  talk: "badge-purple", error: "badge-gray",
};
// 旧データ（9/18 以前）の stumble 表示用
const STUMBLE_LABEL = {
  incomplete: "要素不足", wrong_expression: "式ちがい", reversed: "向き逆",
  hint1: "停滞1", hint2: "停滞2", hint3: "停滞3(テープ図)",
  material_confusion: "題材混同", help_request: "助け求め", repeat_structure: "反復(旧)",
};

function toDate(str) {
  if (!str) return null;
  const s = str.includes("T") ? str : str.replace(" ", "T");
  return new Date(s.endsWith("Z") ? s : s + "Z");
}
function fmtDate(str) {
  const d = toDate(str);
  if (!d || isNaN(d)) return "—";
  return d.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fmtTime(str) {
  const d = toDate(str);
  if (!d || isNaN(d)) return "—";
  return d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ===== Boot =====
showView("view-phase");
loadConfig();
