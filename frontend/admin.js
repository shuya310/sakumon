let currentUserId = null;

// ===== API helper =====
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) throw new Error(await res.text());
  if (options.method === "DELETE") return {};
  return res.json();
}

function showView(id) {
  ["view-students", "view-student-detail"].forEach(v => {
    document.getElementById(v).style.display = "none";
  });
  document.getElementById(id).style.display = "block";
}

// ===== 児童一覧 =====
async function loadStudents() {
  showView("view-students");
  setBreadcrumb([{ label: "児童一覧" }]);
  const tbody = document.getElementById("students-tbody");
  tbody.innerHTML = `<tr><td colspan="3" class="empty">読み込み中…</td></tr>`;
  try {
    const students = await api("/admin/api/students");
    if (students.length === 0) {
      tbody.innerHTML = `<tr><td colspan="3" class="empty">データがありません</td></tr>`;
      return;
    }
    tbody.innerHTML = "";
    students.forEach(s => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `
        <td><strong>${s.user_id}</strong></td>
        <td>${fmtDate(s.last_login)}</td>
        <td>${structureBadges(s.structure_count)}</td>
      `;
      tr.addEventListener("click", () => loadStudentDetail(s.user_id));
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty">読み込みエラー</td></tr>`;
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
  setBreadcrumb([
    { label: "児童一覧", action: loadStudents },
    { label: `${userId} さん` },
  ]);
  document.getElementById("detail-title").textContent = `${userId} さんのセッション一覧`;
  const container = document.getElementById("sessions-container");
  container.innerHTML = `<div class="spinner">読み込み中…</div>`;

  try {
    const data = await api(`/admin/api/students/${userId}`);
    container.innerHTML = "";
    if (data.sessions.length === 0) {
      container.innerHTML = `<div class="empty">セッションがありません</div>`;
      return;
    }
    data.sessions.forEach(s => {
      container.appendChild(buildSessionBlock(s, userId));
    });
  } catch (e) {
    container.innerHTML = `<div class="empty">読み込みエラー</div>`;
  }
}

function buildSessionBlock(session, userId) {
  const block = document.createElement("div");
  block.className = "session-block";
  block.dataset.sessionId = session.session_id;

  const structs = session.structures.map(x => STRUCT_LABEL[x] || x).join("・") || "なし";
  const head = document.createElement("div");
  head.className = "session-head";
  head.innerHTML = `
    <div class="session-head-left">
      <div>
        <div class="s-date">${fmtDate(session.created_at)}</div>
        <div class="s-stat">作問 ${session.new_count} 件（計 ${session.total_count} 回）　${structs}</div>
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
    if (!confirm(`セッション #${session.session_id} を削除しますか？`)) return;
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
  tbl.innerHTML = `
    <thead>
      <tr>
        <th style="width:130px">日時</th>
        <th>入力 / AIの返答</th>
        <th style="width:90px">構造</th>
        <th style="width:60px">新規</th>
        <th style="width:60px"></th>
      </tr>
    </thead>
  `;
  const tbody = document.createElement("tbody");
  logs.forEach(log => tbody.appendChild(buildLogRow(log)));
  tbl.appendChild(tbody);
  return tbl;
}

function buildLogRow(log) {
  const tr = document.createElement("tr");
  tr.className = "log-row";
  tr.dataset.logId = log.id;

  const aiCls = {
    new_structure: "new-structure",
    hint1: "hint", hint2: "hint", hint3: "hint",
    clear: "clear",
  }[log.display_type] || "";

  tr.innerHTML = `
    <td style="font-size:.78rem;color:#888;white-space:nowrap">${fmtDate(log.created_at)}</td>
    <td>
      <div class="msg-user">${esc(log.message)}</div>
      <div class="msg-ai ${aiCls}">${esc(log.ai_message)}</div>
    </td>
    <td>${log.structure
      ? `<span class="badge badge-blue">${STRUCT_LABEL[log.structure] || log.structure}</span>`
      : '<span style="color:#ccc">—</span>'}</td>
    <td>${log.is_new
      ? '<span class="badge badge-green">新規</span>'
      : '<span style="color:#ccc">—</span>'}</td>
    <td>
      <button class="btn btn-danger btn-sm">削除</button>
    </td>
  `;

  tr.querySelector(".btn-danger").addEventListener("click", async () => {
    if (!confirm("このチャットを削除しますか？")) return;
    await api(`/admin/api/logs/${log.id}`, { method: "DELETE" });
    tr.remove();
  });

  return tr;
}

// ===== CSV エクスポート =====
document.getElementById("btn-export-csv").addEventListener("click", async () => {
  const res = await fetch("/admin/api/export/csv");
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
function setBreadcrumb(items) {
  const el = document.getElementById("breadcrumb");
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
const STRUCT_LABEL = { tobun: "とうぶんじょ", hougan: "ほうがんじょ", bai: "ばい" };

function fmtDate(str) {
  if (!str) return "—";
  const d = new Date(str.endsWith("Z") ? str : str + "Z");
  return d.toLocaleDateString("ja-JP", {
    month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function esc(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ===== Boot =====
loadStudents();
