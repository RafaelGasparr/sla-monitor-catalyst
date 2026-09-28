const API_BASE = "/server/sla_monitor";
let _cachedViolations = [];
let _activeTab = "violations";
let _predictLoaded = false;

async function apiFetch(path, options = {}) {
  const res = await fetch(API_BASE + path, options);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const body = await res.json(); msg = body.error || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function topEntry(obj) {
  if (!obj || !Object.keys(obj).length) return "–";
  return Object.entries(obj).sort((a, b) => b[1] - a[1])[0][0];
}

function priorityClass(p) {
  if (!p) return "";
  const lower = p.toLowerCase();
  if (lower === "high" || lower === "urgent") return "priority priority-high";
  if (lower === "medium" || lower === "normal") return "priority priority-medium";
  return "priority priority-low";
}

function formatDate(str) {
  if (!str) return "–";
  return new Date(str).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" });
}

// ── Tab navigation ────────────────────────────────────────────────────────────

function switchTab(tabName) {
  _activeTab = tabName;
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach(panel => {
    panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`);
  });
  if (tabName === "predict" && !_predictLoaded) loadPredict();
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// ── Violations tab ────────────────────────────────────────────────────────────

async function loadMetrics() {
  try {
    const metrics = await apiFetch("/metrics");
    document.getElementById("metric-total").textContent = metrics.total ?? "–";
    document.getElementById("metric-top-category").textContent = topEntry(metrics.by_category);
    document.getElementById("metric-top-agent").textContent = topEntry(metrics.by_agent);
    document.getElementById("metric-top-priority").textContent = topEntry(metrics.by_priority);
  } catch (e) {
    console.error("Metrics error:", e);
  }
}

async function loadViolations() {
  const tbody = document.getElementById("violations-body");
  const badge = document.getElementById("badge-count");
  tbody.innerHTML = `<tr><td colspan="6" class="empty">Carregando…</td></tr>`;

  try {
    const { data, count } = await apiFetch("/violations");
    _cachedViolations = data || [];
    badge.textContent = count;

    if (!data || data.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty">Nenhuma infração encontrada.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.map(t => {
      const agent = t.assignee ? t.assignee.firstName : "–";
      const priority = t.priority || "–";
      return `<tr>
        <td>${t.ticketNumber || "–"}</td>
        <td>${t.subject || "–"}</td>
        <td>${t.category || "–"}</td>
        <td><span class="${priorityClass(priority)}">${priority}</span></td>
        <td>${agent}</td>
        <td>${formatDate(t.createdTime)}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">Erro ao carregar tickets: ${e.message}</td></tr>`;
  }
}

async function runAnalysis() {
  const btn = document.getElementById("btn-analyze");
  const container = document.getElementById("insights-container");
  const loading = document.getElementById("insights-loading");
  const empty = document.getElementById("insights-empty");

  btn.disabled = true;
  container.classList.add("hidden");
  empty.classList.add("hidden");
  loading.classList.remove("hidden");

  try {
    const insights = await apiFetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ violations: _cachedViolations }),
    });

    const blocks = [
      { key: "recurring_issues",  title: "Problemas Recorrentes", icon: "🔁" },
      { key: "product_defects",   title: "Possíveis Defeitos",    icon: "⚠️" },
      { key: "delay_patterns",    title: "Padrões de Atraso",     icon: "🕐" },
      { key: "recommendations",   title: "Recomendações",         icon: "💡" },
    ];

    container.innerHTML = blocks.map(({ key, title, icon }) => {
      const items = insights[key];
      if (!items || !items.length) return "";
      const list = items.map(i => `<li>${i}</li>`).join("");
      return `<div class="insight-block">
        <div class="insight-title"><span class="insight-icon">${icon}</span>${title}</div>
        <ul class="insight-list">${list}</ul>
      </div>`;
    }).join("");

    loading.classList.add("hidden");
    container.classList.remove("hidden");
  } catch (e) {
    loading.classList.add("hidden");
    empty.textContent = `Erro na análise: ${e.message}`;
    empty.classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
}

// ── Predictor tab ─────────────────────────────────────────────────────────────

async function loadPredict() {
  const tbody = document.getElementById("predict-body");
  const badge = document.getElementById("badge-risk");
  tbody.innerHTML = `<tr><td colspan="6" class="empty">Carregando…</td></tr>`;

  try {
    const { data, count, by_risk } = await apiFetch("/predict");
    _predictLoaded = true;

    document.getElementById("risk-alto").textContent = by_risk.alto ?? 0;
    document.getElementById("risk-medio").textContent = by_risk.medio ?? 0;
    document.getElementById("risk-baixo").textContent = by_risk.baixo ?? 0;
    document.getElementById("risk-total").textContent = count;
    badge.textContent = (by_risk.alto ?? 0) + (by_risk.medio ?? 0);

    if (!data || data.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty">Nenhum ticket em risco encontrado.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.map(t => {
      const agent = t.assignee ? t.assignee.firstName : "–";
      const priority = t.priority || "–";
      const score = t._risk_score ?? 0;
      const level = t._risk_level || "baixo";
      const remaining = t._time_remaining || "–";
      return `<tr>
        <td>${t.ticketNumber || "–"}</td>
        <td>${t.subject || "–"}</td>
        <td><span class="${priorityClass(priority)}">${priority}</span></td>
        <td>${agent}</td>
        <td class="time-remaining ${level}">${remaining}</td>
        <td>
          <div class="risk-bar-wrap">
            <div class="risk-bar risk-${level}" style="width:${score}%"></div>
          </div>
          <span class="risk-score-label">${score}%</span>
        </td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">Erro ao carregar predições: ${e.message}</td></tr>`;
  }
}

// ── Refresh ───────────────────────────────────────────────────────────────────

async function refresh() {
  if (_activeTab === "violations") {
    await Promise.all([loadMetrics(), loadViolations()]);
  } else {
    _predictLoaded = false;
    await loadPredict();
  }
}

document.getElementById("btn-refresh").addEventListener("click", refresh);
document.getElementById("btn-analyze").addEventListener("click", runAnalysis);

refresh();
