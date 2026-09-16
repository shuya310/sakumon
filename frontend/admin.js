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
  1: "事前測定：自由に作問。判定は裏で動くが児童には何も表示しない。",
  2: "支援：予告支援（なし／弱／中／強）を受けながら作問（新しいセッション）。",
  3: "事後測定：別の式で自由に作問。新しいセッション。支援なし。",
};

function renderConfig(cfg) {
  document.getElementById("phase-big").textContent = `フェーズ ${cfg.current_phase}`;
  document.getElementById("phase-desc").textContent = PHASE_DESC[cfg.current_phase] || "";
  document.getElementById("cfg-updated").textContent = fmtDate(cfg.updated_at);
  document.querySelectorAll(".btn-phase").forEach(b => {
    b.classList.toggle("active", Number(b.dataset.phase) === cfg.current_phase);
  });
  const asg = (cfg.public && cfg.public.expression_assignment) || {};
  const tbody = document.getElementById("expr-tbody");
  tbody.innerHTML = "";
  [["odd", "奇数番"], ["even", "偶数番"]].forEach(([g, label]) => {
    const row = asg[g] || {};
    tbody.insertAdjacentHTML("beforeend",
      `<tr><td>${label}</td><td>${esc(row["1"] || "—")}</td><td>${esc(row["2"] || "—")}</td><td>${esc(row["3"] || "—")}</td></tr>`);
  });
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
      2: "フェーズ2（支援あり）に切り替えますか？\n児童の画面に「ここまでで いったん おしまい」が出て、新しいセッションで支援が始まります。",
      3: "フェーズ3（事後・式B）に切り替えますか？\n全員に新しいセッションが作られ、到達構造はゼロから数え直します。",
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

// ===== 児童の状態（ライブ） =====
const RESPONSE_LABEL = {
  form: "形式", praise: "称賛", prompt: "予告支援", talk: "対話", done: "3つ達成", error: "判定エラー",
};
const STRENGTH_LABEL = { 0: "なし", 1: "弱", 2: "中", 3: "強" };

function responseBadge(type, strength) {
  if (!type) return '<span style="color:#ccc">—</span>';
  let html = `<span class="badge ${RESPONSE_CLS[type] || "badge-gray"}">${RESPONSE_LABEL[type] || type}</span>`;
  if (type === "prompt" && strength != null) html += ` <span class="muted small">${STRENGTH_LABEL[strength] ?? strength}</span>`;
  return html;
}

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
    tbody.innerHTML = `<tr><td colspan="9" class="empty">このフェーズでログインした児童はまだいません</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  students.forEach(s => {
    const tr = document.createElement("tr");
    if (s.submitted === 0) tr.className = "row-zero";
    const counts = `反復 ${s.stuck_count ?? 0} ／ 不一致 ${s.miss_count ?? 0} ／ 支援要求 ${s.help_count ?? 0}<br>強度 ${s.strength ?? 0}`;
    const declared = s.declared
      ? `<span class="badge ${s.declared_by === "child" ? "badge-blue" : "badge-orange"}">${s.declared_by === "child" ? "児童" : "システム"}</span> ${STRUCT_LABEL[s.declared] || s.declared}`
      : '<span class="muted">—</span>';
    tr.innerHTML = `
      <td><strong>${esc(s.user_id)}</strong></td>
      <td>${s.online ? '<span class="dot-on"></span> 接続中' : `<span class="dot-off"></span> <span class="muted small">${s.last_seen_seconds == null ? "未接続" : Math.round(s.last_seen_seconds / 60) + "分前"}</span>`}</td>
      <td class="${s.submitted === 0 ? "zero" : ""}"><strong>${s.submitted}</strong></td>
      <td>${s.valid}</td>
      <td>${lightsHtml(s.structures)}</td>
      <td class="small">${data.config.phase === 2 ? declared : '<span class="muted">—</span>'}</td>
      <td class="${(s.miss_count ?? 0) > 0 ? "met-ng" : "muted"} small">${counts}</td>
      <td>${data.config.phase === 2 ? responseBadge(s.last_response_type, s.last_prompt_strength) : '<span class="muted">—</span>'}</td>
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
        <div class="s-date">#${session.session_id}　${fmtDate(session.session_start)} 〜 ${session.session_end ? fmtDate(session.session_end) : "（継続中）"}　
          <span class="badge badge-blue">フェーズ${session.phase}</span>
          <span class="badge badge-gray" id="expr-badge-${session.session_id}">${esc(session.expression)}</span>
          <button class="btn btn-ghost btn-sm btn-set-expr" data-id="${session.session_id}">式を変更</button>
          <span class="badge badge-gray">${session.parity_group === "odd" ? "奇数" : "偶数"}</span>
          ${session.declared ? `<span class="badge badge-orange">予告: ${STRUCT_LABEL[session.declared] || session.declared}（${session.declared_by === "child" ? "児童" : "システム"}）</span>` : ""}
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

  head.querySelector(".btn-set-expr").addEventListener("click", async (e) => {
    e.stopPropagation();
    const v = prompt(`セッション #${session.session_id} の式を上書きします（例: 24 ÷ 8）`, session.expression);
    if (!v) return;
    try {
      const r = await api(`/admin/api/sessions/${session.session_id}/expression`, { method: "POST", body: JSON.stringify({ expression: v }) });
      session.expression = r.expression;
      head.querySelector(`#expr-badge-${session.session_id}`).textContent = r.expression;
    } catch (err) {
      alert("式の変更に失敗しました: " + err.message);
    }
  });

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
        <th style="width:90px">応答</th>
        <th style="width:96px">到達 / 回数</th>
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
const ROLE_LABEL = { people: "人数", per_one: "1人分の数", base: "比べる相手の量", dont_know: "わからない", unknown: "判別不能" };
const ISSUE_LABEL = {
  scene_contradiction: "場面矛盾", wrong_number: "式ちがい", reversed: "向きが逆", wrong_operation: "演算ちがい",
  incomplete_text: "途中で切れ", no_question: "問いなし", not_problem: "文章題でない", error: "判定エラー（API不通）",
};

function buildLogRow(log) {
  const tr = document.createElement("tr");
  tr.className = "log-row";
  tr.dataset.logId = log.id;

  const aiCls = log.response_type === "done" ? "clear"
    : (log.response_type === "praise" && log.is_new) ? "new-structure" : "";

  const isTaiwa = log.input_type === "taiwa";
  const inputBadge = {
    taiwa: '<span class="badge badge-purple">対話</span>',
    resend: '<span class="badge badge-gray" title="同じ本文の再送（APIは呼ばず直前の結果を返した）">再送</span>',
    role: '<span class="badge badge-orange">役割</span>',
    declaration: '<span class="badge badge-orange">予告</span>',
    self_label: '<span class="badge badge-orange">自己ラベル</span>',
  }[log.input_type] || '<span class="badge badge-blue">作問</span>';

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

  // 応答の種類と、予告・自己ラベル（予告支援仕様で記録されるようになる列）
  const declared = log.declared_structure
    ? `<div class="small">予告: ${STRUCT_LABEL[log.declared_structure] || log.declared_structure}（${log.declared_by === "child" ? "児童" : "システム"}）${log.declaration_met == null ? "" : (log.declaration_met ? ' <span class="met-ok">一致 ✓</span>' : ' <span class="met-ng">不一致 ✗</span>')}</div>` : "";
  const selfLabel = (log.self_label || log.self_label_text)
    ? `<div class="small">自己ラベル: ${log.self_label ? (STRUCT_LABEL[log.self_label] || log.self_label) : esc(log.self_label_text)}${log.self_label_match == null ? "" : (log.self_label_match ? ' <span class="met-ok">判定と一致</span>' : ' <span class="met-ng">判定と不一致</span>')}</div>` : "";
  const helpReq = log.is_help_request ? '<div class="small"><span class="badge badge-orange">支援要求</span></div>' : "";
  const roleAnswer = log.role_answer
    ? `<div class="small">役割の答え: ${ROLE_LABEL[log.role_answer] || log.role_answer}${log.role_corrected ? ' <span class="met-ng">訂正</span>' : ""}</div>` : "";
  const timingHtml = log.latency_ms != null ? `<div class="muted small">${(log.latency_ms / 1000).toFixed(1)}s</div>` : "";
  const produced = (log.produced_structures || "").split(",").filter(Boolean);
  const countsHtml = log.strength == null
    ? `<div class="muted small">反復 ${log.stuck_count ?? 0} ／ 不一致 ${log.miss_count ?? 0}</div>`
    : `<div class="muted small">反復 ${log.stuck_count ?? 0} ／ 不一致 ${log.miss_count ?? 0} ／ 支援要求 ${log.help_count ?? 0}</div>`
      + `<div class="muted small">強度 ${log.strength}${log.strength_trigger && log.strength_trigger !== "none" ? `（↑${log.strength_trigger}）` : ""}`
      + `${log.target_structure ? ` ／ 目標 ${STRUCT_LABEL[log.target_structure] || log.target_structure}` : ""}</div>`;

  tr.innerHTML = `
    <td style="font-size:.78rem;color:#888;white-space:nowrap">${fmtDate(log.created_at)}</td>
    <td>${log.phase != null ? `<span class="badge badge-gray">${log.phase}</span>` : '<span style="color:#ccc">—</span>'}</td>
    <td>${inputBadge}</td>
    <td>
      <div class="msg-user">${esc(log.message)}</div>
      ${log.ai_message ? `<div class="msg-ai ${aiCls}">${richHtml(log.ai_message)}</div>` : ""}
      ${timingHtml}
    </td>
    <td>${judgeCell}</td>
    <td>${responseBadge(log.response_type, log.prompt_strength)}${declared}${roleAnswer}${helpReq}${selfLabel}</td>
    <td>${lightsHtml(produced)}${countsHtml}</td>
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
const RESPONSE_CLS = {
  form: "badge-orange", praise: "badge-green", prompt: "badge-red", talk: "badge-purple",
  done: "badge-green", error: "badge-gray",
};

// DB の時刻は JST（'YYYY-MM-DD HH:MM:SS'）。タイムゾーン付きで解釈する
function toDate(str) {
  if (!str) return null;
  const s = str.includes("T") ? str : str.replace(" ", "T");
  return new Date(/Z|[+-]\d\d:\d\d$/.test(s) ? s : s + "+09:00");
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
// **強調** と改行を HTML に（エスケープ後）
function richHtml(str) {
  return esc(str).split("\n").map(line => line.split("**").map((p, i) => i % 2 ? `<strong>${p}</strong>` : p).join("")).join("<br>");
}
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ===== Boot =====
showView("view-phase");
loadConfig();
