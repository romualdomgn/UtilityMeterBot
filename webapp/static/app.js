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

const PROPERTY_TYPES = [
  { id: "apartment", icon: "🏢", label: "Квартира", numLabel: "Номер квартиры", placeholder: "Например: 371" },
  { id: "house", icon: "🏡", label: "Дом", numLabel: "Адрес или номер дома", placeholder: "Например: Лесная, 12" },
  { id: "dacha", icon: "🌿", label: "Дача", numLabel: "Название или номер участка", placeholder: "Например: СНТ Весна, уч. 45" },
];

const METER_META = [
  { id: "cold_water", group: "water", label: "💧 ХВС", plain: "ХВС", unit: "м³", tariffLabel: "💧 ХВС (₽/м³)" },
  { id: "hot_water", group: "water", label: "♨️ ГВС", plain: "ГВС", unit: "м³", tariffLabel: "♨️ ГВС (₽/м³)" },
  { id: "t1", group: "electricity", label: "⚡ Т1", plain: "Т1", unit: "кВт·ч", tariffLabel: "⚡ Т1 (₽)" },
  { id: "t2", group: "electricity", label: "⚡ Т2", plain: "Т2", unit: "кВт·ч", tariffLabel: "⚡ Т2 (₽)" },
  { id: "t3", group: "electricity", label: "⚡ Т3", plain: "Т3", unit: "кВт·ч", tariffLabel: "⚡ Т3 (₽)" },
  { id: "gas", group: "gas", label: "🔥 Газ", plain: "Газ", unit: "м³", tariffLabel: "🔥 Газ (₽/м³)" },
];

const DEFAULT_NEW_METERS = ["cold_water", "hot_water", "t1"];

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

function haptic(kind = "light") {
  try {
    const hf = tg?.HapticFeedback;
    if (!hf) return;
    if (kind === "success" || kind === "error" || kind === "warning") {
      hf.notificationOccurred(kind);
    } else {
      hf.impactOccurred(kind);
    }
  } catch (_) {}
}

function showToast(msg, type = "info") {
  const el = document.getElementById("toast");
  const isErr = type === "error" || msg.startsWith("❌");
  const isOk = type === "ok" || msg.startsWith("✅");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " toast-error" : isOk ? " toast-ok" : "");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), isErr ? 5500 : 3400);
  if (isErr) haptic("error");
  else if (isOk) haptic("success");
}

document.getElementById("toast")?.addEventListener("click", () => {
  document.getElementById("toast")?.classList.add("hidden");
});

function showLoading(root, text = "Загрузка...") {
  root.innerHTML = `
    <div class="card center muted loading-card">
      <span class="spinner"></span>
      <span>${esc(text)}</span>
    </div>`;
}

function getPropertyMeta(typeId) {
  return PROPERTY_TYPES.find(t => t.id === typeId) || PROPERTY_TYPES[0];
}

function propTitle(apt) {
  if (!apt) return "";
  return apt.full_title || apt.display_name || `Кв. №${apt.number}`;
}

function propShort(apt) {
  if (!apt) return "";
  return apt.display_name || `Кв. №${apt.number}`;
}

function propIcon(apt) {
  return apt?.property_icon || getPropertyMeta(apt?.property_type).icon;
}

function getEnabledMeters(apt) {
  if (Array.isArray(apt?.enabled_meters) && apt.enabled_meters.length) {
    return apt.enabled_meters;
  }
  return ["cold_water", "hot_water", "t1", "t2", "t3"];
}

function getEnabledGroups(apt) {
  const enabled = new Set(getEnabledMeters(apt));
  const groups = [];
  if (enabled.has("cold_water") || enabled.has("hot_water")) groups.push("water");
  if (enabled.has("t1") || enabled.has("t2") || enabled.has("t3")) groups.push("electricity");
  if (enabled.has("gas")) groups.push("gas");
  return groups;
}

// ── Phone mask: +7-xxx-xxx-xxxx ───────────────────────────────────────
function formatRuPhone(raw) {
  let digits = String(raw || "").replace(/\D/g, "");
  if (digits.startsWith("7")) {
    digits = digits.slice(1);
    if (digits.length === 11 && (digits.startsWith("7") || digits.startsWith("8"))) {
      digits = digits.slice(1);
    }
  } else if (digits.length >= 11 && digits.startsWith("8")) {
    digits = digits.slice(1);
  }
  digits = digits.slice(0, 10);
  if (!digits.length) return "+7-";
  if (digits.length <= 3) return `+7-${digits}`;
  if (digits.length <= 6) return `+7-${digits.slice(0, 3)}-${digits.slice(3)}`;
  return `+7-${digits.slice(0, 3)}-${digits.slice(3, 6)}-${digits.slice(6, 10)}`;
}

function attachPhoneMask(input) {
  if (!input) return;
  input.value = formatRuPhone(input.value);

  const ensureCaretAfterPrefix = () => {
    const pos = input.selectionStart ?? input.value.length;
    if (pos < 3) {
      input.setSelectionRange(input.value.length, input.value.length);
    }
  };

  input.addEventListener("focus", () => {
    if (!input.value.startsWith("+7-")) input.value = formatRuPhone(input.value);
    setTimeout(ensureCaretAfterPrefix, 0);
  });

  input.addEventListener("click", ensureCaretAfterPrefix);

  input.addEventListener("keydown", (e) => {
    const start = input.selectionStart ?? 0;
    const end = input.selectionEnd ?? 0;
    if (e.key === "Backspace" && start <= 3 && end <= 3) {
      e.preventDefault();
      return;
    }
    if (e.key === "Delete" && start < 3) {
      e.preventDefault();
    }
  });

  input.addEventListener("input", () => {
    input.value = formatRuPhone(input.value);
  });
}

function readMaskedPhone(inputId) {
  const el = document.getElementById(inputId);
  if (!el) return { ok: true, value: null };
  const val = formatRuPhone(el.value);
  el.value = val;
  if (val === "+7-") return { ok: true, value: null };
  const subDigits = val.slice(3).replace(/\D/g, "");
  if (subDigits.length !== 10) {
    return { ok: false, error: "Введите полный номер в формате +7-xxx-xxx-xxxx или оставьте поле пустым" };
  }
  return { ok: true, value: val };
}

function wireTelegramLinks(container) {
  container.querySelectorAll(".tg-link").forEach(link => {
    link.addEventListener("click", (e) => {
      const url = link.dataset.tgLink || link.getAttribute("href");
      if (url && tg?.openTelegramLink) {
        e.preventDefault();
        tg.openTelegramLink(url);
      }
    });
  });
}

// ── Modal dialog ──────────────────────────────────────────────────────
function openModal(title, bodyHtml, buttons) {
  document.getElementById("modal-title").textContent = title;
  const bodyEl = document.getElementById("modal-body");
  bodyEl.innerHTML = bodyHtml;
  bodyEl.querySelectorAll(".phone-mask").forEach(attachPhoneMask);
  wireTelegramLinks(bodyEl);

  const actions = document.getElementById("modal-actions");
  actions.innerHTML = "";
  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.className = `btn ${b.cls || ""}`;
    btn.textContent = b.label;
    btn.onclick = async () => {
      if (!b.onClick) { closeModal(); return; }
      const result = await b.onClick();
      if (result !== false) closeModal();
    };
    actions.appendChild(btn);
  });
  document.getElementById("modal-overlay").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
}

document.getElementById("modal-overlay")?.addEventListener("click", (e) => {
  if (e.target.id === "modal-overlay") closeModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("modal-overlay").classList.contains("hidden")) {
    closeModal();
  }
});

// ── API client ────────────────────────────────────────────────────────
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

function updateUserBadge() {
  if (!state.user) return;
  document.getElementById("user-badge").textContent =
    `${state.user.name} · ${ROLE_RU[state.user.role] || state.user.role}`;
  const btn = document.getElementById("edit-profile-btn");
  const ico = document.getElementById("edit-profile-ico");
  btn.disabled = false;
  ico.classList.remove("hidden");
}

function openEditOwnNameModal() {
  if (!state.user) return;
  openModal("✏️ Изменить моё имя", `
    <div class="field">
      <label>Ваше отображаемое имя</label>
      <input type="text" id="own-name-input" maxlength="120" value="${esc(state.user.name)}" placeholder="Введите имя" />
    </div>
  `, [
    { label: "Отмена", cls: "btn-secondary" },
    {
      label: "Сохранить",
      onClick: async () => {
        const name = document.getElementById("own-name-input").value.trim();
        if (!name) { showToast("Имя не может быть пустым", "error"); return false; }
        try {
          const res = await api("/api/me/profile", { method: "PUT", body: { name } });
          state.user.name = res.name;
          updateUserBadge();
          showToast("✅ Имя обновлено");
          if (state.selectedAptId) await loadApartmentDetail(state.selectedAptId);
          await renderTab();
        } catch (err) {
          showToast("❌ " + err.message, "error");
          return false;
        }
      },
    },
  ]);
}

document.getElementById("edit-profile-btn")?.addEventListener("click", openEditOwnNameModal);

// ── Bootstrap ─────────────────────────────────────────────────────────
async function loadBootstrap() {
  try {
    const me = await api("/api/me");
    state.user = me.user;
    state.apartments = me.apartments;
    if (!state.selectedAptId || !state.apartments.some(a => a.id === state.selectedAptId)) {
      state.selectedAptId = state.apartments[0]?.id || null;
    }

    updateUserBadge();
    renderAptSelector();
    renderNav();

    const params = new URLSearchParams(window.location.search);
    const tabParam = params.get("tab");
    if (tabParam === "water" || tabParam === "electricity" || tabParam === "gas") {
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
    .map(a => `<option value="${a.id}" ${a.id === state.selectedAptId ? "selected" : ""}>${esc(propIcon(a))} ${esc(propShort(a))}</option>`)
    .join("");
  sel.onchange = async () => {
    haptic("light");
    state.selectedAptId = Number(sel.value);
    state.historyMonth = "";
    const root = document.getElementById("content");
    showLoading(root, "Загружаю данные объекта...");
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
      { id: "apartments", ico: "🏘", label: "Недвижимость" },
      { id: "history", ico: "📜", label: "История" },
      { id: "families", ico: "👨‍👩‍👧‍👦", label: "Семья" },
    ];
  }
  return [
    { id: "dashboard", ico: "📊", label: "Обзор" },
    { id: "submit", ico: "💧", label: "Подать" },
    { id: "apartments", ico: "🏘", label: "Недвижимость" },
    { id: "families", ico: "👨‍👩‍👧‍👦", label: "Семьи" },
    { id: "admin", ico: "🔧", label: "Управление" },
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
    btn.onclick = async () => {
      if (state.activeTab === btn.dataset.tab) return;
      haptic("light");
      state.activeTab = btn.dataset.tab;
      renderNav();
      await renderTab();
    };
  });
}

async function renderTab() {
  renderNav();
  const root = document.getElementById("content");
  try {
    if (state.activeTab === "dashboard") return renderDashboard(root);
    if (state.activeTab === "submit") return renderSubmit(root);
    if (state.activeTab === "history") { showLoading(root, "Загружаю историю..."); return await renderHistory(root); }
    if (state.activeTab === "photo") return renderPhoto(root);
    if (state.activeTab === "apartments") return renderApartments(root);
    if (state.activeTab === "families") { showLoading(root, "Загружаю данные семьи..."); return await renderFamilies(root); }
    if (state.activeTab === "admin") { showLoading(root, "Загружаю панель управления..."); return await renderAdmin(root); }
  } catch (err) {
    root.innerHTML = `<div class="card center"><p class="muted">${esc(err.message)}</p></div>`;
  }
}

// ── Dashboard (Обзор) ─────────────────────────────────────────────────
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
        <h3>Объекты недвижимости не найдены</h3>
        <p class="muted">Добавьте первый объект (квартиру, дом или дачу), чтобы начать учёт.</p>
        ${canCreate ? `<button class="btn mt-8" id="go-create-apt">➕ Добавить объект</button>` : ""}
      </div>`;
    document.getElementById("go-create-apt")?.addEventListener("click", () => { state.activeTab = "apartments"; renderTab(); });
    return;
  }

  const { apartment: apt, billing, tenants } = state.aptDetail;
  const groups = getEnabledGroups(apt);
  const rows = Object.values(billing.meters).map(renderMeterRow).join("");

  const statBoxes = [];
  if (groups.includes("water")) {
    statBoxes.push(`<div class="stat-box"><div class="label">💧 Вода</div><div class="val">${billing.water_total} ₽</div></div>`);
  }
  if (groups.includes("electricity")) {
    statBoxes.push(`<div class="stat-box"><div class="label">⚡ Свет</div><div class="val">${billing.electricity_total} ₽</div></div>`);
  }
  if (groups.includes("gas")) {
    statBoxes.push(`<div class="stat-box"><div class="label">🔥 Газ</div><div class="val">${billing.gas_total} ₽</div></div>`);
  }
  statBoxes.push(`<div class="stat-box"><div class="label">💰 Итого</div><div class="val">${billing.grand_total} ₽</div></div>`);
  const statGridClass = statBoxes.length === 3 ? "grid-3" : "grid-2";

  const quickButtons = [];
  if (groups.includes("water")) quickButtons.push(`<button class="btn" id="quick-water">💧 Подать воду</button>`);
  if (groups.includes("electricity")) quickButtons.push(`<button class="btn" id="quick-elec">⚡ Подать свет</button>`);
  if (groups.includes("gas")) quickButtons.push(`<button class="btn" id="quick-gas">🔥 Подать газ</button>`);
  const quickGridClass = quickButtons.length === 3 ? "grid-3" : quickButtons.length === 2 ? "grid-2" : "";

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>${esc(propIcon(apt))} ${esc(propTitle(apt))}</h2>
        <span class="pill ${apt.has_tenant ? "ok" : ""}">${apt.has_tenant ? "👤 Сдан" : "Свободен"}</span>
      </div>
      <div class="small muted">📄 Договор: ${esc(apt.agreement_no || "—")} · ${fmtDate(apt.agreement_date_start)} — ${fmtDate(apt.agreement_date_finish)}</div>
      ${tenants.length ? `<div class="small muted mt-8">Наниматели: ${tenants.map(t => esc(t.name || t.telegram_id)).join(", ")}</div>` : ""}
      <div class="${statGridClass} mt-12">
        ${statBoxes.join("")}
      </div>
    </div>
    <div class="card">
      <h3>📊 Расход за ${esc(billing.period_label)}</h3>
      ${rows || `<p class="small muted">Нет подключённых счётчиков.</p>`}
    </div>
    ${quickButtons.length ? `<div class="${quickGridClass}">${quickButtons.join("")}</div>` : ""}
  `;

  document.getElementById("quick-water")?.addEventListener("click", () => { state.activeTab = "submit"; state.submitSubtab = "water"; renderTab(); });
  document.getElementById("quick-elec")?.addEventListener("click", () => { state.activeTab = "submit"; state.submitSubtab = "electricity"; renderTab(); });
  document.getElementById("quick-gas")?.addEventListener("click", () => { state.activeTab = "submit"; state.submitSubtab = "gas"; renderTab(); });
}

async function doExport(format, aptId) {
  try {
    showToast("Формирую файл...");
    await api("/api/export", { method: "POST", body: { format, apartment_id: aptId } });
    showToast("✅ Файл отправлен в чат бота!");
  } catch (err) { showToast("❌ " + err.message, "error"); }
}

function renderExportCardHtml(apt) {
  const shortLabel = apt ? propShort(apt) : "";
  return `
    <div class="card">
      <h3>📤 Быстрая выгрузка в чат</h3>
      ${apt ? `
        <div class="small muted">Текущий объект: ${esc(shortLabel)}</div>
        <div class="grid-2 mt-8">
          <button class="btn btn-secondary" id="exp-xlsx-one">📊 Excel (этот объект)</button>
          <button class="btn btn-secondary" id="exp-pdf-one">📄 PDF (этот объект)</button>
        </div>` : ""}
      <div class="grid-2 mt-8">
        <button class="btn btn-secondary" id="exp-xlsx-all">📦 Excel (все объекты)</button>
        <button class="btn btn-secondary" id="exp-pdf-all">📦 PDF (все объекты)</button>
      </div>
    </div>`;
}

function bindExportButtons(apt) {
  if (apt) {
    document.getElementById("exp-xlsx-one")?.addEventListener("click", () => doExport("excel", apt.id));
    document.getElementById("exp-pdf-one")?.addEventListener("click", () => doExport("pdf", apt.id));
  }
  document.getElementById("exp-xlsx-all")?.addEventListener("click", () => doExport("excel", null));
  document.getElementById("exp-pdf-all")?.addEventListener("click", () => doExport("pdf", null));
}

// ── Submit Readings (Подать) ──────────────────────────────────────────
function fmtPrevLabel(val, unit, tariff) {
  const p = val === null || val === undefined ? "не задано" : `<b>${val}</b> ${unit}`;
  const t = tariff === null || tariff === undefined ? "тариф не задан" : `тариф: ${tariff} ₽`;
  return `предыдущее: ${p}, ${t}`;
}

function renderSubmit(root) {
  if (!state.aptDetail) {
    root.innerHTML = `<div class="card center muted">Сначала выберите или добавьте объект недвижимости.</div>`;
    return;
  }
  const { apartment: apt, prev_readings: prev, tariffs } = state.aptDetail;
  const enabled = getEnabledMeters(apt);
  const groups = getEnabledGroups(apt);
  if (!groups.length) {
    root.innerHTML = `<div class="card center muted">Для этого объекта не выбрано ни одного счётчика.</div>`;
    return;
  }
  if (!groups.includes(state.submitSubtab)) {
    state.submitSubtab = groups[0];
  }

  const groupDefs = {
    water: { label: "💧 Водомеры", btn: "✅ Сохранить показания воды", meters: METER_META.filter(m => m.group === "water" && enabled.includes(m.id)) },
    electricity: { label: "⚡ Электричество", btn: "✅ Сохранить электричество", meters: METER_META.filter(m => m.group === "electricity" && enabled.includes(m.id)) },
    gas: { label: "🔥 Газ", btn: "✅ Сохранить показания газа", meters: METER_META.filter(m => m.group === "gas" && enabled.includes(m.id)) },
  };

  const activeGroup = groupDefs[state.submitSubtab];
  const subtabsHtml = groups.length > 1
    ? `<div class="subtabs">${groups.map(g => `<button class="subtab ${state.submitSubtab === g ? "active" : ""}" data-sub="${g}">${groupDefs[g].label}</button>`).join("")}</div>`
    : "";

  const fieldsHtml = activeGroup.meters.map(m => `
    <div class="field">
      <label>${esc(m.label)} (${fmtPrevLabel(prev[m.id], m.unit, tariffs[m.id])})</label>
      <input type="number" inputmode="numeric" min="0" step="1" id="in-${m.id}" placeholder="${prev[m.id] ?? ""}" />
    </div>
  `).join("");

  root.innerHTML = `
    <div class="card">
      <h2>Подача показаний — ${esc(propShort(apt))}</h2>
      ${subtabsHtml}
      ${fieldsHtml}
      <div id="calc-preview" class="preview-box muted">Введите целые значения для предрасчёта стоимости.</div>
      <button class="btn" id="btn-submit-group">${activeGroup.btn}</button>
    </div>
    ${state.user.role === "tenant" ? "" : `<div class="card"><button class="btn btn-secondary" id="open-photo-btn">📷 Отправить фото счётчиков</button></div>`}
  `;

  root.querySelectorAll("[data-sub]").forEach(btn => {
    btn.onclick = () => { state.submitSubtab = btn.dataset.sub; renderSubmit(root); };
  });
  document.getElementById("open-photo-btn")?.addEventListener("click", () => { state.activeTab = "photo"; renderTab(); });

  const updatePreview = () => {
    const box = document.getElementById("calc-preview");
    const entered = [];
    for (const m of activeGroup.meters) {
      const raw = document.getElementById(`in-${m.id}`).value.trim();
      if (raw === "") continue;
      if (!/^\d+$/.test(raw)) {
        box.textContent = "Допускаются только целые неотрицательные числа.";
        return;
      }
      entered.push({ m, val: parseInt(raw, 10) });
    }
    if (!entered.length) {
      box.textContent = "Введите целые значения для предрасчёта стоимости.";
      return;
    }

    const parts = [];
    let totalCost = 0;
    let missingBase = false;
    let missingTariff = false;

    for (const { m, val } of entered) {
      const pv = prev[m.id];
      const tf = tariffs[m.id];
      if (pv === null || pv === undefined) {
        missingBase = true;
        parts.push(`${m.plain}: <b>${val} ${m.unit}</b> (нет начального)`);
        continue;
      }
      const diff = Math.max(0, val - pv);
      parts.push(`${m.plain}: <b>${diff} ${m.unit}</b>`);
      if (tf === null || tf === undefined) {
        missingTariff = true;
      } else {
        totalCost += Math.round(diff * tf);
      }
    }

    const costLine = missingBase
      ? "⚠️ Для части счётчиков не задано начальное показание"
      : missingTariff
        ? "⚠️ Для части счётчиков не задан тариф"
        : `Ориентировочно: <b>${totalCost} ₽</b>`;
    box.innerHTML = `Расход — ${parts.join(" · ")}<br>${costLine}`;
  };

  activeGroup.meters.forEach(m => {
    document.getElementById(`in-${m.id}`).oninput = updatePreview;
  });

  document.getElementById("btn-submit-group").onclick = () => submitMeterGroup(apt.id, state.submitSubtab, activeGroup.meters, false);
}

async function submitMeterGroup(aptId, groupName, groupMeters, force) {
  const payload = { force };
  let count = 0;
  for (const m of groupMeters) {
    const raw = document.getElementById(`in-${m.id}`).value.trim();
    if (raw === "") continue;
    if (!/^\d+$/.test(raw)) {
      return showToast(`Некорректное значение для ${m.plain}: введите целое число`, "error");
    }
    payload[m.id] = parseInt(raw, 10);
    count++;
  }
  if (!count) return showToast("Введите хотя бы одно показание счётчика", "error");

  const btn = document.getElementById("btn-submit-group");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/apartments/${aptId}/readings/${groupName}`, { method: "POST", body: payload });
    if (res.needs_confirm) {
      if (btn) btn.disabled = false;
      return openModal("⚠️ Значение меньше предыдущего", `<p>${esc(res.warning)}</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        { label: "Сохранить", cls: "btn-danger", onClick: () => { submitMeterGroup(aptId, groupName, groupMeters, true); } },
      ]);
    }
    const costMap = {
      water: `Вода сохранена (${res.billing.water_total} ₽)`,
      electricity: `Электричество сохранено (${res.billing.electricity_total} ₽)`,
      gas: `Газ сохранён (${res.billing.gas_total} ₽)`,
    };
    showToast(`✅ ${costMap[groupName] || "Показания сохранены"}`);
    await loadApartmentDetail(aptId);
    state.activeTab = "dashboard";
    await renderTab();
  } catch (err) {
    showToast("❌ " + err.message, "error");
    if (btn) btn.disabled = false;
  }
}

// ── History (История) ─────────────────────────────────────────────────
async function renderHistory(root) {
  if (!state.selectedAptId) { root.innerHTML = `<div class="card center muted">Нет доступных объектов недвижимости.</div>`; return; }
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

  const billParts = [];
  if (data.billing.water_total || Object.keys(data.billing.meters).some(k => k.includes("water"))) {
    billParts.push(`вода <b>${data.billing.water_total} ₽</b>`);
  }
  if (data.billing.electricity_total || Object.keys(data.billing.meters).some(k => ["t1", "t2", "t3"].includes(k))) {
    billParts.push(`свет <b>${data.billing.electricity_total} ₽</b>`);
  }
  if (data.billing.gas_total || Object.keys(data.billing.meters).includes("gas")) {
    billParts.push(`газ <b>${data.billing.gas_total} ₽</b>`);
  }
  billParts.push(`всего <b>${data.billing.grand_total} ₽</b>`);

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>📜 История — ${esc(propShort(state.aptDetail?.apartment))}</h2>
        <select id="month-filter" class="apt-select">${monthOptions}</select>
      </div>
      ${state.historyMonth ? `<div class="preview-box mt-8">За ${esc(data.billing.period_label)}: ${billParts.join(" · ")}</div>` : ""}
      <div class="mt-8">${listHtml}</div>
    </div>
  `;

  document.getElementById("month-filter").onchange = async (e) => {
    state.historyMonth = e.target.value;
    showLoading(root, "Загружаю историю...");
    await renderHistory(root);
  };

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
            const raw = document.getElementById("corr-val").value.trim();
            if (!/^\d+$/.test(raw)) { showToast("Введите целое число >= 0", "error"); return false; }
            const val = parseInt(raw, 10);
            try {
              await api(`/api/readings/${id}`, { method: "PUT", body: { value: val } });
              showToast("✅ Показание исправлено");
              await loadApartmentDetail(state.selectedAptId);
              await renderHistory(root);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
      ]);
    };
  });
}

// ── Photo (Фото) ──────────────────────────────────────────────────────
function renderPhoto(root) {
  if (!state.aptDetail) { root.innerHTML = `<div class="card center muted">Нет доступных объектов недвижимости.</div>`; return; }
  const apt = state.aptDetail.apartment;
  root.innerHTML = `
    <div class="card">
      <h2>📷 Фото счётчиков — ${esc(propShort(apt))}</h2>
      <p class="small muted">Фото будет переслано владельцам объекта в Telegram и сохранено на сервере на 30 дней.</p>
      <div class="field mt-8"><label>Выберите или сделайте фото (до 15 МБ)</label><input type="file" id="photo-file" accept="image/*" /></div>
      <div class="field"><label>Комментарий (необязательно)</label><input type="text" id="photo-comment" placeholder="Например: счётчики воды за ноябрь" /></div>
      <button class="btn" id="btn-send-photo">📤 Отправить владельцам</button>
    </div>
  `;
  document.getElementById("btn-send-photo").onclick = async () => {
    const fileInput = document.getElementById("photo-file");
    if (!fileInput.files.length) return showToast("Выберите фотографию", "error");
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
    } catch (err) { showToast("❌ " + err.message, "error"); }
    finally { btn.disabled = false; }
  };
}

// ── Properties (Недвижимость) ─────────────────────────────────────────
function openCreatePropertyModal() {
  let chosenType = "apartment";
  const meterChecksHtml = METER_META.map(m => `
    <label class="meter-check">
      <input type="checkbox" class="new-meter-cb" value="${m.id}" ${DEFAULT_NEW_METERS.includes(m.id) ? "checked" : ""} />
      <span>${esc(m.label)}</span>
    </label>
  `).join("");

  openModal("➕ Новый объект недвижимости", `
    <div class="field">
      <label>Тип недвижимости</label>
      <div class="type-picker" id="new-type-picker">
        ${PROPERTY_TYPES.map(t => `
          <button type="button" class="type-btn ${t.id === chosenType ? "active" : ""}" data-ptype="${t.id}">
            ${t.icon} ${t.label}
          </button>
        `).join("")}
      </div>
    </div>
    <div class="field">
      <label id="new-prop-num-label">${getPropertyMeta(chosenType).numLabel}</label>
      <input type="text" id="new-apt-num" maxlength="50" placeholder="${getPropertyMeta(chosenType).placeholder}" />
    </div>
    <div class="field">
      <label>Счётчики на объекте</label>
      <div class="meter-check-grid">${meterChecksHtml}</div>
    </div>
  `, [
    { label: "Отмена", cls: "btn-secondary" },
    {
      label: "Создать",
      onClick: async () => {
        const number = document.getElementById("new-apt-num").value.trim();
        if (!number) { showToast("Укажите номер или название объекта", "error"); return false; }
        const enabledMeters = Array.from(document.querySelectorAll(".new-meter-cb:checked")).map(cb => cb.value);
        if (!enabledMeters.length) { showToast("Выберите хотя бы один счётчик", "error"); return false; }
        try {
          const res = await api("/api/apartments", {
            method: "POST",
            body: { number, property_type: chosenType, enabled_meters: enabledMeters },
          });
          state.selectedAptId = res.apartment.id;
          showToast(`✅ Создано: ${propTitle(res.apartment)}`);
          await loadBootstrap();
        } catch (err) { showToast("❌ " + err.message, "error"); return false; }
      },
    },
  ]);

  document.querySelectorAll("#new-type-picker .type-btn").forEach(btn => {
    btn.onclick = () => {
      chosenType = btn.dataset.ptype;
      document.querySelectorAll("#new-type-picker .type-btn").forEach(b => b.classList.toggle("active", b === btn));
      const meta = getPropertyMeta(chosenType);
      document.getElementById("new-prop-num-label").textContent = meta.numLabel;
      document.getElementById("new-apt-num").placeholder = meta.placeholder;
    };
  });
}

function renderApartments(root) {
  const apt = state.aptDetail?.apartment;
  const tariffs = state.aptDetail?.tariffs || {};
  const initR = state.aptDetail?.initial_readings || {};
  const tenants = state.aptDetail?.tenants || [];
  const enabled = apt ? getEnabledMeters(apt) : [];

  const aptListChips = state.apartments.map(a => `
    <button class="btn btn-sm ${a.id === state.selectedAptId ? "" : "btn-secondary"}" data-apt="${a.id}">
      ${esc(propIcon(a))} ${esc(propShort(a))}${a.has_tenant ? " 👤" : ""}
    </button>`).join("");

  let bodyHtml = "";
  if (apt) {
    if (state.manageSubtab === "settings") {
      const curMeta = getPropertyMeta(apt.property_type);
      bodyHtml = `
        <div class="field">
          <label>Тип недвижимости</label>
          <div class="type-picker" id="edit-type-picker">
            ${PROPERTY_TYPES.map(t => `
              <button type="button" class="type-btn ${t.id === apt.property_type ? "active" : ""}" data-edit-ptype="${t.id}">
                ${t.icon} ${t.label}
              </button>
            `).join("")}
          </div>
        </div>
        <div class="field">
          <label id="edit-prop-num-label">${curMeta.numLabel}</label>
          <input type="text" id="s-number" maxlength="50" value="${esc(apt.number)}" placeholder="${curMeta.placeholder}" />
        </div>
        <div class="field">
          <label>Подключённые счётчики</label>
          <div class="meter-check-grid">
            ${METER_META.map(m => `
              <label class="meter-check">
                <input type="checkbox" class="edit-meter-cb" value="${m.id}" ${enabled.includes(m.id) ? "checked" : ""} />
                <span>${esc(m.label)}</span>
              </label>
            `).join("")}
          </div>
        </div>
        <div id="settings-days-wrap" class="grid-3">
          <div class="field" data-day-group="water"><label>📅 День воды (1–31)</label><input type="number" min="1" max="31" id="s-wday" value="${apt.water_submission_day ?? ""}" /></div>
          <div class="field" data-day-group="electricity"><label>📅 День света (1–31)</label><input type="number" min="1" max="31" id="s-eday" value="${apt.electricity_submission_day ?? ""}" /></div>
          <div class="field" data-day-group="gas"><label>📅 День газа (1–31)</label><input type="number" min="1" max="31" id="s-gday" value="${apt.gas_submission_day ?? ""}" /></div>
        </div>
        <div class="grid-2" id="settings-tariffs-wrap">
          ${METER_META.map(m => `
            <div class="field" data-tariff-meter="${m.id}">
              <label>${esc(m.tariffLabel)}</label>
              <input type="text" inputmode="decimal" id="t-${m.id}" value="${tariffs[m.id] ?? ""}" />
            </div>
          `).join("")}
        </div>
        <div class="row-gap">
          <button class="btn" id="btn-save-settings">💾 Сохранить настройки и тарифы</button>
          <button class="btn btn-secondary" id="btn-tariff-hist">📋 История тарифов</button>
        </div>`;
    } else if (state.manageSubtab === "initial") {
      const enabledMeta = METER_META.filter(m => enabled.includes(m.id));
      bodyHtml = `
        <p class="small muted">Оставьте поле пустым, если начальное значение ещё не задано (укажите 0, если счётчик новый).</p>
        <div class="grid-2 mt-8">
          ${enabledMeta.map(m => `
            <div class="field">
              <label>${esc(m.label)} (${esc(m.unit)})</label>
              <input type="number" min="0" id="i-${m.id}" placeholder="Не задано" value="${initR[m.id] ?? ""}" />
            </div>
          `).join("")}
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
      <div class="row-between"><h2>🏘 Недвижимость</h2><button class="btn btn-sm" id="btn-new-apt">➕ Добавить</button></div>
      <div class="row-gap mt-8">${aptListChips || '<span class="muted small">Пока нет объектов недвижимости</span>'}</div>
    </div>
    ${apt ? `
      <div class="card">
        <h3>${esc(propIcon(apt))} ${esc(propTitle(apt))}</h3>
        <div class="subtabs">
          <button class="subtab ${state.manageSubtab === "settings" ? "active" : ""}" data-m="settings">⚙️ Настройки</button>
          <button class="subtab ${state.manageSubtab === "initial" ? "active" : ""}" data-m="initial">📏 Начальные</button>
          <button class="subtab ${state.manageSubtab === "contract" ? "active" : ""}" data-m="contract">📋 Договор</button>
        </div>
        ${bodyHtml}
      </div>` : ""}
    ${state.user.role === "landlord" && apt ? renderExportCardHtml(apt) : ""}
  `;

  if (state.user.role === "landlord" && apt) {
    bindExportButtons(apt);
  }

  root.querySelectorAll("[data-apt]").forEach(btn => {
    btn.onclick = async () => {
      haptic("light");
      state.selectedAptId = Number(btn.dataset.apt);
      renderAptSelector();
      showLoading(root, "Загружаю объект...");
      const ok = await loadApartmentDetail(state.selectedAptId);
      if (ok) renderApartments(root);
    };
  });
  root.querySelectorAll("[data-m]").forEach(btn => {
    btn.onclick = () => { state.manageSubtab = btn.dataset.m; renderApartments(root); };
  });

  document.getElementById("btn-new-apt").onclick = openCreatePropertyModal;

  if (!apt) return;

  if (state.manageSubtab === "settings") {
    let editPropType = apt.property_type || "apartment";

    const syncSettingsVisibility = () => {
      const checked = new Set(Array.from(document.querySelectorAll(".edit-meter-cb:checked")).map(cb => cb.value));
      const hasW = checked.has("cold_water") || checked.has("hot_water");
      const hasE = checked.has("t1") || checked.has("t2") || checked.has("t3");
      const hasG = checked.has("gas");

      document.querySelector('[data-day-group="water"]')?.classList.toggle("hidden", !hasW);
      document.querySelector('[data-day-group="electricity"]')?.classList.toggle("hidden", !hasE);
      document.querySelector('[data-day-group="gas"]')?.classList.toggle("hidden", !hasG);

      document.querySelectorAll("[data-tariff-meter]").forEach(el => {
        el.classList.toggle("hidden", !checked.has(el.dataset.tariffMeter));
      });
    };

    syncSettingsVisibility();
    document.querySelectorAll(".edit-meter-cb").forEach(cb => {
      cb.onchange = syncSettingsVisibility;
    });

    document.querySelectorAll("#edit-type-picker .type-btn").forEach(btn => {
      btn.onclick = () => {
        editPropType = btn.dataset.editPtype;
        document.querySelectorAll("#edit-type-picker .type-btn").forEach(b => b.classList.toggle("active", b === btn));
        const meta = getPropertyMeta(editPropType);
        document.getElementById("edit-prop-num-label").textContent = meta.numLabel;
        document.getElementById("s-number").placeholder = meta.placeholder;
      };
    });

    document.getElementById("btn-save-settings")?.addEventListener("click", async () => {
      const number = document.getElementById("s-number").value.trim();
      if (!number) return showToast("Укажите номер или название объекта", "error");
      const enabledMeters = Array.from(document.querySelectorAll(".edit-meter-cb:checked")).map(cb => cb.value);
      if (!enabledMeters.length) return showToast("Выберите хотя бы один счётчик", "error");

      const wday = document.getElementById("s-wday").value.trim();
      const eday = document.getElementById("s-eday").value.trim();
      const gday = document.getElementById("s-gday").value.trim();
      const tariffsPayload = {};
      for (const m of METER_META) {
        if (!enabledMeters.includes(m.id)) continue;
        const val = document.getElementById(`t-${m.id}`)?.value.trim();
        if (val) tariffsPayload[m.id] = val;
      }

      try {
        await api(`/api/apartments/${apt.id}/settings`, {
          method: "PUT",
          body: {
            number,
            property_type: editPropType,
            enabled_meters: enabledMeters,
            water_submission_day: wday ? Number(wday) : null,
            electricity_submission_day: eday ? Number(eday) : null,
            gas_submission_day: gday ? Number(gday) : null,
            tariffs: tariffsPayload,
          },
        });
        showToast("✅ Настройки сохранены");
        const me = await api("/api/me");
        state.apartments = me.apartments;
        renderAptSelector();
        await loadApartmentDetail(apt.id);
        renderApartments(root);
      } catch (err) { showToast("❌ " + err.message, "error"); }
    });

    document.getElementById("btn-tariff-hist")?.addEventListener("click", async () => {
      try {
        const res = await api(`/api/apartments/${apt.id}/tariffs/history`);
        const html = res.items.length
          ? res.items.map(i => `<div class="table-row"><span>${esc(i.label)} · ${esc(i.effective_from)}</span><b>${i.rate} ₽</b></div>`).join("")
          : `<p class="muted">История тарифов пуста.</p>`;
        openModal(`История тарифов — ${propShort(apt)}`, `<div style="max-height:55vh;overflow:auto">${html}</div>`, [{ label: "Закрыть", cls: "btn-secondary" }]);
      } catch (err) { showToast("❌ " + err.message, "error"); }
    });
  }

  document.getElementById("btn-save-initial")?.addEventListener("click", async () => {
    const body = {};
    for (const m of METER_META) {
      const el = document.getElementById(`i-${m.id}`);
      if (!el) continue;
      const v = el.value.trim();
      if (v === "") {
        body[m.id] = null;
      } else if (!/^\d+$/.test(v)) {
        return showToast(`Некорректное начальное показание для ${m.plain}`, "error");
      } else {
        body[m.id] = parseInt(v, 10);
      }
    }
    try {
      await api(`/api/apartments/${apt.id}/initial-readings`, { method: "PUT", body });
      showToast("✅ Начальные показания сохранены");
      await loadApartmentDetail(apt.id);
    } catch (err) { showToast("❌ " + err.message, "error"); }
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
    } catch (err) { showToast("❌ " + err.message, "error"); }
  });

  document.getElementById("btn-del-contract")?.addEventListener("click", () => {
    openModal("🗑 Удалить договор?", `<p>Очистить данные договора для объекта «${esc(propShort(apt))}»?</p>`, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Удалить", cls: "btn-danger",
        onClick: async () => {
          try {
            await api(`/api/apartments/${apt.id}/contract`, { method: "DELETE" });
            showToast("✅ Договор удалён");
            await loadApartmentDetail(apt.id);
            renderApartments(root);
          } catch (err) { showToast("❌ " + err.message, "error"); return false; }
        },
      },
    ]);
  });

  document.getElementById("btn-add-tenant")?.addEventListener("click", () => {
    openModal("👤 Добавить нанимателя", `
      <div class="field">
        <label>Telegram ID</label>
        <input type="number" id="nt-id" placeholder="Например: 123456789" />
        <div class="small muted">Telegram ID можно взять в боте <a href="https://t.me/userinfobot" target="_blank" rel="noopener" class="tg-link" data-tg-link="https://t.me/userinfobot">@userinfobot</a></div>
      </div>
      <div class="field"><label>Имя</label><input type="text" id="nt-name" placeholder="Введите имя" /></div>
      <div class="field"><label>Телефон (необязательно)</label><input type="tel" inputmode="tel" id="nt-phone" class="phone-mask" placeholder="+7-___-___-____" /></div>
      <p class="small muted">Бот сможет отправить приглашение только если наниматель ранее уже нажимал /start.</p>
    `, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Добавить",
        onClick: async () => {
          const tgId = Number(document.getElementById("nt-id").value);
          const name = document.getElementById("nt-name").value.trim();
          if (!tgId || tgId <= 0) { showToast("Введите корректный Telegram ID", "error"); return false; }
          if (!name) { showToast("Введите имя нанимателя", "error"); return false; }
          const ph = readMaskedPhone("nt-phone");
          if (!ph.ok) { showToast(ph.error, "error"); return false; }
          return await submitAddTenant(apt.id, {
            telegram_id: tgId,
            name,
            phone: ph.value,
            force_no_contract: false,
          });
        },
      },
    ]);
  });

  root.querySelectorAll(".del-tenant-btn").forEach(btn => {
    btn.onclick = () => {
      const tid = Number(btn.dataset.tid);
      openModal("Удалить нанимателя?", `<p>Отозвать доступ у ID ${tid} к объекту «${esc(propShort(apt))}»?</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            try {
              await api(`/api/apartments/${apt.id}/tenants/${tid}`, { method: "DELETE" });
              showToast("✅ Наниматель удалён");
              await loadApartmentDetail(apt.id);
              renderApartments(root);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
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
      setTimeout(() => {
        openModal("⚠️ Договор не заполнен", `<p>${esc(res.warning)}</p>`, [
          { label: "Отмена", cls: "btn-secondary" },
          { label: "Добавить", onClick: () => submitAddTenant(aptId, { ...payload, force_no_contract: true }) },
        ]);
      }, 0);
      return true;
    }
    showToast(
      res.notified
        ? "✅ Наниматель добавлен, приглашение отправлено"
        : "✅ Наниматель добавлен (попросите его нажать /start в боте)"
    );
    await loadApartmentDetail(aptId);
    renderTab();
    return true;
  } catch (err) {
    showToast("❌ " + err.message, "error");
    return false;
  }
}

// ── Families (Семья / Семьи) ──────────────────────────────────────────
async function renderFamilies(root) {
  const data = await api("/api/families");
  root.innerHTML = `
    <div class="card"><div class="row-between"><h2>👨‍👩‍👧‍👦 ${data.can_create ? "Семьи" : "Семья"}</h2>${data.can_create ? `<button class="btn btn-sm" id="btn-new-fam">➕ Создать</button>` : ""}</div></div>
    ${data.families.map(f => `
      <div class="card">
        <div class="row-between"><h3>${esc(f.name)}</h3><span class="pill">🏘 ${f.apartments_count}</span></div>
        <div class="mt-8">
          ${f.owners.length ? f.owners.map(o => `
            <div class="table-row">
              <div>
                <div class="bold">${esc(o.name || "—")}</div>
                <div class="small muted">ID: ${o.telegram_id}${o.phone ? " · " + esc(o.phone) : ""}</div>
              </div>
              <div class="row-nowrap">
                <button class="btn btn-secondary btn-sm edit-owner-btn" data-fid="${f.id}" data-oid="${o.telegram_id}" data-oname="${esc(o.name || "")}" data-ophone="${esc(o.phone || "")}" title="Редактировать">✏️</button>
                <button class="btn btn-danger btn-sm del-owner-btn" data-fid="${f.id}" data-oid="${o.telegram_id}" data-oname="${esc(o.name || o.telegram_id)}" title="Удалить">✕</button>
              </div>
            </div>`).join("") : `<p class="small muted">Владельцев нет.</p>`}
        </div>
        <div class="row-gap mt-12">
          <button class="btn btn-secondary btn-sm add-owner-btn" data-fid="${f.id}">🔑 + Владелец</button>
          <button class="btn btn-secondary btn-sm ren-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}">✏️ Имя семьи</button>
          ${data.can_create ? `<button class="btn btn-danger btn-sm del-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}">🗑</button>` : ""}
        </div>
      </div>`).join("")}
  `;

  document.getElementById("btn-new-fam")?.addEventListener("click", () => {
    openModal("➕ Новая семья", `<div class="field"><label>Название</label><input type="text" id="nf-name" placeholder="Например: Семья Ивановых" /></div>`, [
      { label: "Отмена", cls: "btn-secondary" },
      {
        label: "Создать",
        onClick: async () => {
          const name = document.getElementById("nf-name").value.trim();
          if (!name) { showToast("Введите название семьи", "error"); return false; }
          try {
            await api("/api/families", { method: "POST", body: { name } });
            showToast("✅ Семья создана");
            await renderFamilies(root);
          } catch (err) { showToast("❌ " + err.message, "error"); return false; }
        },
      },
    ]);
  });

  root.querySelectorAll(".ren-fam-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("✏️ Переименовать семью", `<div class="field"><label>Название семьи</label><input type="text" id="rf-name" value="${esc(btn.dataset.name)}" /></div>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Сохранить",
          onClick: async () => {
            const name = document.getElementById("rf-name").value.trim();
            if (!name) { showToast("Название не может быть пустым", "error"); return false; }
            try {
              await api(`/api/families/${btn.dataset.fid}`, { method: "PUT", body: { name } });
              showToast("✅ Переименовано");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".add-owner-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("🔑 Добавить владельца", `
        <div class="field">
          <label>Telegram ID</label>
          <input type="number" id="no-id" placeholder="Например: 123456789" />
          <div class="small muted">Telegram ID можно взять в боте <a href="https://t.me/userinfobot" target="_blank" rel="noopener" class="tg-link" data-tg-link="https://t.me/userinfobot">@userinfobot</a></div>
        </div>
        <div class="field"><label>Имя</label><input type="text" id="no-name" placeholder="Введите имя" /></div>
        <div class="field"><label>Телефон (необязательно)</label><input type="tel" inputmode="tel" id="no-phone" class="phone-mask" placeholder="+7-___-___-____" /></div>
      `, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Добавить",
          onClick: async () => {
            const tgId = Number(document.getElementById("no-id").value);
            const name = document.getElementById("no-name").value.trim();
            if (!tgId || tgId <= 0) { showToast("Введите корректный Telegram ID", "error"); return false; }
            if (!name) { showToast("Введите имя владельца", "error"); return false; }
            const ph = readMaskedPhone("no-phone");
            if (!ph.ok) { showToast(ph.error, "error"); return false; }
            try {
              await api(`/api/families/${btn.dataset.fid}/owners`, {
                method: "POST",
                body: { telegram_id: tgId, name, phone: ph.value },
              });
              showToast("✅ Владелец добавлен");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".edit-owner-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("✏️ Редактировать члена семьи", `
        <div class="field">
          <label>Имя</label>
          <input type="text" id="eo-name" maxlength="120" value="${esc(btn.dataset.oname)}" placeholder="Введите имя" />
        </div>
        <div class="field">
          <label>Телефон (необязательно)</label>
          <input type="tel" inputmode="tel" id="eo-phone" class="phone-mask" value="${esc(btn.dataset.ophone)}" placeholder="+7-___-___-____" />
        </div>
      `, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Сохранить",
          onClick: async () => {
            const name = document.getElementById("eo-name").value.trim();
            if (!name) { showToast("Имя не может быть пустым", "error"); return false; }
            const ph = readMaskedPhone("eo-phone");
            if (!ph.ok) { showToast(ph.error, "error"); return false; }
            try {
              await api(`/api/families/${btn.dataset.fid}/owners/${btn.dataset.oid}`, {
                method: "PUT",
                body: { name, phone: ph.value },
              });
              if (state.user && Number(btn.dataset.oid) === state.user.telegram_id) {
                state.user.name = name;
                updateUserBadge();
              }
              showToast("✅ Данные члена семьи обновлены");
              await renderFamilies(root);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
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
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
      ]);
    };
  });

  root.querySelectorAll(".del-fam-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("🗑 Удалить семью?", `<p>Удалить «${esc(btn.dataset.name)}» вместе со всеми объектами недвижимости, показаниями и привязанными пользователями?</p>`, [
        { label: "Отмена", cls: "btn-secondary" },
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            try {
              await api(`/api/families/${btn.dataset.fid}`, { method: "DELETE" });
              showToast("✅ Семья удалена");
              await loadBootstrap();
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
      ]);
    };
  });
}

// ── Management / Admin (Управление) ───────────────────────────────────
async function renderAdmin(root) {
  const [usersRes, auditRes] = await Promise.all([api("/api/admin/users"), api("/api/admin/audit")]);
  const apt = state.aptDetail?.apartment || null;

  root.innerHTML = `
    <div class="card">
      <h2>🔧 Управление</h2>
      <div class="grid-2 mt-8">
        <button class="btn" id="btn-backup">💾 Бэкап БД в чат</button>
        <button class="btn btn-secondary" id="btn-go-hist">✏️ Корректировка</button>
      </div>
    </div>
    ${renderExportCardHtml(apt)}
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
    } catch (err) { showToast("❌ " + err.message, "error"); }
  };
  document.getElementById("btn-go-hist").onclick = () => { state.activeTab = "history"; renderTab(); };
  bindExportButtons(apt);
}

loadBootstrap();
