const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const state = {
  user: null,
  apartments: [],
  selectedAptId: null,
  aptDetail: null,
  activeTab: "dashboard",
  submitSubtab: "water",
  manageSubtab: "settings",
  historyMonth: "",
  aptReqSeq: 0,
  histReqSeq: 0,
};

const ROLE_RU = { admin: "Администратор", landlord: "Владелец", tenant: "Наниматель", revoked: "Отозван" };
const MONTH_RU = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, s => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[s]));
}

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return d ? `${d}.${m}.${y}` : iso;
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 3500);
}

function openModal(title, bodyHtml, buttons) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = bodyHtml;
  const actions = document.getElementById("modal-actions");
  actions.innerHTML = "";
  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.className = `btn ${b.cls || ""}`;
    btn.textContent = b.label;
    btn.onclick = () => { closeModal(); b.onClick?.(); };
    actions.appendChild(btn);
  });
  document.getElementById("modal-overlay").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  headers["X-Init-Data"] = tg?.initData || "";
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const resp = await fetch(path, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `Ошибка ${resp.status}`);
  return data;
}

async function loadBootstrap() {
  try {
    const me = await api("/api/me");
    state.user = me.user;
    state.apartments = me.apartments;
    if (!state.selectedAptId || !state.apartments.some(a => a.id === state.selectedAptId)) {
      state.selectedAptId = state.apartments[0]?.id || null;
    }

    document.getElementById("user-badge").textContent =
      `${state.user.name} · ${ROLE_RU[state.user.role] || state.user.role}`;

    renderAptSelector();
    renderNav();

    const params = new URLSearchParams(window.location.search);
    const tabParam = params.get("tab");
    if (tabParam === "water" || tabParam === "electricity") {
      state.activeTab = "submit";
      state.submitSubtab = tabParam;
    } else if (tabParam && ["dashboard", "apartments", "families", "history", "photo", "admin"].includes(tabParam)) {
      state.activeTab = tabParam;
    }

    if (state.selectedAptId) await loadApartmentDetail(state.selectedAptId);
    await renderTab();
  } catch (err) {
    document.getElementById("content").innerHTML = `
      <div class="card center">
        <h3>⛔ Нет доступа</h3>
        <p class="muted">${esc(err.message)}</p>
        <p class="small muted">Откройте приложение через кнопку меню в Telegram-боте.</p>
      </div>`;
  }
}

async function loadApartmentDetail(aptId) {
  if (!aptId) { state.aptDetail = null; return true; }
  const seq = ++state.aptReqSeq;
  const detail = await api(`/api/apartments/${aptId}`);
  if (seq !== state.aptReqSeq || aptId !== state.selectedAptId) return false;
  state.aptDetail = detail;
  return true;
}

function renderAptSelector() {
  const sel = document.getElementById("apt-selector");
  if (state.apartments.length <= 1) { sel.classList.add("hidden"); return; }
  sel.classList.remove("hidden");
  sel.innerHTML = state.apartments
    .map(a => `<option value="${a.id}" ${a.id === state.selectedAptId ? "selected" : ""}>Кв. №${esc(a.number)}</option>`)
    .join("");
  sel.onchange = async () => {
    state.selectedAptId = Number(sel.value);
    state.historyMonth = "";
    const ok = await loadApartmentDetail(state.selectedAptId);
    if (ok) await renderTab();
  };
}

function getNavItems() {
  const r = state.user.role;
  if (r === "tenant") {
    return [
      { id: "dashboard", ico: "📊", label: "Табло" },
      { id: "submit", ico: "💧", label: "Подать" },
      { id: "history", ico: "📜", label: "История" },
      { id: "photo", ico: "📷", label: "Фото" },
    ];
  }
  if (r === "landlord") {
    return [
      { id: "dashboard", ico: "📊", label: "Обзор" },
      { id: "submit", ico: "💧", label: "Подать" },
      { id: "apartments", ico: "🏠", label: "Квартиры" },
      { id: "history", ico: "📜", label: "История" },
      { id: "families", ico: "👨‍👩‍👧‍👦", label: "Семья" },
    ];
  }
  return [
    { id: "dashboard", ico: "📊", label: "Обзор" },
    { id: "submit", ico: "💧", label: "Подать" },
    { id: "apartments", ico: "🏠", label: "Квартиры" },
    { id: "families", ico: "👨‍👩‍👧‍👦", label: "Семьи" },
    { id: "admin", ico: "🔧", label: "Админ" },
  ];
}

function renderNav() {
  const nav = document.getElementById("bottom-nav");
  nav.classList.remove("hidden");
  const items = getNavItems();
  nav.innerHTML = items.map(it => `
    <button class="nav-item ${state.activeTab === it.id ? "active" : ""}" data-tab="${it.id}">
      <span class="ico">${it.ico}</span><span>${it.label}</span>
    </button>
  `).join("");
  nav.querySelectorAll(".nav-item").forEach(btn => {
    btn.onclick = async () => { state.activeTab = btn.dataset.tab; renderNav(); await renderTab(); };
  });
}

async function renderTab() {
  renderNav();
  const root = document.getElementById("content");
  try {
    if (state.activeTab === "dashboard") return renderDashboard(root);
    if (state.activeTab === "submit") return renderSubmit(root);
    if (state.activeTab === "history") return await renderHistory(root);
    if (state.activeTab === "photo") return renderPhoto(root);
    if (state.activeTab === "apartments") return renderApartments(root);
    if (state.activeTab === "families") return await renderFamilies(root);
    if (state.activeTab === "admin") return await renderAdmin(root);
  } catch (err) {
    root.innerHTML = `<div class="card center"><p class="muted">${esc(err.message)}</p></div>`;
  }
}

function renderMeterRow(m) {
  let subline;
  let rightCol;
  if (!m.submitted) {
    const prevTxt = m.has_base ? `пред.: ${m.previous} ${esc(m.unit)}` : "начальное не задано";
    subline = `Ещё не подано (${prevTxt})`;
    rightCol = `<span class="small muted">0 ₽</span>`;
  } else if (!m.has_base) {
    subline = `Подано: ${m.current} ${esc(m.unit)} · <b>не задано начальное показание</b>`;
    rightCol = `<span class="small muted">—</span>`;
  } else if (!m.has_tariff) {
    subline = `${m.previous} → ${m.current} ${esc(m.unit)} (расход ${m.consumption}) · <b>не задан тариф</b>`;
    rightCol = `<span class="small muted">—</span>`;
  } else {
    subline = `${m.previous} → ${m.current} ${esc(m.unit)} (расход ${m.consumption}, тариф ${m.tariff} ₽)`;
    rightCol = `<div class="bold">${m.cost} ₽</div>`;
  }
  return `
    <div class="table-row">
      <div><div class="bold">${esc(m.label)}</div><div class="small muted">${subline}</div></div>
      ${rightCol}
    </div>`;
}

function renderDashboard(root) {
  if (!state.aptDetail) {
    const canCreate = state.user.role !== "tenant";
    root.innerHTML = `
      <div class="card center">
        <h3>Квартиры не найдены</h3>
        <p class="muted">Добавьте первую квартиру, чтобы начать учёт.</p>
        ${canCreate ? `<button class="btn mt-8" id="go-create-apt">➕ Создать квартиру</button>` : ""}
      </div>`;
    document.getElementById("go-create-apt")?.addEventListener("click", () => { state.activeTab = "apartments"; renderTab(); });
    return;
  }

  const { apartment: apt, billing, tenants } = state.aptDetail;
  const rows = Object.values(billing.meters).map(renderMeterRow).join("");

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>🏠 Квартира №${esc(apt.number)}</h2>
        <span class="pill ${apt.has_tenant ? "ok" : ""}">${apt.has_tenant ? "👤 Сдана" : "Свободна"}</span>
      </div>
      <div class="small muted">📄 Договор: ${esc(apt.agreement_no || "—")} · ${fmtDate(apt.agreement_date_start)} — ${fmtDate(apt.agreement_date_finish)}</div>
      ${tenants.length ? `<div class="small muted mt-8">Жильцы: ${tenants.map(t => esc(t.name || t.telegram_id)).join(", ")}</div>` : ""}
      <div class="grid-3 mt-12">
        <div class="stat-box"><div class="label">💧 Вода</div><div class="val">${billing.water_total} ₽</div></div>
        <div class="stat-box"><div class="label">⚡ Свет</div><div class="val">${billing.electricity_total} ₽</div></div>
        <div class="stat-box"><div class="label">💰 Итого</div><div class="val">${billing.grand_total} ₽</div></div>
      </div>
    </div>
    <div class="card">
      <h3>📊 Расход за ${esc(billing.period_label)}</h3>
      ${rows}
    </div>
    <div class="grid-2">
      <button class="btn" id="quick-water">💧 Подать воду</button>
      <button class="btn" id="quick-elec">⚡ Подать свет</button>
    </div>
    ${state.user.role !== "tenant" ? `
      <div class="card">
        <h3>📤 Быстрая выгрузка в чат</h3>
        <div class="grid-2 mt-8">
          <button class="btn btn-secondary" id="exp-xlsx-one">📊 Excel (эта кв.)</button>
          <button class="btn btn-secondary" id="exp-pdf-one">📄 PDF (эта кв.)</button>
        </div>
        <div class="grid-2 mt-8">
          <button class="btn btn-secondary" id="exp-xlsx-all">📦 Excel (все)</button>
          <button class="btn btn-secondary" id="exp-pdf-all">📦 PDF (все)</button>
        </div>
      </div>` : ""}
  `;

  document.getElementById("quick-water").onclick = () => { state.activeTab = "submit"; state.submitSubtab = "water"; renderTab(); };
  document.getElementById("quick-elec").onclick = () => { state.activeTab = "submit"; state.submitSubtab = "electricity"; renderTab(); };
  if (state.user.role !== "tenant") {
    document.getElementById("exp-xlsx-one").onclick = () => doExport("excel", apt.id);
    document.getElementById("exp-pdf-one").onclick = () => doExport("pdf", apt.id);
    document.getElementById("exp-xlsx-all").onclick = () => doExport("excel", null);
    document.getElementById("exp-pdf-all").onclick = () => doExport("pdf", null);
  }
}

async function doExport(format, aptId) {
  try {
    showToast("Формирую файл...");
    await api("/api/export", { method: "POST", body: { format, apartment_id: aptId } });
    showToast("✅ Файл отправлен в чат бота!");
  } catch (err) { showToast("❌ " + err.message); }
}

function fmtPrevLabel(val, unit, tariff) {
  const p = val === null || val === undefined ? "не задано" : `<b>${val}</b> ${unit}`;
  const t = tariff === null || tariff === undefined ? "тариф не задан" : `тариф: ${tariff} ₽`;
  return `предыдущее: ${p}, ${t}`;
}

function renderSubmit(root) {
  if (!state.aptDetail) {
    root.innerHTML = `<div class="card center muted">Сначала выберите или создайте квартиру.</div>`;
    return;
  }
  const { apartment: apt, prev_readings: prev, tariffs } = state.aptDetail;
  const isWater = state.submitSubtab === "water";

  root.innerHTML = `
    <div class="card">
      <h2>Подача показаний — Кв. №${esc(apt.number)}</h2>
      <div class="subtabs">
        <button class="subtab ${isWater ? "active" : ""}" id="tab-w">💧 Водомеры</button>
        <button class="subtab ${!isWater ? "active" : ""}" id="tab-e">⚡ Электричество</button>
      </div>
      ${isWater ? `
        <div class="field"><label>💧 ХВС (${fmtPrevLabel(prev.cold_water, "м³", tariffs.cold_water)})</label><input type="number" inputmode="numeric" min="0" step="1" id="in-cold" placeholder="${prev.cold_water ?? ""}" /></div>
        <div class="field"><label>🔥 ГВС (${fmtPrevLabel(prev.hot_water, "м³", tariffs.hot_water)})</label><input type="number" inputmode="numeric" min="0" step="1" id="in-hot" placeholder="${prev.hot_water ?? ""}" /></div>
        <div id="calc-preview" class="preview-box muted">Введите целые значения для предрасчёта стоимости.</div>
        <button class="btn" id="btn-submit-water">✅ Сохранить показания воды</button>
      ` : `
        <div class="field"><label>⚡ Т1 (${fmtPrevLabel(prev.t1, "кВт·ч", tariffs.t1)})</label><input type="number" inputmode="numeric" min="0" step="1" id="in-t1" placeholder="${prev.t1 ?? ""}" /></div>
        <div class="field"><label>⚡ Т2 (${fmtPrevLabel(prev.t2, "кВт·ч", tariffs.t2)})</label><input type="number" inputmode="numeric" min="0" step="1" id="in-t2" placeholder="${prev.t2 ?? ""}" /></div>
        <div class="field"><label>⚡ Т3 (${fmtPrevLabel(prev.t3, "кВт·ч", tariffs.t3)})</label><input type="number" inputmode="numeric" min="0" step="1" id="in-t3" placeholder="${prev.t3 ?? ""}" /></div>
        <div id="calc-preview" class="preview-box muted">Введите целые значения для предрасчёта стоимости.</div>
        <button class="btn" id="btn-submit-elec">✅ Сохранить показания электричества</button>
      `}
    </div>
    ${state.user.role === "tenant" ? "" : `<div class="card"><button class="btn btn-secondary" id="open-photo-btn">📷 Отправить фото счётчиков</button></div>`}
  `;

  document.getElementById("tab-w").onclick = () => { state.submitSubtab = "water"; renderSubmit(root); };
  document.getElementById("tab-e").onclick = () => { state.submitSubtab = "electricity"; renderSubmit(root); };
  document.getElementById("open-photo-btn")?.addEventListener("click", () => { state.activeTab = "photo"; renderTab(); });

  if (isWater) {
    const updatePreview = () => {
      const c = parseInt(document.getElementById("in-cold").value, 10);
      const h = parseInt(document.getElementById("in-hot").value, 10);
      const box = document.getElementById("calc-preview");
      if (isNaN(c) || isNaN(h)) { box.textContent = "Введите оба значения (целые числа)."; return; }
      if (prev.cold_water === null || prev.hot_water === null) {
        box.innerHTML = "⚠️ Начальные показания не заданы — значения сохранятся без расчёта расхода.";
        return;
      }
      const dc = Math.max(0, c - prev.cold_water), dh = Math.max(0, h - prev.hot_water);
      const hasTariffs = tariffs.cold_water !== null && tariffs.hot_water !== null;
      const cost = hasTariffs ? `${Math.round(dc * tariffs.cold_water) + Math.round(dh * tariffs.hot_water)} ₽` : "тариф не задан";
      box.innerHTML = `Расход ХВС: <b>${dc} м³</b> · ГВС: <b>${dh} м³</b><br>Ориентировочно: <b>${cost}</b>`;
    };
    document.getElementById("in-cold").oninput = updatePreview;
    document.getElementById("in-hot").oninput = updatePreview;
    document.getElementById("btn-submit-water").onclick = () => submitWater(apt.id, false);
  } else {
    const updatePreview = () => {
      const v1 = parseInt(document.getElementById("in-t1").value, 10);
      const v2 = parseInt(document.getElementById("in-t2").value, 10);
      const v3 = parseInt(document.getElementById("in-t3").value, 10);
      const box = document.getElementById("calc-preview");
      if (isNaN(v1) || isNaN(v2) || isNaN(v3)) { box.textContent = "Введите Т1, Т2 и Т3 (целые числа)."; return; }
      if (prev.t1 === null || prev.t2 === null || prev.t3 === null) {
        box.innerHTML = "⚠️ Начальные показания не заданы — значения сохранятся без расчёта расхода.";
        return;
      }
      const d1 = Math.max(0, v1 - prev.t1), d2 = Math.max(0, v2 - prev.t2), d3 = Math.max(0, v3 - prev.t3);
      const hasTariffs = tariffs.t1 !== null && tariffs.t2 !== null && tariffs.t3 !== null;
      const cost = hasTariffs
        ? `${Math.round(d1 * tariffs.t1) + Math.round(d2 * tariffs.t2) + Math.round(d3 * tariffs.t3)} ₽`
        : "тариф не задан";
      box.innerHTML = `Расход: Т1 <b>${d1}</b> · Т2 <b>${d2}</b> · Т3 <b>${d3}</b> кВт·ч<br>Ориентировочно: <b>${cost}</b>`;
    };
    ["in-t1", "in-t2", "in-t3"].forEach(id => document.getElementById(id).oninput = updatePreview);
    document.getElementById("btn-submit-elec").onclick = () => submitElec(apt.id, false);
  }
}

function parseWholeInt(id) {
  const raw = document.getElementById(id).value.trim();
  return /^\d+$/.test(raw) ? parseInt(raw, 10) : null;
}

async function submitWater(aptId, force) {
  const cold = parseWholeInt("in-cold"), hot = parseWholeInt("in-hot");
  if (cold === null || hot === null) return showToast("Введите целые неотрицательные числа");
  const btn = document.getElementById("btn-submit-water");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/apartments/${aptId}/readings/water`, { method: "POST", body: { cold_water: cold, hot_water: hot, force } });
    if (res.needs_confirm) {
      if (btn) btn.disabled = false;
      return openModal("⚠️ Значение меньше предыдущего", `<p>${esc(res.warning)}</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        { label: "Сохранить", cls: "btn-danger", onClick: () => submitWater(aptId, true) },
      ]);
    }
    showToast(`✅ Вода сохранена (${res.billing.water_total} ₽)`);
    await loadApartmentDetail(aptId);
    state.activeTab = "dashboard";
    await renderTab();
  } catch (err) {
    showToast("❌ " + err.message);
    if (btn) btn.disabled = false;
  }
}

async function submitElec(aptId, force) {
  const t1 = parseWholeInt("in-t1"), t2 = parseWholeInt("in-t2"), t3 = parseWholeInt("in-t3");
  if (t1 === null || t2 === null || t3 === null) return showToast("Введите целые неотрицательные числа");
  const btn = document.getElementById("btn-submit-elec");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/apartments/${aptId}/readings/electricity`, { method: "POST", body: { t1, t2, t3, force } });
    if (res.needs_confirm) {
      if (btn) btn.disabled = false;
      return openModal("⚠️ Значение меньше предыдущего", `<p>${esc(res.warning)}</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        { label: "Сохранить", cls: "btn-danger", onClick: () => submitElec(aptId, true) },
      ]);
    }
    showToast(`✅ Электричество сохранено (${res.billing.electricity_total} ₽)`);
    await loadApartmentDetail(aptId);
    state.activeTab = "dashboard";
    await renderTab();
  } catch (err) {
    showToast("❌ " + err.message);
    if (btn) btn.disabled = false;
  }
}

async function renderHistory(root) {
  if (!state.selectedAptId) { root.innerHTML = `<div class="card center muted">Нет доступных квартир.</div>`; return; }
  const aptId = state.selectedAptId;
  const seq = ++state.histReqSeq;
  const q = state.historyMonth ? `?year=${state.historyMonth.split("-")[0]}&month=${state.historyMonth.split("-")[1]}` : "";
  const data = await api(`/api/apartments/${aptId}/history${q}`);
  if (seq !== state.histReqSeq || aptId !== state.selectedAptId || state.activeTab !== "history") return;

  const canEdit = state.user.role !== "tenant";
  const monthOptions = `<option value="">Все последние</option>` +
    data.months.map(m => {
      const key = `${m.year}-${m.month}`;
      return `<option value="${key}" ${state.historyMonth === key ? "selected" : ""}>${MONTH_RU[m.month]} ${m.year}</option>`;
    }).join("");

  const listHtml = data.readings.length
    ? data.readings.map(r => `
        <div class="table-row">
          <div>
            <div class="bold">${esc(r.label)}: ${r.value} ${esc(r.unit)}</div>
            <div class="small muted">#${r.id} · ${esc(r.reading_date)} · ID ${r.submitted_by}</div>
          </div>
          ${canEdit ? `<button class="btn btn-secondary btn-sm edit-reading-btn" data-id="${r.id}" data-val="${r.value}" data-lbl="${esc(r.label)}">✏️</button>` : ""}
        </div>`).join("")
    : `<p class="muted center">Показаний за выбранный период нет.</p>`;

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>📜 История — Кв. №${esc(state.aptDetail?.apartment.number || "")}</h2>
        <select id="month-filter" class="apt-select">${monthOptions}</select>
      </div>
      ${state.historyMonth ? `<div class="preview-box mt-8">За ${esc(data.billing.period_label)}: вода <b>${data.billing.water_total} ₽</b> · свет <b>${data.billing.electricity_total} ₽</b> · всего <b>${data.billing.grand_total} ₽</b></div>` : ""}
      <div class="mt-8">${listHtml}</div>
    </div>
  `;

  document.getElementById("month-filter").onchange = async (e) => { state.historyMonth = e.target.value; await renderHistory(root); };
  root.querySelectorAll(".edit-reading-btn").forEach(btn => {
    btn.onclick = () => {
      const id = Number(btn.dataset.id);
      openModal(`Корректировка #${id}`, `
        <p class="small muted">${esc(btn.dataset.lbl)}</p>
        <div class="field"><label>Новое целое значение</label><input type="number" min="0" step="1" id="corr-val" value="${btn.dataset.val}" /></div>
      `, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Сохранить",
          onClick: async () => {
            const val = parseInt(document.getElementById("corr-val").value, 10);
            if (isNaN(val) || val < 0) return showToast("Введите целое число >= 0");
            try {
              await api(`/api/readings/${id}`, { method: "PUT", body: { value: val } });
              showToast("✅ Показание исправлено");
              await loadApartmentDetail(state.selectedAptId);
              await renderHistory(root);
            } catch (err) { showToast("❌ " + err.message); }
          },
        },
      ]);
    };
  });
}

function renderPhoto(root) {
  if (!state.aptDetail) { root.innerHTML = `<div class="card center muted">Нет доступных квартир.</div>`; return; }
  const apt = state.aptDetail.apartment;
  root.innerHTML = `
    <div class="card">
      <h2>📷 Фото счётчиков — Кв. №${esc(apt.number)}</h2>
      <p class="small muted">Фото будет переслано владельцам квартиры в Telegram и сохранено на сервере на 30 дней.</p>
      <div class="field mt-8"><label>Выберите или сделайте фото (до 15 МБ)</label><input type="file" id="photo-file" accept="image/*" /></div>
      <div class="field"><label>Комментарий (необязательно)</label><input type="text" id="photo-comment" placeholder="Например: счётчики воды за ноябрь" /></div>
      <button class="btn" id="btn-send-photo">📤 Отправить владельцам</button>
    </div>
  `;
  document.getElementById("btn-send-photo").onclick = async () => {
    const fileInput = document.getElementById("photo-file");
    if (!fileInput.files.length) return showToast("Выберите фотографию");
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("comment", document.getElementById("photo-comment").value);
    const btn = document.getElementById("btn-send-photo");
    btn.disabled = true;
    try {
      const res = await api(`/api/apartments/${apt.id}/photo`, { method: "POST", body: fd });
      if (res.recipients_count > 0) {
        showToast(`✅ Фото отправлено (получателей: ${res.recipients_count})`);
      } else {
        showToast("⚠️ Фото сохранено, но получателей в Telegram не найдено");
      }
      fileInput.value = "";
      document.getElementById("photo-comment").value = "";
    } catch (err) { showToast("❌ " + err.message); }
    finally { btn.disabled = false; }
  };
}

function renderApartments(root) {
  const apt = state.aptDetail?.apartment;
  const tariffs = state.aptDetail?.tariffs || {};
  const initR = state.aptDetail?.initial_readings || {};
  const tenants = state.aptDetail?.tenants || [];

  const aptListChips = state.apartments.map(a => `
    <button class="btn btn-sm ${a.id === state.selectedAptId ? "" : "btn-secondary"}" data-apt="${a.id}">
      🏠 Кв. ${esc(a.number)}${a.has_tenant ? " 👤" : ""}
    </button>`).join("");

  let bodyHtml = "";
  if (apt) {
    if (state.manageSubtab === "settings") {
      bodyHtml = `
        <div class="grid-2">
          <div class="field"><label>📅 День подачи воды (1–31)</label><input type="number" min="1" max="31" id="s-wday" value="${apt.water_submission_day ?? ""}" /></div>
          <div class="field"><label>📅 День подачи света (1–31)</label><input type="number" min="1" max="31" id="s-eday" value="${apt.electricity_submission_day ?? ""}" /></div>
        </div>
        <div class="grid-2">
          <div class="field"><label>💧 ХВС (₽/м³)</label><input type="text" inputmode="decimal" id="t-cold" value="${tariffs.cold_water ?? ""}" /></div>
          <div class="field"><label>🔥 ГВС (₽/м³)</label><input type="text" inputmode="decimal" id="t-hot" value="${tariffs.hot_water ?? ""}" /></div>
        </div>
        <div class="grid-3">
          <div class="field"><label>⚡ Т1 (₽)</label><input type="text" inputmode="decimal" id="t-t1" value="${tariffs.t1 ?? ""}" /></div>
          <div class="field"><label>⚡ Т2 (₽)</label><input type="text" inputmode="decimal" id="t-t2" value="${tariffs.t2 ?? ""}" /></div>
          <div class="field"><label>⚡ Т3 (₽)</label><input type="text" inputmode="decimal" id="t-t3" value="${tariffs.t3 ?? ""}" /></div>
        </div>
        <div class="row-gap">
          <button class="btn" id="btn-save-settings">💾 Сохранить настройки и тарифы</button>
          <button class="btn btn-secondary" id="btn-tariff-hist">📋 История тарифов</button>
        </div>`;
    } else if (state.manageSubtab === "initial") {
      bodyHtml = `
        <p class="small muted">Оставьте поле пустым, если начальное значение ещё не задано (укажите 0, если счётчик новый).</p>
        <div class="grid-2 mt-8">
          <div class="field"><label>💧 ХВС (м³)</label><input type="number" min="0" id="i-cold" placeholder="Не задано" value="${initR.cold_water ?? ""}" /></div>
          <div class="field"><label>🔥 ГВС (м³)</label><input type="number" min="0" id="i-hot" placeholder="Не задано" value="${initR.hot_water ?? ""}" /></div>
        </div>
        <div class="grid-3">
          <div class="field"><label>⚡ Т1</label><input type="number" min="0" id="i-t1" placeholder="Не задано" value="${initR.t1 ?? ""}" /></div>
          <div class="field"><label>⚡ Т2</label><input type="number" min="0" id="i-t2" placeholder="Не задано" value="${initR.t2 ?? ""}" /></div>
          <div class="field"><label>⚡ Т3</label><input type="number" min="0" id="i-t3" placeholder="Не задано" value="${initR.t3 ?? ""}" /></div>
        </div>
        <button class="btn" id="btn-save-initial">💾 Сохранить начальные показания</button>`;
    } else {
      bodyHtml = `
        <div class="field"><label><input type="checkbox" id="c-has" ${apt.has_tenant ? "checked" : ""} style="width:auto;margin-right:6px" /> Наниматель проживает</label></div>
        <div class="field"><label>🔢 Номер договора</label><input type="text" id="c-no" value="${esc(apt.agreement_no || "")}" /></div>
        <div class="grid-2">
          <div class="field"><label>📅 Дата начала</label><input type="date" id="c-start" value="${apt.agreement_date_start || ""}" /></div>
          <div class="field"><label>📅 Дата окончания</label><input type="date" id="c-end" value="${apt.agreement_date_finish || ""}" /></div>
        </div>
        <div class="grid-2">
          <button class="btn" id="btn-save-contract">💾 Сохранить договор</button>
          <button class="btn btn-danger" id="btn-del-contract">🗑 Удалить договор</button>
        </div>
        <h3 class="mt-12">👤 Наниматели</h3>
        ${tenants.length ? tenants.map(t => `
          <div class="table-row">
            <div><div class="bold">${esc(t.name || "—")}</div><div class="small muted">ID: ${t.telegram_id}${t.phone ? " · " + esc(t.phone) : ""}</div></div>
            <button class="btn btn-danger btn-sm del-tenant-btn" data-tid="${t.telegram_id}">✕</button>
          </div>`).join("") : `<p class="small muted">Нанимателей пока нет.</p>`}
        <button class="btn btn-secondary mt-8" id="btn-add-tenant">➕ Добавить нанимателя</button>`;
    }
  }

  root.innerHTML = `
    <div class="card">
      <div class="row-between"><h2>🏠 Квартиры</h2><button class="btn btn-sm" id="btn-new-apt">➕ Новая</button></div>
      <div class="row-gap mt-8">${aptListChips || '<span class="muted small">Пока нет квартир</span>'}</div>
    </div>
    ${apt ? `
      <div class="card">
        <h3>Кв. №${esc(apt.number)}</h3>
        <div class="subtabs">
          <button class="subtab ${state.manageSubtab === "settings" ? "active" : ""}" data-m="settings">⚙️ Тарифы</button>
          <button class="subtab ${state.manageSubtab === "initial" ? "active" : ""}" data-m="initial">📏 Начальные</button>
          <button class="subtab ${state.manageSubtab === "contract" ? "active" : ""}" data-m="contract">📋 Договор</button>
        </div>
        ${bodyHtml}
      </div>` : ""}
  `;

  root.querySelectorAll("[data-apt]").forEach(btn => {
    btn.onclick = async () => {
      state.selectedAptId = Number(btn.dataset.apt);
      renderAptSelector();
      const ok = await loadApartmentDetail(state.selectedAptId);
      if (ok) renderApartments(root);
    };
  });
  root.querySelectorAll("[data-m]").forEach(btn => {
    btn.onclick = () => { state.manageSubtab = btn.dataset.m; renderApartments(root); };
  });

  document.getElementById("btn-new-apt").onclick = () => {
    openModal("➕ Новая квартира", `<div class="field"><label>Номер квартиры</label><input type="text" id="new-apt-num" placeholder="Например: 371" /></div>`, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Создать",
        onClick: async () => {
          const number = document.getElementById("new-apt-num").value.trim();
          if (!number) return showToast("Введите номер квартиры");
          try {
            const res = await api("/api/apartments", { method: "POST", body: { number } });
            state.selectedAptId = res.apartment.id;
            showToast(`✅ Квартира №${number} создана`);
            await loadBootstrap();
          } catch (err) { showToast("❌ " + err.message); }
        },
      },
    ]);
  };

  if (!apt) return;

  document.getElementById("btn-save-settings")?.addEventListener("click", async () => {
    const wday = document.getElementById("s-wday").value.trim();
    const eday = document.getElementById("s-eday").value.trim();
    try {
      await api(`/api/apartments/${apt.id}/settings`, {
        method: "PUT",
        body: {
          water_submission_day: wday ? Number(wday) : null,
          electricity_submission_day: eday ? Number(eday) : null,
          tariffs: {
            cold_water: document.getElementById("t-cold").value.trim() || null,
            hot_water: document.getElementById("t-hot").value.trim() || null,
            t1: document.getElementById("t-t1").value.trim() || null,
            t2: document.getElementById("t-t2").value.trim() || null,
            t3: document.getElementById("t-t3").value.trim() || null,
          },
        },
      });
      showToast("✅ Настройки сохранены");
      await loadApartmentDetail(apt.id);
      renderApartments(root);
    } catch (err) { showToast("❌ " + err.message); }
  });

  document.getElementById("btn-tariff-hist")?.addEventListener("click", async () => {
    const res = await api(`/api/apartments/${apt.id}/tariffs/history`);
    const html = res.items.length
      ? res.items.map(i => `<div class="table-row"><span>${esc(i.label)} · ${esc(i.effective_from)}</span><b>${i.rate} ₽</b></div>`).join("")
      : `<p class="muted">История тарифов пуста.</p>`;
    openModal(`История тарифов — Кв. №${apt.number}`, `<div style="max-height:55vh;overflow:auto">${html}</div>`, [{ label: "Закрыть", cls: "btn-secondary" }]);
  });

  document.getElementById("btn-save-initial")?.addEventListener("click", async () => {
    const optInt = (id) => {
      const v = document.getElementById(id).value.trim();
      return v === "" ? null : Number(v);
    };
    try {
      await api(`/api/apartments/${apt.id}/initial-readings`, {
        method: "PUT",
        body: {
          cold_water: optInt("i-cold"),
          hot_water: optInt("i-hot"),
          t1: optInt("i-t1"),
          t2: optInt("i-t2"),
          t3: optInt("i-t3"),
        },
      });
      showToast("✅ Начальные показания сохранены");
      await loadApartmentDetail(apt.id);
    } catch (err) { showToast("❌ " + err.message); }
  });

  document.getElementById("btn-save-contract")?.addEventListener("click", async () => {
    try {
      await api(`/api/apartments/${apt.id}/contract`, {
        method: "PUT",
        body: {
          has_tenant: document.getElementById("c-has").checked,
          agreement_no: document.getElementById("c-no").value.trim() || null,
          agreement_date_start: document.getElementById("c-start").value || null,
          agreement_date_finish: document.getElementById("c-end").value || null,
        },
      });
      showToast("✅ Договор сохранён");
      await loadApartmentDetail(apt.id);
      renderApartments(root);
    } catch (err) { showToast("❌ " + err.message); }
  });

  document.getElementById("btn-del-contract")?.addEventListener("click", () => {
    openModal("🗑 Удалить договор?", `<p>Очистить данные договора для кв. №${esc(apt.number)}?</p>`, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Удалить", cls: "btn-danger",
        onClick: async () => {
          await api(`/api/apartments/${apt.id}/contract`, { method: "DELETE" });
          showToast("✅ Договор удалён");
          await loadApartmentDetail(apt.id);
          renderApartments(root);
        },
      },
    ]);
  });

  document.getElementById("btn-add-tenant")?.addEventListener("click", () => {
    openModal("👤 Добавить нанимателя", `
      <div class="field"><label>Telegram ID (через @userinfobot)</label><input type="number" id="nt-id" /></div>
      <div class="field"><label>Имя</label><input type="text" id="nt-name" /></div>
      <div class="field"><label>Телефон (необязательно)</label><input type="text" id="nt-phone" /></div>
      <p class="small muted">Бот сможет отправить приглашение только если наниматель ранее уже нажимал /start.</p>
    `, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Добавить",
        onClick: () => submitAddTenant(apt.id, {
          telegram_id: Number(document.getElementById("nt-id").value),
          name: document.getElementById("nt-name").value.trim(),
          phone: document.getElementById("nt-phone").value.trim() || null,
          force_no_contract: false,
        }),
      },
    ]);
  });

  root.querySelectorAll(".del-tenant-btn").forEach(btn => {
    btn.onclick = () => {
      const tid = Number(btn.dataset.tid);
      openModal("Удалить нанимателя?", `<p>Отозвать доступ у ID ${tid} к кв. №${esc(apt.number)}?</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            await api(`/api/apartments/${apt.id}/tenants/${tid}`, { method: "DELETE" });
            showToast("✅ Наниматель удалён");
            await loadApartmentDetail(apt.id);
            renderApartments(root);
          },
        },
      ]);
    };
  });
}

async function submitAddTenant(aptId, payload) {
  try {
    const res = await api(`/api/apartments/${aptId}/tenants`, { method: "POST", body: payload });
    if (res.needs_confirm) {
      return openModal("⚠️ Договор не заполнен", `<p>${esc(res.warning)}</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        { label: "Добавить", onClick: () => submitAddTenant(aptId, { ...payload, force_no_contract: true }) },
      ]);
    }
    showToast(
      res.notified
        ? "✅ Наниматель добавлен, приглашение отправлено"
        : "✅ Наниматель добавлен (попросите его нажать /start в боте)"
    );
    await loadApartmentDetail(aptId);
    renderTab();
  } catch (err) { showToast("❌ " + err.message); }
}

async function renderFamilies(root) {
  const data = await api("/api/families");
  root.innerHTML = `
    <div class="card"><div class="row-between"><h2>👨‍👩‍👧‍👦 Семьи</h2>${data.can_create ? `<button class="btn btn-sm" id="btn-new-fam">➕ Создать</button>` : ""}</div></div>
    ${data.families.map(f => `
      <div class="card">
        <div class="row-between"><h3>${esc(f.name)}</h3><span class="pill">🏠 ${f.apartments_count}</span></div>
        <div class="mt-8">
          ${f.owners.length ? f.owners.map(o => `
            <div class="table-row">
              <div><div class="bold">${esc(o.name || "—")}</div><div class="small muted">ID: ${o.telegram_id}${o.phone ? " · " + esc(o.phone) : ""}</div></div>
              <button class="btn btn-danger btn-sm del-owner-btn" data-fid="${f.id}" data-oid="${o.telegram_id}" data-oname="${esc(o.name || o.telegram_id)}">✕</button>
            </div>`).join("") : `<p class="small muted">Владельцев нет.</p>`}
        </div>
        <div class="row-gap mt-12">
          <button class="btn btn-secondary btn-sm add-owner-btn" data-fid="${f.id}">🔑 + Владелец</button>
          <button class="btn btn-secondary btn-sm ren-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}">✏️ Имя</button>
          ${data.can_create ? `<button class="btn btn-danger btn-sm del-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}">🗑</button>` : ""}
        </div>
      </div>`).join("")}
  `;

  document.getElementById("btn-new-fam")?.addEventListener("click", () => {
    openModal("➕ Новая семья", `<div class="field"><label>Название</label><input type="text" id="nf-name" /></div>`, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Создать",
        onClick: async () => {
          try {
            await api("/api/families", { method: "POST", body: { name: document.getElementById("nf-name").value } });
            showToast("✅ Семья создана");
            await renderFamilies(root);
          } catch (err) { showToast("❌ " + err.message); }
        },
      },
    ]);
  });

  root.querySelectorAll(".ren-fam-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("✏️ Переименовать семью", `<div class="field"><input type="text" id="rf-name" value="${esc(btn.dataset.name)}" /></div>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Сохранить",
          onClick: async () => {
            try {
              await api(`/api/families/${btn.dataset.fid}`, { method: "PUT", body: { name: document.getElementById("rf-name").value } });
              showToast("✅ Переименовано");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message); }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".add-owner-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("🔑 Добавить владельца", `
        <div class="field"><label>Telegram ID</label><input type="number" id="no-id" /></div>
        <div class="field"><label>Имя</label><input type="text" id="no-name" /></div>
        <div class="field"><label>Телефон (необязательно)</label><input type="text" id="no-phone" /></div>
      `, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Добавить",
          onClick: async () => {
            try {
              await api(`/api/families/${btn.dataset.fid}/owners`, {
                method: "POST",
                body: {
                  telegram_id: Number(document.getElementById("no-id").value),
                  name: document.getElementById("no-name").value.trim(),
                  phone: document.getElementById("no-phone").value.trim() || null,
                },
              });
              showToast("✅ Владелец добавлен");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message); }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".del-owner-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("Удалить владельца?", `<p>Удалить владельца <b>${esc(btn.dataset.oname)}</b> (ID ${btn.dataset.oid})?</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            try {
              await api(`/api/families/${btn.dataset.fid}/owners/${btn.dataset.oid}`, { method: "DELETE" });
              showToast("✅ Владелец удалён");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message); }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".del-fam-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("🗑 Удалить семью?", `<p>Удалить «${esc(btn.dataset.name)}» вместе со всеми квартирами, показаниями и привязанными пользователями?</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            await api(`/api/families/${btn.dataset.fid}`, { method: "DELETE" });
            showToast("✅ Семья удалена");
            await loadBootstrap();
          },
        },
      ]);
    };
  });
}

async function renderAdmin(root) {
  const [usersRes, auditRes] = await Promise.all([api("/api/admin/users"), api("/api/admin/audit")]);
  root.innerHTML = `
    <div class="card">
      <h2>🔧 Администрирование</h2>
      <div class="grid-2 mt-8">
        <button class="btn" id="btn-backup">💾 Бэкап БД в чат</button>
        <button class="btn btn-secondary" id="btn-go-hist">✏️ Корректировка</button>
      </div>
      <div class="grid-2 mt-8">
        <button class="btn btn-secondary" id="adm-xlsx">📊 Excel (все)</button>
        <button class="btn btn-secondary" id="adm-pdf">📄 PDF (все)</button>
      </div>
    </div>
    <div class="card">
      <h3>👥 Пользователи (${usersRes.users.length})</h3>
      ${usersRes.users.map(u => `
        <div class="table-row">
          <div><div class="bold">${esc(u.name || "—")} ${u.is_blocked ? "🚫" : ""}</div><div class="small muted">ID: ${u.telegram_id}${u.phone ? " · " + esc(u.phone) : ""}</div></div>
          <span class="pill">${esc(ROLE_RU[u.role] || u.role)}</span>
        </div>`).join("")}
    </div>
    <div class="card">
      <h3>📋 Журнал действий</h3>
      ${auditRes.logs.slice(0, 30).map(l => `
        <div class="table-row">
          <div><div class="small bold">${esc(l.action)} · <span class="muted">${esc(l.created_at)}</span></div><div class="small muted">ID ${l.user_telegram_id}: ${esc(l.details || "")}</div></div>
        </div>`).join("") || `<p class="muted small">Журнал пуст.</p>`}
    </div>
  `;

  document.getElementById("btn-backup").onclick = async () => {
    try {
      showToast("Создаю бэкап...");
      await api("/api/admin/backup", { method: "POST" });
      showToast("✅ Бэкап отправлен в чат бота!");
    } catch (err) { showToast("❌ " + err.message); }
  };
  document.getElementById("btn-go-hist").onclick = () => { state.activeTab = "history"; renderTab(); };
  document.getElementById("adm-xlsx").onclick = () => doExport("excel", null);
  document.getElementById("adm-pdf").onclick = () => doExport("pdf", null);
}

loadBootstrap();
