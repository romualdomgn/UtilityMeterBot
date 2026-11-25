const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const state = {
  user: null,
  apartments: [],
  selectedAptId: null,
  aptDetail: null,
  aptLoading: false,
  aptLoadError: null,
  activeTab: "dashboard",
  submitSubtab: "water",
  manageSubtab: "settings",
  historyMonth: "",
  aptReqSeq: 0,
  histReqSeq: 0,
  tabRenderSeq: 0,
  reorderMode: false,
  modalBusy: false,
};

const ROLE_RU = { admin: "Администратор", landlord: "Владелец", tenant: "Наниматель", revoked: "Отозван" };
const MONTH_RU = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];
const VERIFY_INTERVALS = [
  { id: "6m", label: "Полгода" },
  { id: "3m", label: "3 месяца" },
  { id: "1m", label: "Месяц" },
  { id: "1w", label: "Неделя" },
];

const PROPERTY_TYPES = [
  { id: "apartment", icon: "🏢", label: "Квартира", numLabel: "Номер квартиры", placeholder: "Например: 371", inLocative: "в квартире" },
  { id: "house", icon: "🏡", label: "Дом", numLabel: "Адрес или номер дома", placeholder: "Например: Лесная, 12", inLocative: "в доме" },
  { id: "dacha", icon: "🌿", label: "Дача", numLabel: "Название или номер участка", placeholder: "Например: СНТ Весна, уч. 45", inLocative: "на даче" },
];

const METER_DEVICE_LABELS = {
  cold_water: "💧 Водомер холодной воды",
  hot_water: "♨️ Водомер горячей воды",
  irrigation: "🌱 Полив",
  t: "⚡ Электрический счётчик",
  gas: "🔥 Газ",
};

function getPhysicalMeterDevices(enabledMeters) {
  const result = [];
  let elecAdded = false;
  for (const mt of enabledMeters) {
    if (mt === "t" || MULTI_ELEC_IDS.includes(mt)) {
      if (elecAdded) continue;
      elecAdded = true;
      result.push("t");
    } else {
      result.push(mt);
    }
  }
  return result;
}

const METER_META = [
  { id: "cold_water", group: "water", label: "💧 ХВС", plain: "ХВС", unit: "м³", tariffLabel: "💧 ХВС (руб/м³)" },
  { id: "hot_water", group: "water", label: "♨️ ГВС", plain: "ГВС", unit: "м³", tariffLabel: "♨️ ГВС (руб/м³)" },
  { id: "irrigation", group: "water", houseOnly: true, label: "🌱 Полив", plain: "Полив", unit: "м³", tariffLabel: "🌱 Полив (руб/м³)" },
  { id: "t", group: "electricity", label: "⚡ Т", plain: "Т", unit: "кВт·ч", tariffLabel: "⚡ Т (руб/кВт·ч)" },
  { id: "t1", group: "electricity", label: "⚡ Т1", plain: "Т1", unit: "кВт·ч", tariffLabel: "⚡ Т1 (руб/кВт·ч)" },
  { id: "t2", group: "electricity", label: "⚡ Т2", plain: "Т2", unit: "кВт·ч", tariffLabel: "⚡ Т2 (руб/кВт·ч)" },
  { id: "t3", group: "electricity", label: "⚡ Т3", plain: "Т3", unit: "кВт·ч", tariffLabel: "⚡ Т3 (руб/кВт·ч)" },
  { id: "gas", group: "gas", label: "🔥 Газ", plain: "Газ", unit: "м³", tariffLabel: "🔥 Газ (руб/м³)" },
];

const WATER_GAS_METERS = METER_META.filter(m => m.group === "water" || m.group === "gas");
const ELECTRICITY_METER_META = METER_META.filter(m => m.group === "electricity");
const MULTI_ELEC_IDS = ["t1", "t2", "t3"];
const DEFAULT_NEW_METERS = ["cold_water", "hot_water", "t"];
const MAX_READING_DIGITS = 12;

function genSubmissionId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `sub_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, s => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[s]));
}

function fmtNum(val) {
  if (val === null || val === undefined || val === "") return "—";
  const num = Number(val);
  if (!Number.isFinite(num)) return String(val);
  const parts = String(num).split(".");
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, "\u00A0");
  return parts.join(".");
}

// HIGH-09: never silently alter decimals, negatives, letters, or >12-digit numbers
function formatIntSpaces(raw) {
  if (raw === null || raw === undefined) return "";
  const str = String(raw);
  if (!str.trim()) return "";
  if (!/^[\d\s\u00A0\u202F]+$/.test(str)) return str;
  let digits = str.replace(/[\s\u00A0\u202F]+/g, "");
  if (!digits || digits.length > MAX_READING_DIGITS) return str;
  digits = digits.replace(/^0+(?=\d)/, "");
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00A0");
}

function parseIntSpaces(raw) {
  const str = String(raw ?? "");
  const cleaned = str.replace(/[\s\u00A0\u202F]+/g, "");
  if (cleaned === "") return { empty: true, ok: true, digits: "", value: null, error: null };
  if (/^-/.test(cleaned)) {
    return { empty: false, ok: false, digits: "", value: null, error: "Отрицательные значения не допускаются." };
  }
  if (/[.,]/.test(cleaned)) {
    return { empty: false, ok: false, digits: "", value: null, error: "Введите целое число без дробной части." };
  }
  if (!/^\d+$/.test(cleaned)) {
    return { empty: false, ok: false, digits: "", value: null, error: "Допускаются только цифры (целые числа)." };
  }
  const norm = cleaned.replace(/^0+(?=\d)/, "");
  if (norm.length > MAX_READING_DIGITS) {
    return { empty: false, ok: false, digits: "", value: null, error: `Слишком большое число (максимум ${MAX_READING_DIGITS} цифр).` };
  }
  const val = parseInt(norm, 10);
  if (!Number.isSafeInteger(val)) {
    return { empty: false, ok: false, digits: "", value: null, error: "Число превышает допустимый предел." };
  }
  return { empty: false, ok: true, digits: norm, value: val, error: null };
}

function attachIntMask(input) {
  if (!input || input.dataset.intMaskBound === "1") return;
  input.dataset.intMaskBound = "1";

  const setCaretByDigitIndex = (formatted, digitIdx) => {
    let seen = 0;
    let pos = formatted.length;
    if (digitIdx <= 0) {
      pos = 0;
    } else {
      for (let i = 0; i < formatted.length; i++) {
        if (/\d/.test(formatted[i])) {
          seen++;
          if (seen === digitIdx) {
            pos = i + 1;
            break;
          }
        }
      }
    }
    try { input.setSelectionRange(pos, pos); } catch (_) {}
  };

  if (input.value) {
    input.value = formatIntSpaces(input.value);
  }

  input.addEventListener("keydown", (e) => {
    const start = input.selectionStart ?? 0;
    const end = input.selectionEnd ?? 0;
    if (start !== end) return;

    if (e.key === "Backspace" && start > 0 && input.value[start - 1] === "\u00A0") {
      e.preventDefault();
      const digitsBefore = input.value.slice(0, start - 1).replace(/\D/g, "").length;
      const before = input.value.slice(0, Math.max(0, start - 2));
      const after = input.value.slice(start);
      const formatted = formatIntSpaces(before + after);
      input.value = formatted;
      setCaretByDigitIndex(formatted, Math.max(0, digitsBefore - 1));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    } else if (e.key === "Delete" && start < input.value.length && input.value[start] === "\u00A0") {
      e.preventDefault();
      const digitsBefore = input.value.slice(0, start).replace(/\D/g, "").length;
      const before = input.value.slice(0, start);
      const after = input.value.slice(start + 2);
      const formatted = formatIntSpaces(before + after);
      input.value = formatted;
      setCaretByDigitIndex(formatted, digitsBefore);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });

  input.addEventListener("input", () => {
    const raw = input.value;
    const parsed = parseIntSpaces(raw);
    input.classList.toggle("input-error", !parsed.empty && !parsed.ok);
    if (!parsed.empty && parsed.ok) {
      const caret = input.selectionStart ?? raw.length;
      const digitsBefore = raw.slice(0, caret).replace(/\D/g, "").length;
      const formatted = formatIntSpaces(raw);
      if (formatted !== raw) {
        input.value = formatted;
        setCaretByDigitIndex(formatted, digitsBefore);
      }
    }
  });
}

function hasContract(apt) {
  if (!apt) return false;
  return Boolean(
    String(apt.agreement_no || "").trim() ||
    apt.agreement_date_start ||
    apt.agreement_date_finish
  );
}

function isPropertyRented(apt) {
  if (!apt) return false;
  return Boolean(apt.is_rented || apt.has_tenant || hasContract(apt));
}

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return d ? `${d}.${m}.${y}` : iso;
}

function isoToDisplayDate(iso) {
  if (!iso) return "";
  const m = String(iso).trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : String(iso);
}

function formatDateMask(raw) {
  const trimmed = String(raw || "").trim();
  const isoMatch = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (isoMatch) {
    return `${isoMatch[3]}/${isoMatch[2]}/${isoMatch[1]}`;
  }
  const digits = trimmed.replace(/\D/g, "").slice(0, 8);
  if (digits.length <= 2) return digits;
  if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
  return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4, 8)}`;
}

function parseDisplayDate(raw) {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return { ok: true, iso: null, display: null };
  const m = trimmed.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
  if (!m) {
    return { ok: false, error: "Укажите дату в формате ДД/ММ/ГГГГ (например, 25/11/2026)" };
  }
  const day = Number(m[1]);
  const month = Number(m[2]);
  const year = Number(m[3]);
  if (year < 1900 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31) {
    return { ok: false, error: `Некорректная дата: ${trimmed}` };
  }
  const dt = new Date(year, month - 1, day);
  if (dt.getFullYear() !== year || dt.getMonth() !== month - 1 || dt.getDate() !== day) {
    return { ok: false, error: `Такой даты не существует: ${trimmed}` };
  }
  return {
    ok: true,
    iso: `${m[3]}-${m[2]}-${m[1]}`,
    display: `${m[1]}/${m[2]}/${m[3]}`,
  };
}

function attachDateField(textId, nativeId) {
  const textEl = document.getElementById(textId);
  const nativeEl = document.getElementById(nativeId);
  if (!textEl) return;

  textEl.addEventListener("input", () => {
    const formatted = formatDateMask(textEl.value);
    textEl.value = formatted;
    if (!nativeEl) return;
    const parsed = parseDisplayDate(formatted);
    if (parsed.ok) {
      nativeEl.value = parsed.iso || "";
    }
  });

  if (nativeEl) {
    nativeEl.addEventListener("click", () => {
      try { nativeEl.showPicker?.(); } catch (_) {}
    });
    nativeEl.addEventListener("change", () => {
      textEl.value = isoToDisplayDate(nativeEl.value);
      textEl.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
}

function dateFieldHtml(textId, nativeId, isoVal, ariaLabel) {
  return `
    <div class="date-input-wrap">
      <input type="text" inputmode="numeric" id="${textId}" class="date-mask" maxlength="10" placeholder="ДД/ММ/ГГГГ" value="${esc(isoToDisplayDate(isoVal))}" />
      <span class="date-picker-trigger" title="Выбрать в календаре">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
        <input type="date" id="${nativeId}" class="date-native-overlay" tabindex="-1" aria-label="${esc(ariaLabel)}" value="${esc(isoVal || "")}" />
      </span>
    </div>`;
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
  if (enabled.has("cold_water") || enabled.has("hot_water") || enabled.has("irrigation")) groups.push("water");
  if (enabled.has("t") || enabled.has("t1") || enabled.has("t2") || enabled.has("t3")) groups.push("electricity");
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

// ── Modal dialog (MEDIUM-13: double-click & busy-close protection) ────
function openModal(title, bodyHtml, buttons, opts = {}) {
  state.modalBusy = false;
  document.getElementById("modal-title").textContent = title;
  const bodyEl = document.getElementById("modal-body");
  bodyEl.innerHTML = bodyHtml;
  bodyEl.querySelectorAll(".phone-mask").forEach(attachPhoneMask);
  bodyEl.querySelectorAll(".int-mask").forEach(attachIntMask);
  wireTelegramLinks(bodyEl);

  const actions = document.getElementById("modal-actions");
  actions.className = opts.stackActions ? "modal-actions-stack mt-12" : "row-gap mt-12";
  actions.innerHTML = "";
  const createdButtons = [];

  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `btn ${b.cls || ""}`;
    btn.textContent = b.label;
    if (b.id) btn.id = b.id;
    if (b.disabled) btn.disabled = true;
    createdButtons.push({ el: btn, def: b });

    btn.onclick = async () => {
      if (btn.disabled || state.modalBusy) return;
      if (!b.onClick) { closeModal(); return; }

      const prevStates = createdButtons.map(x => ({ el: x.el, disabled: x.el.disabled, text: x.el.textContent }));
      state.modalBusy = true;
      createdButtons.forEach(x => { x.el.disabled = true; });
      btn.textContent = b.busyLabel || "Выполняю...";

      let shouldClose = true;
      try {
        const result = await b.onClick();
        if (result === false) shouldClose = false;
      } catch (err) {
        showToast("❌ " + (err?.message || "Ошибка выполнения"), "error");
        shouldClose = false;
      } finally {
        state.modalBusy = false;
        if (!shouldClose) {
          prevStates.forEach(ps => {
            ps.el.disabled = ps.disabled;
            ps.el.textContent = ps.text;
          });
        }
      }
      if (shouldClose) closeModal();
    };
    actions.appendChild(btn);
  });
  document.getElementById("modal-overlay").classList.remove("hidden");
}

function closeModal(force = false) {
  if (state.modalBusy && !force) return;
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

// ── API client (LOW-06: structured FastAPI 422 & network error parsing) ─
function formatApiErrorDetail(detail, status) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    const msgs = detail.map(item => {
      if (typeof item === "string") return item;
      const type = item?.type || "";
      const loc = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : "";
      if (type === "int_type" || type === "int_parsing") return `Поле «${loc}» должно быть целым числом`;
      if (type === "greater_than_equal") return `Поле «${loc}» меньше допустимого значения`;
      if (type === "less_than_equal") return `Поле «${loc}» превышает допустимое значение`;
      if (type === "missing") return `Не заполнено обязательное поле «${loc}»`;
      return item?.msg || "Некорректные данные";
    });
    return msgs.join("; ");
  }
  if (status === 401) return "Сессия Telegram устарела. Закройте и откройте Mini App заново.";
  if (status === 403) return "Нет доступа.";
  if (status === 413) return "Файл слишком большой.";
  if (status >= 500) return `Ошибка сервера (${status}). Попробуйте повторить позже.`;
  return `Ошибка запроса (${status})`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  headers["X-Init-Data"] = tg?.initData || "";
  const fetchOpts = { ...options, headers };
  if (fetchOpts.body && !(fetchOpts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchOpts.body = JSON.stringify(fetchOpts.body);
  }
  let resp;
  try {
    resp = await fetch(path, fetchOpts);
  } catch (_) {
    const netErr = new Error("Нет соединения с сервером. Проверьте интернет и повторите попытку.");
    netErr.isNetwork = true;
    netErr.status = 0;
    throw netErr;
  }

  let data = null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await resp.json().catch(() => null);
  } else {
    const txt = await resp.text().catch(() => "");
    try { data = JSON.parse(txt); } catch (_) { data = { detail: txt || null }; }
  }

  if (!resp.ok) {
    const msg = formatApiErrorDetail(data?.detail, resp.status);
    const err = new Error(msg);
    err.status = resp.status;
    err.isNetwork = false;
    throw err;
  }
  return data ?? {};
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
  const initialName = String(state.user.name || "").trim();
  openModal("✏️ Изменить моё имя", `
    <div class="field">
      <label>Ваше отображаемое имя</label>
      <input type="text" id="own-name-input" maxlength="120" value="${esc(state.user.name)}" placeholder="Введите имя" />
    </div>
  `, [
    {
      id: "modal-save-own-name",
      label: "Сохранить",
      disabled: true,
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
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  const inputEl = document.getElementById("own-name-input");
  const saveBtn = document.getElementById("modal-save-own-name");
  inputEl?.addEventListener("input", () => {
    const val = inputEl.value.trim();
    if (saveBtn) saveBtn.disabled = !val || val === initialName;
  });
}

document.getElementById("edit-profile-btn")?.addEventListener("click", openEditOwnNameModal);

// ── Bootstrap (LOW-07: distinguish network, 401, 403, 5xx errors) ─────
async function loadBootstrap(applyUrlTab = false) {
  const content = document.getElementById("content");
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

    if (applyUrlTab) {
      const params = new URLSearchParams(window.location.search);
      const tabParam = params.get("tab");
      if (tabParam === "water" || tabParam === "electricity" || tabParam === "gas") {
        state.activeTab = "submit";
        state.submitSubtab = tabParam;
      } else if (tabParam && ["dashboard", "apartments", "families", "history", "photo", "admin"].includes(tabParam)) {
        state.activeTab = tabParam;
      }
    }

    if (state.selectedAptId) {
      await loadApartmentDetail(state.selectedAptId);
    } else {
      state.aptDetail = null;
      state.aptLoadError = null;
    }
    await renderTab();
  } catch (err) {
    if (err.status === 403) {
      content.innerHTML = `
        <div class="card center">
          <h3>⛔ Нет доступа</h3>
          <p class="muted">${esc(err.message)}</p>
          <p class="small muted">Обратитесь к владельцу объекта или администратору.</p>
        </div>`;
    } else if (err.status === 401) {
      content.innerHTML = `
        <div class="card center">
          <h3>🔒 Сессия устарела</h3>
          <p class="muted">${esc(err.message)}</p>
          <p class="small muted">Закройте окно и откройте приложение заново через кнопку меню в Telegram-боте.</p>
        </div>`;
    } else {
      content.innerHTML = `
        <div class="card center">
          <h3>⚠️ Ошибка подключения</h3>
          <p class="muted">${esc(err.message)}</p>
          <button class="btn mt-12" id="btn-retry-bootstrap">🔄 Повторить</button>
        </div>`;
      document.getElementById("btn-retry-bootstrap")?.addEventListener("click", () => {
        showLoading(content, "Подключение к серверу...");
        loadBootstrap(applyUrlTab);
      });
    }
  }
}

// HIGH-08: clear stale aptDetail immediately on switch & guard async responses
async function loadApartmentDetail(aptId) {
  if (!aptId) {
    state.aptDetail = null;
    state.aptLoading = false;
    state.aptLoadError = null;
    return true;
  }
  const seq = ++state.aptReqSeq;
  if (state.aptDetail?.apartment?.id !== aptId) {
    state.aptDetail = null;
  }
  state.aptLoading = true;
  state.aptLoadError = null;

  try {
    const detail = await api(`/api/apartments/${aptId}`);
    if (seq !== state.aptReqSeq || aptId !== state.selectedAptId) return false;
    state.aptDetail = detail;
    state.aptLoading = false;
    state.aptLoadError = null;
    if (detail?.apartment) {
      const idx = state.apartments.findIndex(a => a.id === detail.apartment.id);
      if (idx !== -1) {
        state.apartments[idx] = detail.apartment;
        renderAptSelector();
      }
    }
    return true;
  } catch (err) {
    if (seq !== state.aptReqSeq || aptId !== state.selectedAptId) return false;
    state.aptLoading = false;
    state.aptLoadError = err.message || "Не удалось загрузить объект";
    return false;
  }
}

async function selectApartment(nextId) {
  if (!nextId || nextId === state.selectedAptId) return;
  haptic("light");
  state.selectedAptId = nextId;
  state.aptDetail = null;
  state.aptLoadError = null;
  state.historyMonth = "";
  state.tabRenderSeq++;
  renderAptSelector();
  const root = document.getElementById("content");
  showLoading(root, "Загружаю данные объекта...");
  await loadApartmentDetail(state.selectedAptId);
  if (state.selectedAptId === nextId) {
    await renderTab();
  }
}

function renderAptSelector() {
  const sel = document.getElementById("apt-selector");
  if (state.apartments.length <= 1) { sel.classList.add("hidden"); return; }
  sel.classList.remove("hidden");
  const isTenant = state.user?.role === "tenant";
  sel.innerHTML = state.apartments
    .map(a => `<option value="${a.id}" ${a.id === state.selectedAptId ? "selected" : ""}>${esc(propIcon(a))} ${esc(propShort(a))}${!isTenant && isPropertyRented(a) ? " 👤" : ""}</option>`)
    .join("");
  sel.onchange = () => selectApartment(Number(sel.value));
}

function getNavItems() {
  const r = state.user.role;
  if (r === "tenant") {
    return [
      { id: "dashboard", ico: "📊", label: "Обзор" },
      { id: "submit", ico: "💧", label: "Подать" },
      { id: "history", ico: "📜", label: "История" },
      { id: "photo", ico: "📷", label: "Фото" },
    ];
  }
  if (r === "landlord") {
    return [
      { id: "dashboard", ico: "📊", label: "Обзор" },
      { id: "submit", ico: "💧", label: "Подать" },
      { id: "apartments", ico: "🌆", label: "Недвижимость" },
      { id: "history", ico: "📜", label: "История" },
      { id: "families", ico: "👨‍👩‍👧‍👦", label: "Люди" },
    ];
  }
  return [
    { id: "dashboard", ico: "📊", label: "Обзор" },
    { id: "submit", ico: "💧", label: "Подать" },
    { id: "apartments", ico: "🌆", label: "Недвижимость" },
    { id: "families", ico: "👨‍👩‍👧‍👦", label: "Люди" },
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
      if (state.reorderMode) {
        state.reorderMode = false;
        try { tg?.enableVerticalSwipes?.(); } catch (_) {}
      }
      state.activeTab = btn.dataset.tab;
      renderNav();
      await renderTab();
    };
  });
}

function renderAptLoadGuard(root) {
  if (!state.selectedAptId) return false;
  if (state.aptLoading) {
    showLoading(root, "Загружаю данные объекта...");
    return true;
  }
  if (state.aptLoadError) {
    root.innerHTML = `
      <div class="card center">
        <h3>⚠️ Не удалось загрузить объект</h3>
        <p class="muted">${esc(state.aptLoadError)}</p>
        <button class="btn mt-12" id="btn-retry-apt">🔄 Повторить</button>
      </div>`;
    document.getElementById("btn-retry-apt")?.addEventListener("click", async () => {
      showLoading(root, "Загружаю данные объекта...");
      await loadApartmentDetail(state.selectedAptId);
      await renderTab();
    });
    return true;
  }
  if (state.aptDetail && state.aptDetail.apartment?.id !== state.selectedAptId) {
    showLoading(root, "Загружаю данные объекта...");
    return true;
  }
  return false;
}

async function renderTab() {
  renderNav();
  const root = document.getElementById("content");
  const renderSeq = ++state.tabRenderSeq;
  try {
    if (["dashboard", "submit", "photo", "apartments"].includes(state.activeTab)) {
      if (renderAptLoadGuard(root)) return;
    }
    if (state.activeTab === "dashboard") return renderDashboard(root);
    if (state.activeTab === "submit") return renderSubmit(root);
    if (state.activeTab === "history") { showLoading(root, "Загружаю историю..."); return await renderHistory(root, renderSeq); }
    if (state.activeTab === "photo") return renderPhoto(root);
    if (state.activeTab === "apartments") return renderApartments(root);
    if (state.activeTab === "families") { showLoading(root, "Загружаю список людей..."); return await renderFamilies(root, renderSeq); }
    if (state.activeTab === "admin") { showLoading(root, "Загружаю панель управления..."); return await renderAdmin(root, renderSeq); }
  } catch (err) {
    if (renderSeq !== state.tabRenderSeq) return;
    root.innerHTML = `
      <div class="card center">
        <p class="muted">${esc(err.message)}</p>
        <button class="btn btn-secondary mt-8" id="btn-retry-tab">🔄 Повторить</button>
      </div>`;
    document.getElementById("btn-retry-tab")?.addEventListener("click", () => renderTab());
  }
}

// ── Dashboard (Обзор) ─────────────────────────────────────────────────
function formatTenantContractLine(apt) {
  const rawNo = String(apt.agreement_no || "").trim().replace(/^№\s*/i, "");
  const numPart = rawNo ? `№${esc(rawNo)}` : "б/н";
  if (!rawNo && !apt.agreement_date_start && !apt.agreement_date_finish) {
    return "📄 Договор: не указан";
  }
  return `📄 Договор ${numPart} от ${fmtDate(apt.agreement_date_start)} до ${fmtDate(apt.agreement_date_finish)}`;
}

function renderMeterRow(m, isTenant = false, irrigationMode = "parallel") {
  const displayLabel = m.meter_type === "t" ? "⚡ Электричество" : m.label;
  const isIrrAfterCold = m.meter_type === "irrigation" && irrigationMode === "after_cold";
  const replacedTag = m.replaced ? " · замена счётчика" : "";
  let subline;
  let rightCol;
  if (isTenant) {
    if (!m.submitted) {
      const prevTxt = m.has_base ? `пред.: ${fmtNum(m.previous)} ${esc(m.unit)}` : "начальное не задано";
      subline = `Ещё не подано (${prevTxt})`;
      rightCol = `<span class="small muted">0 ${esc(m.unit)}</span>`;
    } else if (!m.has_base) {
      subline = `Подано: ${fmtNum(m.current)} ${esc(m.unit)} · <b>не задано начальное показание</b>`;
      rightCol = `<span class="small muted">${fmtNum(m.current)} ${esc(m.unit)}</span>`;
    } else {
      subline = `${fmtNum(m.previous)} → ${fmtNum(m.current)} ${esc(m.unit)}${replacedTag}`;
      rightCol = `<div class="bold">${fmtNum(m.consumption)} ${esc(m.unit)}</div>`;
    }
  } else if (isIrrAfterCold) {
    if (!m.submitted) {
      const prevTxt = m.has_base ? `пред.: ${fmtNum(m.previous)} ${esc(m.unit)}` : "начальное не задано";
      subline = `Ещё не подано (${prevTxt}) · после ХВС`;
      rightCol = `<span class="small muted">в ХВС</span>`;
    } else if (!m.has_base) {
      subline = `Подано: ${fmtNum(m.current)} ${esc(m.unit)} · <b>не задано начальное показание</b>`;
      rightCol = `<span class="small muted">—</span>`;
    } else {
      subline = `${fmtNum(m.previous)} → ${fmtNum(m.current)} ${esc(m.unit)} (расход ${fmtNum(m.consumption)} ${esc(m.unit)}, по тарифу ХВС · вычет из водоотведения${replacedTag})`;
      rightCol = `<span class="small muted">в ХВС</span>`;
    }
  } else {
    if (!m.submitted) {
      const prevTxt = m.has_base ? `пред.: ${fmtNum(m.previous)} ${esc(m.unit)}` : "начальное не задано";
      subline = `Ещё не подано (${prevTxt})`;
      rightCol = `<span class="small muted">0 ₽</span>`;
    } else if (!m.has_base) {
      subline = `Подано: ${fmtNum(m.current)} ${esc(m.unit)} · <b>не задано начальное показание</b>`;
      rightCol = `<span class="small muted">—</span>`;
    } else if (!m.has_tariff) {
      subline = `${fmtNum(m.previous)} → ${fmtNum(m.current)} ${esc(m.unit)} (расход ${fmtNum(m.consumption)} ${esc(m.unit)}) · <b>не задан тариф</b>`;
      rightCol = `<span class="small muted">—</span>`;
    } else {
      subline = `${fmtNum(m.previous)} → ${fmtNum(m.current)} ${esc(m.unit)} (расход ${fmtNum(m.consumption)} ${esc(m.unit)}, тариф ${fmtNum(m.tariff)} ₽${replacedTag})`;
      rightCol = `<div class="bold">${fmtNum(m.cost)} ₽</div>`;
    }
  }
  return `
    <div class="table-row">
      <div><div class="bold">${esc(displayLabel)}</div><div class="small muted">${subline}</div></div>
      ${rightCol}
    </div>`;
}

function renderSewerageRow(sew, billing = null) {
  if (!sew) return "";
  const hasGvs = Boolean(billing?.meters?.hot_water);
  const isAfterCold = billing?.irrigation_mode === "after_cold" && Boolean(billing?.meters?.irrigation);
  const formula = isAfterCold
    ? (hasGvs ? "ХВС + ГВС минус Полив" : "ХВС минус Полив")
    : (hasGvs ? "ХВС + ГВС" : "ХВС");

  let subline;
  let rightCol;
  if (!sew.submitted) {
    subline = `${formula} · ещё не подано (тариф ${fmtNum(sew.tariff)} ₽)`;
    rightCol = `<span class="small muted">0 ₽</span>`;
  } else if (!sew.has_base) {
    subline = `${formula} · <b>не задано начальное показание</b>`;
    rightCol = `<span class="small muted">—</span>`;
  } else {
    subline = `${formula}: ${fmtNum(sew.consumption)} м³ (тариф ${fmtNum(sew.tariff)} ₽)`;
    rightCol = `<div class="bold">${fmtNum(sew.cost)} ₽</div>`;
  }
  return `
    <div class="table-row">
      <div><div class="bold">${esc(sew.label || "🚰 Водоотведение")}</div><div class="small muted">${subline}</div></div>
      ${rightCol}
    </div>`;
}

function renderDashboard(root) {
  const isTenant = state.user.role === "tenant";
  if (!state.aptDetail) {
    const canCreate = !isTenant;
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
  const enabled = new Set(getEnabledMeters(apt));
  const groups = getEnabledGroups(apt);
  const hasDomesticWater = enabled.has("cold_water") || enabled.has("hot_water");
  const hasIrrigation = enabled.has("irrigation");

  const irrMode = billing.irrigation_mode || apt.irrigation_mode || "parallel";
  const meterRows = [];
  let sewerageInserted = false;
  const meterList = Object.values(billing.meters);
  meterList.forEach((m, idx) => {
    meterRows.push(renderMeterRow(m, false, irrMode));
    const isDomWater = m.meter_type === "cold_water" || m.meter_type === "hot_water";
    const nextIsDomWater = meterList[idx + 1] && (meterList[idx + 1].meter_type === "cold_water" || meterList[idx + 1].meter_type === "hot_water");
    if (billing.sewerage && isDomWater && !nextIsDomWater && !sewerageInserted) {
      meterRows.push(renderSewerageRow(billing.sewerage, billing));
      sewerageInserted = true;
    }
  });
  if (billing.sewerage && !sewerageInserted) {
    meterRows.push(renderSewerageRow(billing.sewerage, billing));
  }
  const rows = meterRows.join("");

  const statBoxes = [];
  if (hasDomesticWater) {
    statBoxes.push(`<div class="stat-box"><div class="label">💧 Вода</div><div class="val">${fmtNum(billing.water_total)} ₽</div></div>`);
  }
  if (billing.sewerage) {
    statBoxes.push(`<div class="stat-box"><div class="label">🚰 Водоотведение</div><div class="val">${fmtNum(billing.sewerage_total)} ₽</div></div>`);
  }
  if (hasIrrigation && irrMode !== "after_cold") {
    statBoxes.push(`<div class="stat-box"><div class="label">🌱 Полив</div><div class="val">${fmtNum(billing.irrigation_total || 0)} ₽</div></div>`);
  }
  if (groups.includes("electricity")) {
    statBoxes.push(`<div class="stat-box"><div class="label">⚡ Электричество</div><div class="val">${fmtNum(billing.electricity_total)} ₽</div></div>`);
  }
  if (groups.includes("gas")) {
    statBoxes.push(`<div class="stat-box"><div class="label">🔥 Газ</div><div class="val">${fmtNum(billing.gas_total)} ₽</div></div>`);
  }
  const isTotalWide = statBoxes.length >= 2 && statBoxes.length % 2 === 0;
  const totalLbl = billing.is_complete === false ? "💰 Итого (предв.)" : "💰 Итого";
  statBoxes.push(`<div class="stat-box ${isTotalWide ? "stat-box-wide" : ""}"><div class="label">${totalLbl}</div><div class="val">${fmtNum(billing.grand_total)} ₽</div></div>`);
  const statGridClass = statBoxes.length >= 2 ? "grid-2" : "";

  const quickButtons = [];
  if (groups.includes("water")) quickButtons.push(`<button class="btn" id="quick-water">💧 Подать воду</button>`);
  if (groups.includes("electricity")) quickButtons.push(`<button class="btn" id="quick-elec">⚡ Подать электричество</button>`);
  if (groups.includes("gas")) quickButtons.push(`<button class="btn" id="quick-gas">🔥 Подать газ</button>`);
  const quickGridClass = quickButtons.length === 3 ? "grid-3" : quickButtons.length === 2 ? "grid-2" : "";

  const rented = isPropertyRented(apt);
  const statusPillHtml = !isTenant && rented
    ? `<span class="pill ok">👤 Аренда</span>`
    : "";

  const contractExists = hasContract(apt);
  const contractLineHtml = contractExists
    ? (isTenant
        ? formatTenantContractLine(apt)
        : `📄 Договор: ${esc(apt.agreement_no || "—")} · ${fmtDate(apt.agreement_date_start)} — ${fmtDate(apt.agreement_date_finish)}`)
    : "";

  const incompleteBannerHtml = (billing.is_complete === false && billing.incomplete_reasons?.length)
    ? `<div class="incomplete-banner">ℹ️ Предварительный расчёт: ${esc(billing.incomplete_reasons.join(", "))}</div>`
    : "";

  const showDashChips = state.apartments.length > 0 && (!isTenant || state.apartments.length > 1);
  const dashChipsHtml = showDashChips
    ? `
      <div class="card">
        <div id="dash-apt-chips-list" class="apt-chips-list">
          ${state.apartments.map(a => `
            <button type="button" class="btn btn-sm apt-chip ${a.id === state.selectedAptId ? "" : "btn-secondary"}" data-apt="${a.id}">
              ${esc(propIcon(a))} ${esc(propShort(a))}${!isTenant && isPropertyRented(a) ? " 👤" : ""}${a.has_overdue_meters ? '<span class="overdue-dot" title="Есть неподанные в срок счётчики">🟡</span>' : ""}
            </button>`).join("")}
        </div>
      </div>`
    : "";

  root.innerHTML = `
    ${dashChipsHtml}
    <div class="card">
      <div class="row-between">
        <h2>${esc(propIcon(apt))} ${esc(propTitle(apt))}</h2>
        ${statusPillHtml}
      </div>
      ${contractLineHtml ? `<div class="small muted">${contractLineHtml}</div>` : ""}
      ${!isTenant && tenants.length ? `<div class="small muted mt-8">Наниматели: ${tenants.map(t => esc(t.name || t.telegram_id)).join(", ")}</div>` : ""}
      ${statBoxes.length ? `<div class="${statGridClass} mt-12">${statBoxes.join("")}</div>` : ""}
      ${incompleteBannerHtml}
    </div>
    <div class="card">
      <h3>📊 Расход за ${esc(billing.period_label)}</h3>
      ${rows || `<p class="small muted">Нет подключённых счётчиков.</p>`}
    </div>
    ${quickButtons.length ? `<div class="${quickGridClass}">${quickButtons.join("")}</div>` : ""}
  `;

  const dashChipsContainer = document.getElementById("dash-apt-chips-list");
  if (!isTenant && state.apartments.length > 1 && dashChipsContainer) {
    initPropertyDragAndDrop(dashChipsContainer, root);
  }
  dashChipsContainer?.querySelectorAll("[data-apt]").forEach(btn => {
    btn.onclick = () => {
      if (dashChipsContainer.dataset.justDragged === "1") return;
      selectApartment(Number(btn.dataset.apt));
    };
  });

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
  const p = val === null || val === undefined ? "не задано" : `<b>${fmtNum(val)}</b> ${unit}`;
  const isTenant = state.user?.role === "tenant";
  if (isTenant) return `предыдущее: ${p}`;
  const t = tariff === null || tariff === undefined ? "тариф не задан" : `тариф: ${fmtNum(tariff)} ₽`;
  return `предыдущее: ${p}, ${t}`;
}

function renderSubmit(root) {
  if (!state.aptDetail) {
    root.innerHTML = `<div class="card center muted">Сначала выберите или добавьте объект недвижимости.</div>`;
    return;
  }
  const { apartment: apt, prev_readings: prev, billing } = state.aptDetail;
  const tariffs = state.aptDetail.tariffs || {};
  const monthBaseInfo = state.aptDetail.month_base_info || {};
  const enabled = getEnabledMeters(apt);
  const groups = getEnabledGroups(apt);
  if (!groups.length) {
    root.innerHTML = `<div class="card center muted">Для этого объекта не выбрано ни одного счётчика.</div>`;
    return;
  }
  if (!groups.includes(state.submitSubtab)) {
    state.submitSubtab = groups[0];
  }

  const isTenant = state.user.role === "tenant";
  const elecBtnLabel = isTenant ? "✅ Сохранить показания электроэнергии" : "✅ Сохранить электричество";

  const groupDefs = {
    water: { label: "💧 Водомеры", btn: "✅ Сохранить показания воды", meters: METER_META.filter(m => m.group === "water" && enabled.includes(m.id)) },
    electricity: { label: "⚡ Электричество", btn: elecBtnLabel, meters: METER_META.filter(m => m.group === "electricity" && enabled.includes(m.id)) },
    gas: { label: "🔥 Газ", btn: "✅ Сохранить показания газа", meters: METER_META.filter(m => m.group === "gas" && enabled.includes(m.id)) },
  };

  const activeGroup = groupDefs[state.submitSubtab];
  const subtabsHtml = groups.length > 1
    ? `<div class="subtabs">${groups.map(g => `<button class="subtab ${state.submitSubtab === g ? "active" : ""}" data-sub="${g}">${groupDefs[g].label}</button>`).join("")}</div>`
    : "";

  const isIrrAfterCold = apt.irrigation_mode === "after_cold";
  const fieldsHtml = activeGroup.meters.map(m => {
    let prevInfo;
    if (m.id === "irrigation" && isIrrAfterCold && !isTenant) {
      const pTxt = prev[m.id] === null || prev[m.id] === undefined ? "не задано" : `<b>${fmtNum(prev[m.id])}</b> ${m.unit}`;
      prevInfo = `предыдущее: ${pTxt}, по тарифу ХВС (вычет из водоотведения)`;
    } else {
      prevInfo = fmtPrevLabel(prev[m.id], m.unit, tariffs[m.id]);
    }
    return `
      <div class="field">
        <label>${esc(m.label)} (${prevInfo})</label>
        <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="in-${m.id}" placeholder="${prev[m.id] !== null && prev[m.id] !== undefined ? esc(formatIntSpaces(prev[m.id])) : ""}" />
      </div>
    `;
  }).join("");

  root.innerHTML = `
    <div class="card">
      <h2>Подача показаний — ${esc(propShort(apt))}</h2>
      ${subtabsHtml}
      ${fieldsHtml}
      <div id="calc-preview" class="preview-box muted">Введите целые значения для предрасчёта.</div>
      <button class="btn" id="btn-submit-group" disabled>${activeGroup.btn}</button>
    </div>
    ${isTenant ? "" : `<div class="card"><button class="btn btn-secondary" id="open-photo-btn">📷 Отправить фото счётчиков</button></div>`}
  `;

  root.querySelectorAll("[data-sub]").forEach(btn => {
    btn.onclick = () => { state.submitSubtab = btn.dataset.sub; renderSubmit(root); };
  });
  document.getElementById("open-photo-btn")?.addEventListener("click", () => { state.activeTab = "photo"; renderTab(); });

  let activeSubmissionId = null;
  const submitBtn = document.getElementById("btn-submit-group");
  const boundAptId = apt.id;
  const boundGroup = state.submitSubtab;

  const updatePreview = () => {
    activeSubmissionId = null;
    const box = document.getElementById("calc-preview");
    const entered = [];
    for (const m of activeGroup.meters) {
      const parsed = parseIntSpaces(document.getElementById(`in-${m.id}`).value);
      if (parsed.empty) continue;
      if (!parsed.ok) {
        box.innerHTML = `<span class="inline-error">${esc(m.plain)}: ${esc(parsed.error)}</span>`;
        if (submitBtn) submitBtn.disabled = true;
        return;
      }
      entered.push({ m, val: parsed.value });
    }
    if (!entered.length) {
      box.textContent = "Введите целые значения для предрасчёта.";
      if (submitBtn) submitBtn.disabled = true;
      return;
    }
    if (submitBtn) submitBtn.disabled = false;

    const deltaParts = [];
    const monthParts = [];
    let monthTotalCost = 0;
    let monthWaterSum = 0;
    let monthIrrDiff = 0;
    let hasDomWaterInMonth = false;
    let missingBase = false;
    let missingTariff = false;
    let hasRepeatOrReplace = false;

    // Seed with already-submitted meters of the same group in current month (HIGH-01)
    const enteredIds = new Set(entered.map(e => e.m.id));
    if (boundGroup === "water" && billing?.meters) {
      for (const wm of ["cold_water", "hot_water", "irrigation"]) {
        if (enteredIds.has(wm)) continue;
        const bm = billing.meters[wm];
        if (!bm || !bm.submitted || !bm.has_base) continue;
        if (wm === "cold_water" || wm === "hot_water") {
          monthWaterSum += bm.consumption;
          hasDomWaterInMonth = true;
        }
        if (wm === "irrigation") {
          monthIrrDiff = bm.consumption;
        }
      }
    }

    for (const { m, val } of entered) {
      const pv = prev[m.id];
      const tf = tariffs[m.id];
      const mbi = monthBaseInfo[m.id] || {};
      const mBase = mbi.month_base !== undefined ? mbi.month_base : pv;
      const repOffset = mbi.replacement_offset || 0;
      if (mbi.submitted_this_month || mbi.replaced_this_month) {
        hasRepeatOrReplace = true;
      }

      if (pv === null || pv === undefined) {
        missingBase = true;
        deltaParts.push(`${m.plain}: <b>${fmtNum(val)} ${m.unit}</b> (нет начального)`);
        continue;
      }

      const deltaDiff = Math.max(0, val - pv);
      const monthDiff = mBase !== null && mBase !== undefined
        ? Math.max(0, val - mBase) + repOffset
        : deltaDiff;

      deltaParts.push(`${m.plain}: <b>${fmtNum(deltaDiff)} ${m.unit}</b>`);
      monthParts.push(`${m.plain}: <b>${fmtNum(monthDiff)} ${m.unit}</b>`);

      if (m.id === "cold_water" || m.id === "hot_water") {
        monthWaterSum += monthDiff;
        hasDomWaterInMonth = true;
      }
      if (m.id === "irrigation") {
        monthIrrDiff = monthDiff;
        if (isIrrAfterCold) continue;
      }
      if (!isTenant) {
        if (tf === null || tf === undefined) {
          missingTariff = true;
        } else {
          monthTotalCost += Math.round(monthDiff * tf);
        }
      }
    }

    const sewRate = tariffs.sewerage;
    let sewCost = 0;
    if (!isTenant && boundGroup === "water" && hasDomWaterInMonth && sewRate !== null && sewRate !== undefined && sewRate > 0 && !missingBase) {
      const sewVol = isIrrAfterCold ? Math.max(0, monthWaterSum - monthIrrDiff) : monthWaterSum;
      sewCost = Math.round(sewVol * sewRate);
      monthTotalCost += sewCost;
      monthParts.push(`Водоотведение: <b>${fmtNum(sewVol)} м³</b>`);
    }

    const lines = [];
    if (hasRepeatOrReplace) {
      lines.push(`С последней подачи — ${deltaParts.join(" · ")}`);
      lines.push(`За текущий месяц — ${monthParts.join(" · ")}`);
    } else {
      lines.push(`Расход за месяц — ${(monthParts.length ? monthParts : deltaParts).join(" · ")}`);
    }

    if (!isTenant) {
      const costLine = missingBase
        ? "⚠️ Для части счётчиков не задано начальное показание"
        : missingTariff
          ? "⚠️ Для части счётчиков не задан тариф"
          : sewCost > 0
            ? `За месяц (по группе): <b>${fmtNum(monthTotalCost)} ₽</b> (вкл. водоотведение ${fmtNum(sewCost)} ₽)`
            : `За месяц (по группе): <b>${fmtNum(monthTotalCost)} ₽</b>`;
      lines.push(costLine);
    }
    box.innerHTML = lines.join("<br>");
  };

  activeGroup.meters.forEach(m => {
    const el = document.getElementById(`in-${m.id}`);
    attachIntMask(el);
    el?.addEventListener("input", updatePreview);
  });

  document.getElementById("btn-submit-group").onclick = () => {
    if (!activeSubmissionId) activeSubmissionId = genSubmissionId();
    submitMeterGroup(boundAptId, boundGroup, activeGroup.meters, false, activeSubmissionId);
  };
}

async function submitMeterGroup(aptId, groupName, groupMeters, force, submissionId) {
  if (aptId !== state.selectedAptId) {
    return showToast("Выбран другой объект. Обновите форму.", "error");
  }
  const payload = { force, submission_id: submissionId || genSubmissionId() };
  let count = 0;
  for (const m of groupMeters) {
    const el = document.getElementById(`in-${m.id}`);
    const parsed = parseIntSpaces(el?.value);
    if (parsed.empty) continue;
    if (!parsed.ok) {
      return showToast(`Некорректное значение для ${m.plain}: ${parsed.error}`, "error");
    }
    payload[m.id] = parsed.value;
    count++;
  }
  if (!count) return showToast("Введите хотя бы одно показание счётчика", "error");

  const btn = document.getElementById("btn-submit-group");
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/apartments/${aptId}/readings/${groupName}`, { method: "POST", body: payload });

    if (res.rejected_decrease) {
      if (btn) btn.disabled = false;
      const linkHtml = res.can_open_meters
        ? `<div class="mt-8"><button type="button" class="inline-link-btn" id="jump-to-meters-btn">🔧 Перейти к управлению счётчиками</button></div>`
        : "";
      openModal("⚠️ Значение меньше зафиксированного", `<p>${esc(res.warning)}</p>${linkHtml}`, [
        { label: "Понятно", cls: "btn-secondary" },
      ]);
      document.getElementById("jump-to-meters-btn")?.addEventListener("click", async () => {
        closeModal(true);
        state.activeTab = "apartments";
        state.manageSubtab = "settings";
        await renderTab();
        openMetersModal(aptId);
      });
      return;
    }

    if (res.needs_confirm) {
      if (btn) btn.disabled = false;
      return openModal("⚠️ Повторная подача за этот месяц", `<p>${esc(res.warning)}</p>`, [
        {
          label: "Сохранить",
          onClick: () => {
            submitMeterGroup(aptId, groupName, groupMeters, true, payload.submission_id);
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    }

    const isTenant = state.user.role === "tenant";
    const sewAdd = res.billing.sewerage_total || 0;
    const irrAdd = res.billing.irrigation_total || 0;
    const waterSum = (res.billing.water_total || 0) + sewAdd + irrAdd;
    const waterVolSum = (res.billing.water_consumption_total || 0) + (res.billing.irrigation_consumption_total || 0);
    const costMap = isTenant
      ? {
          water: `Вода сохранена (за месяц: ${fmtNum(waterVolSum)} м³)`,
          electricity: `Показания электроэнергии сохранены (за месяц: ${fmtNum(res.billing.electricity_consumption_total)} кВт·ч)`,
          gas: `Газ сохранён (за месяц: ${fmtNum(res.billing.gas_consumption_total)} м³)`,
        }
      : {
          water: sewAdd > 0
            ? `Вода сохранена (за месяц: ${fmtNum(waterSum)} ₽, вкл. водоотведение ${fmtNum(sewAdd)} ₽)`
            : `Вода сохранена (за месяц: ${fmtNum(waterSum)} ₽)`,
          electricity: `Электричество сохранено (за месяц: ${fmtNum(res.billing.electricity_total)} ₽)`,
          gas: `Газ сохранён (за месяц: ${fmtNum(res.billing.gas_total)} ₽)`,
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
async function renderHistory(root, renderSeq = state.tabRenderSeq) {
  if (!state.selectedAptId) { root.innerHTML = `<div class="card center muted">Нет доступных объектов недвижимости.</div>`; return; }
  const aptId = state.selectedAptId;
  const seq = ++state.histReqSeq;
  const q = state.historyMonth ? `?year=${state.historyMonth.split("-")[0]}&month=${state.historyMonth.split("-")[1]}` : "";
  const data = await api(`/api/apartments/${aptId}/history${q}`);
  if (seq !== state.histReqSeq || renderSeq !== state.tabRenderSeq || aptId !== state.selectedAptId || state.activeTab !== "history") return;

  const currentApt = state.apartments.find(a => a.id === aptId) || state.aptDetail?.apartment;
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
            <div class="bold">${esc(r.meter_type === "t" ? "⚡ Электричество" : r.label)}: ${fmtNum(r.value)} ${esc(r.unit)}${r.is_replacement_init ? ' <span class="pill">новый счётчик</span>' : ""}</div>
            <div class="small muted">#${r.id} · ${esc(r.reading_date)} · ID ${r.submitted_by}</div>
          </div>
          ${canEdit ? `<button class="btn btn-secondary btn-sm edit-reading-btn" data-id="${r.id}" data-val="${r.value}" data-lbl="${esc(r.label)}">✏️</button>` : ""}
        </div>`).join("")
    : `<p class="muted center">Показаний за выбранный период нет.</p>`;

  const isTenantHist = state.user.role === "tenant";
  const billParts = [];
  if (isTenantHist) {
    if (Object.keys(data.billing.meters).some(k => k === "cold_water" || k === "hot_water")) {
      billParts.push(`вода <b>${fmtNum(data.billing.water_consumption_total)} м³</b>`);
    }
    if (Object.keys(data.billing.meters).includes("irrigation")) {
      billParts.push(`полив <b>${fmtNum(data.billing.irrigation_consumption_total || 0)} м³</b>`);
    }
    if (Object.keys(data.billing.meters).some(k => ["t", "t1", "t2", "t3"].includes(k))) {
      billParts.push(`электричество <b>${fmtNum(data.billing.electricity_consumption_total)} кВт·ч</b>`);
    }
    if (Object.keys(data.billing.meters).includes("gas")) {
      billParts.push(`газ <b>${fmtNum(data.billing.gas_consumption_total)} м³</b>`);
    }
  } else {
    if (data.billing.water_total || Object.keys(data.billing.meters).some(k => k === "cold_water" || k === "hot_water")) {
      billParts.push(`вода <b>${fmtNum(data.billing.water_total)} ₽</b>`);
    }
    if (data.billing.sewerage) {
      billParts.push(`водоотведение <b>${fmtNum(data.billing.sewerage_total)} ₽</b>`);
    }
    if (data.billing.irrigation_mode !== "after_cold" && (data.billing.irrigation_total || Object.keys(data.billing.meters).includes("irrigation"))) {
      billParts.push(`полив <b>${fmtNum(data.billing.irrigation_total || 0)} ₽</b>`);
    }
    if (data.billing.electricity_total || Object.keys(data.billing.meters).some(k => ["t", "t1", "t2", "t3"].includes(k))) {
      billParts.push(`электричество <b>${fmtNum(data.billing.electricity_total)} ₽</b>`);
    }
    if (data.billing.gas_total || Object.keys(data.billing.meters).includes("gas")) {
      billParts.push(`газ <b>${fmtNum(data.billing.gas_total)} ₽</b>`);
    }
    billParts.push(`всего <b>${fmtNum(data.billing.grand_total)} ₽</b>`);
  }

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>📜 История — ${esc(propShort(currentApt))}</h2>
        <select id="month-filter" class="apt-select">${monthOptions}</select>
      </div>
      ${state.historyMonth ? `<div class="preview-box mt-8">За ${esc(data.billing.period_label)}: ${billParts.join(" · ")}</div>` : ""}
      <div class="mt-8">${listHtml}</div>
    </div>
  `;

  document.getElementById("month-filter").onchange = async (e) => {
    state.historyMonth = e.target.value;
    showLoading(root, "Загружаю историю...");
    await renderHistory(root, renderSeq);
  };

  root.querySelectorAll(".edit-reading-btn").forEach(btn => {
    btn.onclick = () => {
      const id = Number(btn.dataset.id);
      const initialDigits = parseIntSpaces(btn.dataset.val).digits;
      openModal(`Корректировка #${id}`, `
        <p class="small muted">${esc(btn.dataset.lbl)}</p>
        <div class="field"><label>Новое целое значение</label><input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="corr-val" value="${esc(formatIntSpaces(btn.dataset.val))}" /></div>
      `, [
        {
          id: "modal-save-corr",
          label: "Сохранить",
          disabled: true,
          onClick: async () => {
            const parsed = parseIntSpaces(document.getElementById("corr-val").value);
            if (parsed.empty || !parsed.ok) { showToast(parsed.error || "Введите целое число >= 0", "error"); return false; }
            try {
              await api(`/api/readings/${id}`, { method: "PUT", body: { value: parsed.value } });
              showToast("✅ Показание исправлено");
              await loadApartmentDetail(state.selectedAptId);
              await renderHistory(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);

      const corrInput = document.getElementById("corr-val");
      const saveBtn = document.getElementById("modal-save-corr");
      corrInput?.addEventListener("input", () => {
        const parsed = parseIntSpaces(corrInput.value);
        if (saveBtn) saveBtn.disabled = parsed.empty || !parsed.ok || parsed.digits === initialDigits;
      });
    };
  });
}

// ── Photo (Фото — без сохранения на сервере, LOW-09 isUploading) ──────
function getPhotoResourceOptions(apt) {
  const enabled = new Set(getEnabledMeters(apt));
  const opts = [];
  if (enabled.has("cold_water")) opts.push({ id: "cold_water", icon: "💧", label: "ХВС" });
  if (enabled.has("hot_water")) opts.push({ id: "hot_water", icon: "♨️", label: "ГВС" });
  if (enabled.has("irrigation")) opts.push({ id: "irrigation", icon: "🌱", label: "Полив" });
  if (enabled.has("t") || enabled.has("t1") || enabled.has("t2") || enabled.has("t3")) {
    opts.push({ id: "electricity", icon: "⚡", label: "Электричество" });
  }
  if (enabled.has("gas")) opts.push({ id: "gas", icon: "🔥", label: "Газ" });
  return opts;
}

function renderPhoto(root) {
  if (!state.aptDetail) { root.innerHTML = `<div class="card center muted">Нет доступных объектов недвижимости.</div>`; return; }
  const apt = state.aptDetail.apartment;
  const boundAptId = apt.id;
  const isTenant = state.user.role === "tenant";
  const resources = getPhotoResourceOptions(apt);
  const selectedIds = new Set();
  let isUploading = false;

  const recipientHint = isTenant
    ? "Фото будет переслано наймодателю в Telegram."
    : "Фото будет переслано владельцам объекта в Telegram.";
  const sendBtnLabel = isTenant ? "📤 Отправить фото наймодателю" : "📤 Отправить владельцам";

  root.innerHTML = `
    <div class="card">
      <h2>📷 Фото счётчиков — ${esc(propShort(apt))}</h2>
      <p class="small muted">${recipientHint}</p>
      <div class="field mt-8"><label>Выберите или сделайте фото (до 15 МБ)</label><input type="file" id="photo-file" accept="image/*" /></div>
      ${resources.length ? `
        <div class="field" id="photo-resources-field">
          <label>Что на фото? (нажмите, чтобы добавить в описание)</label>
          <div class="resource-chips" id="photo-resource-chips">
            ${resources.map(r => `<button type="button" class="resource-chip" data-res-id="${r.id}">${r.icon} ${esc(r.label)}</button>`).join("")}
          </div>
        </div>` : ""}
      <div class="field"><label>Дополнительный комментарий (необязательно)</label><input type="text" id="photo-comment" placeholder="Например: за ноябрь" /></div>
      <div id="photo-caption-preview" class="photo-caption-preview hidden"></div>
      <button class="btn" id="btn-send-photo" disabled>${sendBtnLabel}</button>
    </div>
  `;

  const fileInput = document.getElementById("photo-file");
  const commentInput = document.getElementById("photo-comment");
  const previewBox = document.getElementById("photo-caption-preview");
  const sendBtn = document.getElementById("btn-send-photo");

  const buildPhotoComment = () => {
    const chosenLabels = resources.filter(r => selectedIds.has(r.id)).map(r => r.label);
    const extra = (commentInput?.value || "").trim();
    if (chosenLabels.length && extra) return `${chosenLabels.join(", ")} — ${extra}`;
    if (chosenLabels.length) return chosenLabels.join(", ");
    return extra;
  };

  const updatePhotoUi = () => {
    const caption = buildPhotoComment();
    if (previewBox) {
      if (caption) {
        previewBox.innerHTML = `💬 Описание под фото: <b>${esc(caption)}</b>`;
        previewBox.classList.remove("hidden");
      } else {
        previewBox.classList.add("hidden");
      }
    }
    if (sendBtn) {
      sendBtn.disabled = isUploading || !(fileInput?.files?.length > 0);
      sendBtn.textContent = isUploading ? "⏳ Отправляю фото..." : sendBtnLabel;
    }
  };

  fileInput?.addEventListener("change", updatePhotoUi);
  commentInput?.addEventListener("input", updatePhotoUi);

  root.querySelectorAll(".resource-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      if (isUploading) return;
      haptic("light");
      const id = btn.dataset.resId;
      if (selectedIds.has(id)) {
        selectedIds.delete(id);
        btn.classList.remove("active");
      } else {
        selectedIds.add(id);
        btn.classList.add("active");
      }
      updatePhotoUi();
    });
  });

  sendBtn.onclick = async () => {
    if (isUploading) return;
    if (!fileInput?.files?.length) return showToast("Выберите фотографию", "error");
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("comment", buildPhotoComment());
    isUploading = true;
    updatePhotoUi();
    try {
      const res = await api(`/api/apartments/${boundAptId}/photo`, { method: "POST", body: fd });
      if (res.recipients_count > 0) {
        showToast(`✅ Фото отправлено (получателей: ${res.recipients_count})`);
      } else {
        showToast("⚠️ Получателей в Telegram не найдено — фото не доставлено");
      }
      fileInput.value = "";
      if (commentInput) commentInput.value = "";
      selectedIds.clear();
      root.querySelectorAll(".resource-chip.active").forEach(b => b.classList.remove("active"));
    } catch (err) {
      showToast("❌ " + err.message, "error");
    } finally {
      isUploading = false;
      updatePhotoUi();
    }
  };
}

// ── Settings & Meters Management Modals ───────────────────────────────
async function openMetersModal(aptId) {
  const apt = state.aptDetail?.apartment;
  if (!apt || apt.id !== aptId) return;
  const enabled = getEnabledMeters(apt);
  const physicalDevices = getPhysicalMeterDevices(enabled);
  if (!physicalDevices.length) {
    return showToast("Для объекта не подключено ни одного счётчика", "error");
  }

  let devices = state.aptDetail?.meter_devices || [];
  try {
    const resp = await api(`/api/apartments/${aptId}/meters`);
    devices = resp.devices || [];
  } catch (err) {
    showToast("❌ " + err.message, "error");
    return;
  }

  const devMap = new Map(devices.map(d => [d.meter_type, d]));
  const selectedIntervals = {};
  physicalDevices.forEach(mt => {
    const d = devMap.get(mt);
    selectedIntervals[mt] = new Set(Array.isArray(d?.notify_intervals) ? d.notify_intervals : []);
  });

  const cardsHtml = physicalDevices.map(mt => {
    const label = METER_DEVICE_LABELS[mt] || mt;
    const dev = devMap.get(mt) || {};
    const activeSet = selectedIntervals[mt];
    return `
      <div class="meter-device-card" data-dev-mt="${mt}">
        <h4>${esc(label)}</h4>
        <div class="field">
          <label>Номер счётчика (необязательно)</label>
          <input type="text" id="md-ser-${mt}" maxlength="100" placeholder="Например: 12345678" value="${esc(dev.serial_number || "")}" />
        </div>
        <div class="field">
          <label>Срок поверки (ДД/ММ/ГГГГ)</label>
          ${dateFieldHtml(`md-due-${mt}`, `md-due-nat-${mt}`, dev.verification_due_date || "", `Срок поверки ${label}`)}
        </div>
        <div class="field" style="margin-bottom:0">
          <label>Уведомить о поверке или замене счётчика</label>
          <div class="notify-chips">
            ${VERIFY_INTERVALS.map(iv => `
              <button type="button" class="notify-chip ${activeSet.has(iv.id) ? "active" : ""}" data-md-mt="${mt}" data-md-iv="${iv.id}">
                ${esc(iv.label)}
              </button>
            `).join("")}
          </div>
        </div>
      </div>`;
  }).join("");

  const bodyHtml = `
    <div style="margin-bottom:12px">
      <button type="button" class="btn btn-secondary" id="btn-open-replace-meter">🔄 Замена счётчика</button>
    </div>
    <p class="small muted">Уведомления о поверке приходят владельцу объекта в 15:00 по локальному времени.</p>
    <div class="mt-8">${cardsHtml}</div>
  `;

  openModal(`🔧 Управление счётчиками — ${propShort(apt)}`, bodyHtml, [
    {
      label: "💾 Сохранить",
      onClick: async () => {
        const items = [];
        for (const mt of physicalDevices) {
          const label = METER_DEVICE_LABELS[mt] || mt;
          const serial = (document.getElementById(`md-ser-${mt}`)?.value || "").trim() || null;
          const dueRaw = document.getElementById(`md-due-${mt}`)?.value || "";
          const parsedDue = parseDisplayDate(dueRaw);
          if (!parsedDue.ok) {
            showToast(`❌ ${label}: ${parsedDue.error}`, "error");
            return false;
          }
          items.push({
            meter_type: mt,
            serial_number: serial,
            verification_due_date: parsedDue.display,
            notify_intervals: Array.from(selectedIntervals[mt]),
          });
        }
        try {
          await api(`/api/apartments/${aptId}/meters`, { method: "PUT", body: { items } });
          showToast("✅ Данные счётчиков сохранены");
          await loadApartmentDetail(aptId);
        } catch (err) {
          showToast("❌ " + err.message, "error");
          return false;
        }
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  physicalDevices.forEach(mt => attachDateField(`md-due-${mt}`, `md-due-nat-${mt}`));

  document.querySelectorAll(".notify-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      haptic("light");
      const mt = btn.dataset.mdMt;
      const iv = btn.dataset.mdIv;
      if (selectedIntervals[mt].has(iv)) {
        selectedIntervals[mt].delete(iv);
        btn.classList.remove("active");
      } else {
        selectedIntervals[mt].add(iv);
        btn.classList.add("active");
      }
    });
  });

  document.getElementById("btn-open-replace-meter")?.addEventListener("click", () => {
    closeModal(true);
    openReplaceMeterModal(aptId);
  });
}

function openReplaceMeterModal(aptId) {
  const apt = state.aptDetail?.apartment;
  if (!apt || apt.id !== aptId) return;
  const enabled = getEnabledMeters(apt);
  const physicalDevices = getPhysicalMeterDevices(enabled);
  const multiElecEnabled = MULTI_ELEC_IDS.filter(id => enabled.includes(id));
  const prev = state.aptDetail?.prev_readings || {};

  const optionsHtml = physicalDevices.map(mt => {
    const label = METER_DEVICE_LABELS[mt] || mt;
    return `<option value="${mt}">${esc(label)}</option>`;
  }).join("");

  const renderValueFields = (selectedDevice) => {
    const isMultiElec = selectedDevice === "t" && multiElecEnabled.length > 0;
    if (isMultiElec) {
      return multiElecEnabled.map(mt => {
        const meta = METER_META.find(m => m.id === mt) || { plain: mt, unit: "кВт·ч" };
        const pv = prev[mt];
        const pvTxt = pv === null || pv === undefined ? "не задано" : `${fmtNum(pv)} ${meta.unit}`;
        return `
          <div class="meter-device-card">
            <h4>${esc(meta.label)} <span class="small muted">(последнее: ${esc(pvTxt)})</span></h4>
            <div class="grid-2">
              <div class="field" style="margin-bottom:0">
                <label>Старый (финал)</label>
                <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="rep-old-${mt}" placeholder="Финал" />
              </div>
              <div class="field" style="margin-bottom:0">
                <label>Новый (старт)</label>
                <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="rep-new-${mt}" value="0" placeholder="0" />
              </div>
            </div>
          </div>`;
      }).join("");
    }
    const meta = METER_META.find(m => m.id === selectedDevice) || { unit: "" };
    const pv = prev[selectedDevice];
    const pvTxt = pv === null || pv === undefined
      ? "Последнее показание не задано"
      : `Последнее зафиксированное показание: ${fmtNum(pv)} ${meta.unit}`;
    return `
      <div class="small muted" style="margin-bottom:8px">${esc(pvTxt)}</div>
      <div class="field">
        <label>Конечное показание старого счётчика</label>
        <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="rep-old-final" placeholder="Например: 12 480" />
      </div>
      <div class="field">
        <label>Начальное показание нового счётчика</label>
        <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="rep-new-init" value="0" placeholder="Например: 0" />
      </div>`;
  };

  const firstDev = physicalDevices[0];
  openModal(`🔄 Замена счётчика — ${propShort(apt)}`, `
    <p class="small muted">Остаток потребления по старому счётчику будет учтён в текущем месяце, а дальнейший расчёт пойдёт с начального показания нового счётчика.</p>
    <div class="field mt-8">
      <label>Выберите счётчик для замены</label>
      <select id="rep-meter-type">${optionsHtml}</select>
    </div>
    <div id="rep-values-wrap">${renderValueFields(firstDev)}</div>
    <div class="field">
      <label>Номер нового счётчика (необязательно)</label>
      <input type="text" id="rep-new-serial" maxlength="100" placeholder="Например: 87654321" />
    </div>
    <div class="field">
      <label>Новый срок поверки (необязательно, ДД/ММ/ГГГГ)</label>
      ${dateFieldHtml("rep-new-due", "rep-new-due-nat", "", "Срок поверки нового счётчика")}
    </div>
  `, [
    {
      label: "✅ Выполнить замену",
      onClick: async () => {
        const devMt = document.getElementById("rep-meter-type").value;
        const dueParsed = parseDisplayDate(document.getElementById("rep-new-due").value);
        if (!dueParsed.ok) {
          showToast(dueParsed.error, "error");
          return false;
        }
        const newSerial = (document.getElementById("rep-new-serial").value || "").trim() || null;
        const isMultiElec = devMt === "t" && multiElecEnabled.length > 0;

        let body;
        if (isMultiElec) {
          const entries = [];
          for (const mt of multiElecEnabled) {
            const meta = METER_META.find(m => m.id === mt) || { plain: mt };
            const oldP = parseIntSpaces(document.getElementById(`rep-old-${mt}`)?.value);
            if (oldP.empty || !oldP.ok) {
              showToast(`${meta.plain}: ${oldP.error || "укажите конечное показание старого счётчика"}`, "error");
              return false;
            }
            const newP = parseIntSpaces(document.getElementById(`rep-new-${mt}`)?.value);
            if (newP.empty || !newP.ok) {
              showToast(`${meta.plain}: ${newP.error || "укажите начальное показание нового счётчика"}`, "error");
              return false;
            }
            entries.push({ meter_type: mt, old_final_value: oldP.value, new_initial_value: newP.value });
          }
          body = {
            meter_type: "t",
            tariff_entries: entries,
            new_serial_number: newSerial,
            new_verification_due_date: dueParsed.display,
          };
        } else {
          const oldParsed = parseIntSpaces(document.getElementById("rep-old-final")?.value);
          if (oldParsed.empty || !oldParsed.ok) {
            showToast(oldParsed.error || "Укажите конечное показание старого счётчика", "error");
            return false;
          }
          const newParsed = parseIntSpaces(document.getElementById("rep-new-init")?.value);
          if (newParsed.empty || !newParsed.ok) {
            showToast(newParsed.error || "Укажите начальное показание нового счётчика", "error");
            return false;
          }
          body = {
            meter_type: devMt,
            old_final_value: oldParsed.value,
            new_initial_value: newParsed.value,
            new_serial_number: newSerial,
            new_verification_due_date: dueParsed.display,
          };
        }

        try {
          await api(`/api/apartments/${aptId}/meters/replace`, { method: "POST", body });
          showToast("✅ Замена счётчика зафиксирована");
          await loadApartmentDetail(aptId);
          await renderTab();
        } catch (err) {
          showToast("❌ " + err.message, "error");
          return false;
        }
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  attachDateField("rep-new-due", "rep-new-due-nat");
  document.getElementById("rep-meter-type")?.addEventListener("change", (e) => {
    const wrap = document.getElementById("rep-values-wrap");
    if (!wrap) return;
    wrap.innerHTML = renderValueFields(e.target.value);
    wrap.querySelectorAll(".int-mask").forEach(attachIntMask);
  });
}

function openEnabledMetersModal(aptId, activePropertyType) {
  const apt = state.aptDetail?.apartment;
  if (!apt || apt.id !== aptId) return;
  const ptype = activePropertyType || apt.property_type || "apartment";
  const pmeta = getPropertyMeta(ptype);
  const enabled = getEnabledMeters(apt);
  const initialIrrMode = apt.irrigation_mode || "parallel";

  const getSnap = () => JSON.stringify({
    meters: sel.getEnabledMeters(),
    irr: sel.getEnabledMeters().includes("irrigation") ? sel.getIrrigationMode() : "parallel",
  });

  let initialSnap = "";
  const doSave = async () => {
    const enabledMeters = sel.getEnabledMeters();
    if (!enabledMeters.length) {
      showToast("Выберите хотя бы один счётчик", "error");
      return false;
    }
    const irrMode = sel.getIrrigationMode();
    try {
      await api(`/api/apartments/${aptId}/settings`, {
        method: "PUT",
        body: {
          property_type: ptype,
          enabled_meters: enabledMeters,
          irrigation_mode: irrMode,
        },
      });
      showToast("✅ Состав счётчиков сохранён");
      const me = await api("/api/me");
      state.apartments = me.apartments;
      renderAptSelector();
      await loadApartmentDetail(aptId);
      await renderTab();
      return true;
    } catch (err) {
      showToast("❌ " + err.message, "error");
      return false;
    }
  };

  openModal(
    `🚰 Счётчики ${pmeta.inLocative} — ${propShort(apt)}`,
    renderMeterSelectorHtml("editm", enabled, ptype, initialIrrMode),
    [
      {
        id: "modal-save-enabled-meters",
        label: "💾 Сохранить",
        disabled: true,
        onClick: async () => {
          const enabledMeters = sel.getEnabledMeters();
          if (!enabledMeters.length) {
            showToast("Выберите хотя бы один счётчик", "error");
            return false;
          }
          const irrMode = sel.getIrrigationMode();
          const irrChanged = enabledMeters.includes("irrigation") && irrMode !== initialIrrMode;
          if (irrChanged && (state.aptDetail?.readings_count || 0) > 0) {
            closeModal(true);
            openModal("⚠️ Изменение схемы полива", `
              <p>По объекту уже есть сохранённые показания. Изменение схемы подключения полива повлияет на расчёт водоотведения за исторические периоды.</p>
              <p class="small muted">Вы уверены, что хотите сохранить изменения?</p>
            `, [
              { label: "💾 Сохранить", onClick: doSave },
              { label: "Отмена", cls: "btn-secondary" },
            ]);
            return false;
          }
          return await doSave();
        },
      },
      { label: "Отмена", cls: "btn-secondary" },
    ]
  );

  const saveBtn = document.getElementById("modal-save-enabled-meters");
  const updateDirty = () => {
    if (!saveBtn || !initialSnap) return;
    const ms = sel.getEnabledMeters();
    saveBtn.disabled = !ms.length || getSnap() === initialSnap;
  };
  const sel = wireMeterSelector("editm", detectElectricityMode(enabled), updateDirty, initialIrrMode);
  initialSnap = getSnap();
  updateDirty();
}

function openSubmissionDaysModal(aptId) {
  const apt = state.aptDetail?.apartment;
  if (!apt || apt.id !== aptId) return;
  const checked = new Set(getEnabledMeters(apt));
  const hasWaterAny = checked.has("cold_water") || checked.has("hot_water") || checked.has("irrigation");
  const hasE = checked.has("t") || checked.has("t1") || checked.has("t2") || checked.has("t3");
  const hasG = checked.has("gas");

  if (!hasWaterAny && !hasE && !hasG) {
    return showToast("Сначала выберите хотя бы один счётчик для объекта", "error");
  }

  const bodyHtml = `
    <p class="small muted">Выберите день отправки показаний счётчиков (с 1 по 31 число)</p>
    <div class="mt-8">
      ${hasWaterAny ? `<div class="field"><label>💧 Вода</label><input type="number" min="1" max="31" id="m-wday" placeholder="1–31" value="${apt.water_submission_day ?? ""}" /></div>` : ""}
      ${hasE ? `<div class="field"><label>⚡ Электричество</label><input type="number" min="1" max="31" id="m-eday" placeholder="1–31" value="${apt.electricity_submission_day ?? ""}" /></div>` : ""}
      ${hasG ? `<div class="field"><label>🔥 Газ</label><input type="number" min="1" max="31" id="m-gday" placeholder="1–31" value="${apt.gas_submission_day ?? ""}" /></div>` : ""}
    </div>
  `;

  const readSnap = () => JSON.stringify({
    w: hasWaterAny ? (document.getElementById("m-wday")?.value || "").trim() : null,
    e: hasE ? (document.getElementById("m-eday")?.value || "").trim() : null,
    g: hasG ? (document.getElementById("m-gday")?.value || "").trim() : null,
  });

  openModal(`📅 Дни подачи показаний — ${propShort(apt)}`, bodyHtml, [
    {
      id: "modal-save-days",
      label: "💾 Сохранить",
      disabled: true,
      onClick: async () => {
        const body = {};
        if (hasWaterAny) {
          const v = (document.getElementById("m-wday")?.value || "").trim();
          body.water_submission_day = v ? Number(v) : null;
        }
        if (hasE) {
          const v = (document.getElementById("m-eday")?.value || "").trim();
          body.electricity_submission_day = v ? Number(v) : null;
        }
        if (hasG) {
          const v = (document.getElementById("m-gday")?.value || "").trim();
          body.gas_submission_day = v ? Number(v) : null;
        }
        try {
          await api(`/api/apartments/${aptId}/settings`, { method: "PUT", body });
          showToast("✅ Дни подачи сохранены");
          const me = await api("/api/me");
          state.apartments = me.apartments;
          renderAptSelector();
          await loadApartmentDetail(aptId);
          await renderTab();
        } catch (err) {
          showToast("❌ " + err.message, "error");
          return false;
        }
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  const initialSnap = readSnap();
  const saveBtn = document.getElementById("modal-save-days");
  const updateDirty = () => {
    if (saveBtn) saveBtn.disabled = readSnap() === initialSnap;
  };
  ["m-wday", "m-eday", "m-gday"].forEach(id => {
    document.getElementById(id)?.addEventListener("input", updateDirty);
  });
}

function openTariffsModal(aptId) {
  const apt = state.aptDetail?.apartment;
  const tariffs = state.aptDetail?.tariffs || {};
  if (!apt || apt.id !== aptId) return;

  const checked = new Set(getEnabledMeters(apt));
  const hasDomW = checked.has("cold_water") || checked.has("hot_water");
  const isIrrAfterCold = (apt.irrigation_mode || "parallel") === "after_cold";
  const sewPlaceholder = checked.has("irrigation") && isIrrAfterCold
    ? (checked.has("hot_water") ? "ХВС + ГВС минус Полив" : "ХВС минус Полив")
    : (checked.has("hot_water") ? "ХВС + ГВС" : "ХВС");

  const fields = [];
  for (const m of METER_META.filter(x => x.id === "cold_water" || x.id === "hot_water")) {
    if (checked.has(m.id)) {
      fields.push(`<div class="field"><label>${esc(m.tariffLabel)}</label><input type="text" inputmode="decimal" id="t-${m.id}" value="${tariffs[m.id] ?? ""}" /></div>`);
    }
  }
  if (hasDomW) {
    fields.push(`<div class="field"><label>🚰 Водоотведение (руб/м³)</label><input type="text" inputmode="decimal" id="t-sewerage" placeholder="${esc(sewPlaceholder)}" value="${tariffs.sewerage ? tariffs.sewerage : ""}" /></div>`);
  }
  if (checked.has("irrigation") && !isIrrAfterCold) {
    const m = METER_META.find(x => x.id === "irrigation");
    fields.push(`<div class="field"><label>${esc(m.tariffLabel)}</label><input type="text" inputmode="decimal" id="t-${m.id}" value="${tariffs[m.id] ?? ""}" /></div>`);
  }
  for (const m of METER_META.filter(x => x.group !== "water")) {
    if (checked.has(m.id)) {
      fields.push(`<div class="field"><label>${esc(m.tariffLabel)}</label><input type="text" inputmode="decimal" id="t-${m.id}" value="${tariffs[m.id] ?? ""}" /></div>`);
    }
  }

  const bodyHtml = `
    <div style="margin-bottom:10px">
      <button type="button" class="btn btn-secondary" id="btn-tariff-hist">📋 История тарифов</button>
    </div>
    <div class="grid-2">${fields.join("") || '<p class="small muted">Нет подключённых счётчиков с отдельным тарифом.</p>'}</div>
  `;

  const readSnap = () => {
    const snap = {};
    for (const m of METER_META) {
      const el = document.getElementById(`t-${m.id}`);
      if (el) snap[m.id] = el.value.trim().replace(",", ".");
    }
    const sewEl = document.getElementById("t-sewerage");
    if (sewEl) snap.sewerage = sewEl.value.trim().replace(",", ".");
    return JSON.stringify(snap);
  };

  openModal(`💰 Тарифы — ${propShort(apt)}`, bodyHtml, [
    {
      id: "modal-save-tariffs",
      label: "💾 Сохранить",
      disabled: true,
      onClick: async () => {
        const tariffsPayload = {};
        for (const m of METER_META) {
          const el = document.getElementById(`t-${m.id}`);
          if (!el) continue;
          const val = el.value.trim();
          if (val) tariffsPayload[m.id] = val;
        }
        const sewEl = document.getElementById("t-sewerage");
        if (sewEl) {
          const sewVal = sewEl.value.trim();
          if (sewVal) {
            tariffsPayload.sewerage = sewVal;
          } else if (tariffs.sewerage !== null && tariffs.sewerage !== undefined && tariffs.sewerage > 0) {
            tariffsPayload.sewerage = "0";
          }
        }
        try {
          await api(`/api/apartments/${aptId}/settings`, {
            method: "PUT",
            body: { tariffs: tariffsPayload },
          });
          showToast("✅ Тарифы сохранены");
          await loadApartmentDetail(aptId);
          await renderTab();
        } catch (err) {
          showToast("❌ " + err.message, "error");
          return false;
        }
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  const initialSnap = readSnap();
  const saveBtn = document.getElementById("modal-save-tariffs");
  const updateDirty = () => {
    if (saveBtn) saveBtn.disabled = readSnap() === initialSnap;
  };
  ["t-sewerage", ...METER_META.map(m => `t-${m.id}`)].forEach(id => {
    document.getElementById(id)?.addEventListener("input", updateDirty);
  });

  document.getElementById("btn-tariff-hist")?.addEventListener("click", async () => {
    try {
      const res = await api(`/api/apartments/${aptId}/tariffs/history`);
      const html = res.items.length
        ? res.items.map(i => `<div class="table-row"><span>${esc(i.label)} · ${esc(i.effective_from)}</span><b>${fmtNum(i.rate)} ₽</b></div>`).join("")
        : `<p class="muted">История тарифов пуста.</p>`;
      closeModal(true);
      openModal(`История тарифов — ${propShort(apt)}`, `<div style="max-height:55vh;overflow:auto">${html}</div>`, [
        { label: "Закрыть", cls: "btn-secondary" },
      ]);
    } catch (err) {
      showToast("❌ " + err.message, "error");
    }
  });
}

// ── Properties (Недвижимость) ─────────────────────────────────────────
function detectElectricityMode(enabledMeters) {
  return enabledMeters.some(m => MULTI_ELEC_IDS.includes(m)) ? "multi" : "single";
}

const IRRIGATION_MODE_HINTS = {
  after_cold: "Цена воды на полив = цене ХВС (отдельный тариф не нужен). Водоотведение = ХВС минус Полив.",
  parallel: "Задаётся отдельный тариф «Полив» (руб/м³). Расчёт полива идёт отдельно, без учёта в водоотведении.",
};

function renderMeterSelectorHtml(prefix, enabledMeters, propertyType = "apartment", irrigationMode = "parallel") {
  const elecMode = detectElectricityMode(enabledMeters);
  const isHouse = propertyType === "house";
  const irrMode = irrigationMode === "after_cold" ? "after_cold" : "parallel";
  const showIrrMode = isHouse && enabledMeters.includes("irrigation");

  const waterGasHtml = WATER_GAS_METERS.map(m => {
    const hiddenByType = Boolean(m.houseOnly) && !isHouse;
    const isChecked = !hiddenByType && enabledMeters.includes(m.id);
    return `
      <label class="meter-check ${hiddenByType ? "hidden" : ""}" ${m.houseOnly ? `data-${prefix}-house-only="1"` : ""}>
        <input type="checkbox" class="${prefix}-meter-cb" value="${m.id}" ${isChecked ? "checked" : ""} ${hiddenByType ? "disabled" : ""} />
        <span>${esc(m.label)}</span>
      </label>
    `;
  }).join("");

  const elecChecksHtml = ELECTRICITY_METER_META.map(m => {
    const isSingle = m.id === "t";
    const isLocked = elecMode === "single" ? !isSingle : isSingle;
    const isChecked = !isLocked && enabledMeters.includes(m.id);
    return `
      <label class="meter-check ${isLocked ? "is-disabled" : ""}" data-${prefix}-elec-label="${m.id}">
        <input type="checkbox" class="${prefix}-meter-cb" value="${m.id}" ${isChecked ? "checked" : ""} ${isLocked ? "disabled" : ""} />
        <span>${esc(m.label)}</span>
      </label>
    `;
  }).join("");

  return `
    <div class="field">
      <label>Подключённые счётчики (вода и газ)</label>
      <div class="meter-check-grid">${waterGasHtml}</div>
    </div>
    <div class="field ${showIrrMode ? "" : "hidden"}" id="${prefix}-irr-mode-wrap">
      <label>🌱 Схема подключения поливного водомера</label>
      <div class="irr-mode-picker" id="${prefix}-irr-mode-picker">
        <button type="button" class="type-btn irr-mode-btn ${irrMode === "after_cold" ? "active" : ""}" data-${prefix}-irr-mode="after_cold">Поливной водомер после водомера ХВС</button>
        <button type="button" class="type-btn irr-mode-btn ${irrMode === "parallel" ? "active" : ""}" data-${prefix}-irr-mode="parallel">Поливной водомер параллельно водомеру ХВС</button>
      </div>
      <div class="irr-mode-hint" id="${prefix}-irr-mode-hint">${IRRIGATION_MODE_HINTS[irrMode]}</div>
    </div>
    <div class="field">
      <label>⚡ Тип показаний электричества</label>
      <div class="elec-mode-picker" id="${prefix}-elec-mode-picker">
        <button type="button" class="type-btn ${elecMode === "single" ? "active" : ""}" data-${prefix}-elec-mode="single">Один тариф</button>
        <button type="button" class="type-btn ${elecMode === "multi" ? "active" : ""}" data-${prefix}-elec-mode="multi">Тарифы Т1, Т2, Т3</button>
      </div>
      <div class="meter-check-grid-4">${elecChecksHtml}</div>
    </div>
  `;
}

function wireMeterSelector(prefix, initialMode, onChange, initialIrrMode = "parallel") {
  let currentMode = initialMode;
  let currentIrrMode = initialIrrMode === "after_cold" ? "after_cold" : "parallel";

  const applyMode = (mode, autoCheck) => {
    currentMode = mode;
    document.querySelectorAll(`#${prefix}-elec-mode-picker [data-${prefix}-elec-mode]`).forEach(btn => {
      btn.classList.toggle("active", btn.getAttribute(`data-${prefix}-elec-mode`) === mode);
    });

    const singleCb = document.querySelector(`.${prefix}-meter-cb[value="t"]`);
    const singleLabel = document.querySelector(`[data-${prefix}-elec-label="t"]`);
    const multiCbs = MULTI_ELEC_IDS.map(id => ({
      cb: document.querySelector(`.${prefix}-meter-cb[value="${id}"]`),
      label: document.querySelector(`[data-${prefix}-elec-label="${id}"]`),
    }));

    if (mode === "single") {
      if (singleCb) {
        singleCb.disabled = false;
        if (autoCheck) singleCb.checked = true;
      }
      singleLabel?.classList.remove("is-disabled");
      multiCbs.forEach(({ cb, label }) => {
        if (cb) {
          cb.checked = false;
          cb.disabled = true;
        }
        label?.classList.add("is-disabled");
      });
    } else {
      if (singleCb) {
        singleCb.checked = false;
        singleCb.disabled = true;
      }
      singleLabel?.classList.add("is-disabled");
      const anyMultiChecked = multiCbs.some(({ cb }) => cb?.checked);
      multiCbs.forEach(({ cb, label }) => {
        if (cb) {
          cb.disabled = false;
          if (autoCheck && !anyMultiChecked) cb.checked = true;
        }
        label?.classList.remove("is-disabled");
      });
    }
    onChange?.();
  };

  document.querySelectorAll(`#${prefix}-elec-mode-picker [data-${prefix}-elec-mode]`).forEach(btn => {
    btn.onclick = () => {
      const nextMode = btn.getAttribute(`data-${prefix}-elec-mode`);
      if (nextMode === currentMode) return;
      haptic("light");
      applyMode(nextMode, true);
    };
  });

  const syncIrrModeVisibility = () => {
    const irrCb = document.querySelector(`.${prefix}-meter-cb[value="irrigation"]`);
    const show = Boolean(irrCb && !irrCb.disabled && irrCb.checked);
    document.getElementById(`${prefix}-irr-mode-wrap`)?.classList.toggle("hidden", !show);
  };

  document.querySelectorAll(`#${prefix}-irr-mode-picker [data-${prefix}-irr-mode]`).forEach(btn => {
    btn.onclick = () => {
      const nextIrr = btn.getAttribute(`data-${prefix}-irr-mode`);
      if (nextIrr === currentIrrMode) return;
      haptic("light");
      currentIrrMode = nextIrr;
      document.querySelectorAll(`#${prefix}-irr-mode-picker [data-${prefix}-irr-mode]`).forEach(b => {
        b.classList.toggle("active", b.getAttribute(`data-${prefix}-irr-mode`) === currentIrrMode);
      });
      const hintEl = document.getElementById(`${prefix}-irr-mode-hint`);
      if (hintEl) hintEl.textContent = IRRIGATION_MODE_HINTS[currentIrrMode] || "";
      onChange?.();
    };
  });

  document.querySelectorAll(`.${prefix}-meter-cb`).forEach(cb => {
    cb.onchange = () => {
      syncIrrModeVisibility();
      onChange?.();
    };
  });

  const setPropertyType = (ptype) => {
    const isHouse = ptype === "house";
    document.querySelectorAll(`[data-${prefix}-house-only="1"]`).forEach(label => {
      label.classList.toggle("hidden", !isHouse);
      const cb = label.querySelector("input[type='checkbox']");
      if (cb) {
        cb.disabled = !isHouse;
        if (!isHouse) cb.checked = false;
      }
    });
    syncIrrModeVisibility();
    onChange?.();
  };

  syncIrrModeVisibility();

  return {
    getMode: () => currentMode,
    getIrrigationMode: () => currentIrrMode,
    setPropertyType,
    getEnabledMeters: () =>
      Array.from(document.querySelectorAll(`.${prefix}-meter-cb:checked:not(:disabled)`)).map(cb => cb.value),
  };
}

function openCreatePropertyModal() {
  let chosenType = "apartment";
  const initMeters = DEFAULT_NEW_METERS.slice();

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
    ${renderMeterSelectorHtml("new", initMeters, chosenType)}
  `, [
    {
      id: "modal-create-apt-btn",
      label: "Создать",
      disabled: true,
      onClick: async () => {
        const number = document.getElementById("new-apt-num").value.trim();
        if (!number) { showToast("Укажите номер или название объекта", "error"); return false; }
        const enabledMeters = meterSelector.getEnabledMeters();
        if (!enabledMeters.length) { showToast("Выберите хотя бы один счётчик", "error"); return false; }
        try {
          const res = await api("/api/apartments", {
            method: "POST",
            body: {
              number,
              property_type: chosenType,
              enabled_meters: enabledMeters,
              irrigation_mode: meterSelector.getIrrigationMode(),
            },
          });
          state.selectedAptId = res.apartment.id;
          showToast(`✅ Создано: ${propTitle(res.apartment)}`);
          await loadBootstrap();
        } catch (err) { showToast("❌ " + err.message, "error"); return false; }
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);

  const updateCreateBtn = () => {
    const createBtn = document.getElementById("modal-create-apt-btn");
    const numVal = document.getElementById("new-apt-num")?.value.trim() || "";
    const meters = meterSelector.getEnabledMeters();
    if (createBtn) createBtn.disabled = !numVal || !meters.length;
  };

  const meterSelector = wireMeterSelector("new", detectElectricityMode(initMeters), updateCreateBtn);
  document.getElementById("new-apt-num")?.addEventListener("input", updateCreateBtn);

  document.querySelectorAll("#new-type-picker .type-btn").forEach(btn => {
    btn.onclick = () => {
      chosenType = btn.dataset.ptype;
      document.querySelectorAll("#new-type-picker .type-btn").forEach(b => b.classList.toggle("active", b === btn));
      const meta = getPropertyMeta(chosenType);
      document.getElementById("new-prop-num-label").textContent = meta.numLabel;
      document.getElementById("new-apt-num").placeholder = meta.placeholder;
      meterSelector.setPropertyType(chosenType);
    };
  });
}

async function saveApartmentOrder(newIds, root) {
  const prevApartments = state.apartments.slice();
  const byId = new Map(state.apartments.map(a => [a.id, a]));
  const reordered = newIds.map(id => byId.get(id)).filter(Boolean);
  if (reordered.length !== state.apartments.length) {
    for (const a of state.apartments) {
      if (!newIds.includes(a.id)) reordered.push(a);
    }
  }
  state.apartments = reordered;
  renderAptSelector();

  try {
    await api("/api/apartments/reorder", {
      method: "PUT",
      body: { apartment_ids: reordered.map(a => a.id) },
    });
    showToast("✅ Порядок объектов сохранён");
  } catch (err) {
    state.apartments = prevApartments;
    renderAptSelector();
    renderTab();
    showToast("❌ " + err.message, "error");
  }
}

async function moveSelectedApartment(delta, root) {
  const ids = state.apartments.map(a => a.id);
  const idx = ids.indexOf(state.selectedAptId);
  if (idx === -1) return;
  const targetIdx = idx + delta;
  if (targetIdx < 0 || targetIdx >= ids.length) return;
  const [moved] = ids.splice(idx, 1);
  ids.splice(targetIdx, 0, moved);
  haptic("light");
  await saveApartmentOrder(ids, root);
  renderApartments(root);
}

function initPropertyDragAndDrop(container, root) {
  const getOrder = () => Array.from(container.querySelectorAll(".apt-chip")).map(el => Number(el.dataset.apt));

  container.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".apt-chip")) e.preventDefault();
  });

  container.addEventListener("dragstart", (e) => {
    e.preventDefault();
  });

  let activeDrag = null;
  let lastTouchEndAt = 0;

  const startDrag = (chip, cx, cy) => {
    if (!activeDrag || activeDrag.dragging) return;
    activeDrag.dragging = true;
    activeDrag.initialOrder = getOrder();
    activeDrag.lastSwapAt = 0;

    const rect = chip.getBoundingClientRect();
    activeDrag.offsetX = cx - rect.left;
    activeDrag.offsetY = cy - rect.top;

    const ghost = chip.cloneNode(true);
    ghost.classList.add("apt-chip-ghost");
    ghost.style.pointerEvents = "none";
    ghost.style.left = `${rect.left}px`;
    ghost.style.top = `${rect.top}px`;
    ghost.style.width = `${rect.width}px`;
    ghost.style.height = `${rect.height}px`;
    document.body.appendChild(ghost);
    activeDrag.ghost = ghost;

    chip.classList.add("apt-chip-placeholder");
    document.body.classList.add("is-dragging-apt");
    try { tg?.disableVerticalSwipes?.(); } catch (_) {}
    haptic("medium");
  };

  const reorderAtPoint = (chip, cx, cy) => {
    const now = Date.now();
    if (activeDrag && now - activeDrag.lastSwapAt < 55) return;

    const chips = Array.from(container.querySelectorAll(".apt-chip"));
    if (chips.length < 2) return;

    const dragIdx = chips.indexOf(chip);
    if (dragIdx === -1) return;
    const dragRect = chip.getBoundingClientRect();

    let target = null;
    let targetRect = null;
    for (const candidate of chips) {
      if (candidate === chip) continue;
      const r = candidate.getBoundingClientRect();
      if (cx >= r.left - 5 && cx <= r.right + 5 && cy >= r.top - 5 && cy <= r.bottom + 5) {
        target = candidate;
        targetRect = r;
        break;
      }
    }

    if (target && targetRect) {
      const targetIdx = chips.indexOf(target);
      const targetCenterX = targetRect.left + targetRect.width / 2;
      const sameRow = Math.abs(targetRect.top - dragRect.top) <= targetRect.height * 0.55;

      let moved = false;
      if (sameRow) {
        if (dragIdx < targetIdx && cx > targetRect.left + targetRect.width * 0.42) {
          container.insertBefore(chip, target.nextSibling);
          moved = true;
        } else if (dragIdx > targetIdx && cx < targetRect.left + targetRect.width * 0.58) {
          container.insertBefore(chip, target);
          moved = true;
        }
      } else {
        if (cx < targetCenterX) {
          if (chip.nextSibling !== target) {
            container.insertBefore(chip, target);
            moved = true;
          }
        } else {
          if (target.nextSibling !== chip) {
            container.insertBefore(chip, target.nextSibling);
            moved = true;
          }
        }
      }
      if (moved) {
        if (activeDrag) activeDrag.lastSwapAt = now;
        haptic("light");
      }
      return;
    }

    const firstChip = chips[0];
    const lastChip = chips[chips.length - 1];
    if (firstChip && firstChip !== chip) {
      const fr = firstChip.getBoundingClientRect();
      if (cy <= fr.bottom && cx < fr.left) {
        container.insertBefore(chip, firstChip);
        if (activeDrag) activeDrag.lastSwapAt = now;
        haptic("light");
        return;
      }
    }
    if (lastChip && lastChip !== chip) {
      const lr = lastChip.getBoundingClientRect();
      if ((cy >= lr.top && cx > lr.right) || cy > lr.bottom + 4) {
        container.appendChild(chip);
        if (activeDrag) activeDrag.lastSwapAt = now;
        haptic("light");
      }
    }
  };

  const finishDrag = (commit) => {
    if (!activeDrag) return;
    const d = activeDrag;
    activeDrag = null;

    if (d.holdTimer) clearTimeout(d.holdTimer);
    if (d.cleanup) d.cleanup();

    if (!d.dragging) return;

    d.ghost?.remove();
    d.chip.classList.remove("apt-chip-placeholder");
    document.body.classList.remove("is-dragging-apt");
    if (!state.reorderMode) {
      try { tg?.enableVerticalSwipes?.(); } catch (_) {}
    }

    container.dataset.justDragged = "1";
    setTimeout(() => { delete container.dataset.justDragged; }, 140);

    if (commit) {
      const newOrder = getOrder();
      if (newOrder.join(",") !== d.initialOrder.join(",")) {
        saveApartmentOrder(newOrder, root);
      }
    }
  };

  const findTouch = (list, id) => {
    for (let i = 0; i < list.length; i++) {
      if (list[i].identifier === id) return list[i];
    }
    return null;
  };

  container.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (Date.now() - lastTouchEndAt < 500) return;
    const chip = e.target.closest(".apt-chip");
    if (!chip || !container.contains(chip)) return;

    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;

    const onMouseMove = (ev) => {
      if (!activeDrag || activeDrag.mode !== "mouse") return;
      const dist = Math.hypot(ev.clientX - startX, ev.clientY - startY);
      if (!activeDrag.dragging && dist >= (state.reorderMode ? 3 : 5)) {
        startDrag(chip, ev.clientX, ev.clientY);
      }
      if (activeDrag.dragging && activeDrag.ghost) {
        ev.preventDefault();
        activeDrag.ghost.style.left = `${ev.clientX - activeDrag.offsetX}px`;
        activeDrag.ghost.style.top = `${ev.clientY - activeDrag.offsetY}px`;
        reorderAtPoint(chip, ev.clientX, ev.clientY);
      }
    };

    const onMouseUp = () => finishDrag(true);

    activeDrag = {
      mode: "mouse",
      chip,
      dragging: false,
      ghost: null,
      holdTimer: null,
      initialOrder: [],
      offsetX: 0,
      offsetY: 0,
      lastSwapAt: 0,
      cleanup: () => {
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        window.removeEventListener("blur", onMouseUp);
      },
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    window.addEventListener("blur", onMouseUp);
  });

  container.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    const chip = e.target.closest(".apt-chip");
    if (!chip || !container.contains(chip)) return;

    const touch = e.touches[0];
    const touchId = touch.identifier;
    const startX = touch.clientX;
    const startY = touch.clientY;

    activeDrag = {
      mode: "touch",
      touchId,
      startX,
      startY,
      lastX: startX,
      lastY: startY,
      chip,
      dragging: false,
      ghost: null,
      holdTimer: null,
      initialOrder: [],
      offsetX: 0,
      offsetY: 0,
      lastSwapAt: 0,
      cleanup: null,
    };

    if (!state.reorderMode) {
      activeDrag.holdTimer = setTimeout(() => {
        if (activeDrag && activeDrag.mode === "touch" && !activeDrag.dragging) {
          activeDrag.holdTimer = null;
          startDrag(chip, activeDrag.lastX, activeDrag.lastY);
        }
      }, 220);
    }
  }, { passive: true });

  container.addEventListener("touchmove", (e) => {
    if (!activeDrag || activeDrag.mode !== "touch") return;
    const t = findTouch(e.touches, activeDrag.touchId);
    if (!t) return;
    activeDrag.lastX = t.clientX;
    activeDrag.lastY = t.clientY;
    const dist = Math.hypot(t.clientX - activeDrag.startX, t.clientY - activeDrag.startY);

    if (!activeDrag.dragging) {
      if (state.reorderMode) {
        e.preventDefault();
        if (dist >= 4) {
          startDrag(activeDrag.chip, t.clientX, t.clientY);
        }
      } else if (dist > 10) {
        if (activeDrag.holdTimer) {
          clearTimeout(activeDrag.holdTimer);
          activeDrag.holdTimer = null;
        }
      }
    }

    if (activeDrag.dragging && activeDrag.ghost) {
      e.preventDefault();
      activeDrag.ghost.style.left = `${t.clientX - activeDrag.offsetX}px`;
      activeDrag.ghost.style.top = `${t.clientY - activeDrag.offsetY}px`;
      reorderAtPoint(activeDrag.chip, t.clientX, t.clientY);
    }
  }, { passive: false });

  container.addEventListener("touchend", (e) => {
    if (!activeDrag || activeDrag.mode !== "touch") return;
    if (!findTouch(e.changedTouches, activeDrag.touchId)) return;
    lastTouchEndAt = Date.now();
    finishDrag(true);
  });

  container.addEventListener("touchcancel", () => {
    if (!activeDrag || activeDrag.mode !== "touch") return;
    lastTouchEndAt = Date.now();
    finishDrag(false);
  });
}

function renderApartments(root) {
  const apt = state.aptDetail?.apartment;
  const tariffs = state.aptDetail?.tariffs || {};
  const initR = state.aptDetail?.initial_readings || {};
  const tenants = state.aptDetail?.tenants || [];
  const enabled = apt ? getEnabledMeters(apt) : [];

  const canReorder = state.apartments.length > 1;
  if (!canReorder) state.reorderMode = false;

  const aptListChips = state.apartments.map(a => `
    <button type="button" class="btn btn-sm apt-chip ${a.id === state.selectedAptId ? "" : "btn-secondary"}" data-apt="${a.id}">
      ${state.reorderMode ? '<span class="drag-grip">⠿</span>' : ""}${esc(propIcon(a))} ${esc(propShort(a))}${isPropertyRented(a) ? " 👤" : ""}
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
        <button class="btn" id="btn-save-basic" disabled>💾 Сохранить</button>
        <div class="settings-menu-stack">
          <button type="button" class="btn btn-secondary settings-menu-btn" id="btn-open-enabled-meters">
            <span id="lbl-open-enabled-meters">🚰 Имеющиеся счётчики воды и газа ${curMeta.inLocative}</span>
            <span class="settings-menu-arrow">›</span>
          </button>
          <button type="button" class="btn btn-secondary settings-menu-btn" id="btn-meters-manage">
            <span>🔧 Управление счётчиками</span>
            <span class="settings-menu-arrow">›</span>
          </button>
          <button type="button" class="btn btn-secondary settings-menu-btn" id="btn-open-days">
            <span>📅 Дни подачи показаний</span>
            <span class="settings-menu-arrow">›</span>
          </button>
          <button type="button" class="btn btn-secondary settings-menu-btn" id="btn-open-tariffs">
            <span>💰 Тарифы</span>
            <span class="settings-menu-arrow">›</span>
          </button>
        </div>
        <div class="danger-zone">
          <button class="btn btn-danger" id="btn-delete-property">🗑 Удалить объект недвижимости</button>
        </div>`;
    } else if (state.manageSubtab === "initial") {
      const enabledMeta = METER_META.filter(m => enabled.includes(m.id));
      bodyHtml = `
        <p class="small muted">Оставьте поле пустым, если начальное значение ещё не задано (укажите 0, если счётчик новый).</p>
        <div class="grid-2 mt-8">
          ${enabledMeta.map(m => `
            <div class="field">
              <label>${esc(m.label)} (${esc(m.unit)})</label>
              <input type="text" inputmode="numeric" autocomplete="off" class="int-mask" id="i-${m.id}" placeholder="Не задано" value="${initR[m.id] !== null && initR[m.id] !== undefined ? esc(formatIntSpaces(initR[m.id])) : ""}" />
            </div>
          `).join("")}
        </div>
        <button class="btn" id="btn-save-initial" disabled>💾 Сохранить начальные показания</button>`;
    } else {
      bodyHtml = `
        <div class="field"><label><input type="checkbox" id="c-has" ${apt.has_tenant ? "checked" : ""} style="width:auto;margin-right:6px" /> Наниматель проживает</label></div>
        <div class="field"><label>🔢 Номер договора</label><input type="text" id="c-no" value="${esc(apt.agreement_no || "")}" /></div>
        <div class="grid-2">
          <div class="field">
            <label>📅 Дата начала</label>
            ${dateFieldHtml("c-start", "c-start-native", apt.agreement_date_start || "", "Выбрать дату начала")}
          </div>
          <div class="field">
            <label>📅 Дата окончания</label>
            ${dateFieldHtml("c-end", "c-end-native", apt.agreement_date_finish || "", "Выбрать дату окончания")}
          </div>
        </div>
        <div class="grid-2">
          <button class="btn" id="btn-save-contract" disabled>💾 Сохранить договор</button>
          <button class="btn btn-danger" id="btn-del-contract">🗑 Удалить договор</button>
        </div>
        <h3 class="mt-12">👤 Наниматели</h3>
        ${tenants.length ? tenants.map(t => `
          <div class="table-row">
            <div><div class="bold">${esc(t.name || "—")}</div><div class="small muted">ID: ${t.telegram_id}${t.phone ? " · " + esc(t.phone) : ""}</div></div>
            <button class="btn btn-secondary btn-sm btn-del-icon del-tenant-btn" data-tid="${t.telegram_id}" title="Удалить"><span class="del-x">❌</span></button>
          </div>`).join("") : `<p class="small muted">Нанимателей пока нет.</p>`}
        <button class="btn btn-secondary mt-8" id="btn-add-tenant">➕ Добавить нанимателя</button>`;
    }
  }

  root.innerHTML = `
    <div class="card">
      <div class="row-between">
        <h2>🌆 Недвижимость</h2>
        <div class="row-nowrap">
          ${canReorder ? `<button type="button" class="btn btn-sm ${state.reorderMode ? "" : "btn-secondary"}" id="btn-toggle-reorder" title="Изменить порядок объектов">${state.reorderMode ? "✓ Готово" : "⇅"}</button>` : ""}
          <button type="button" class="btn btn-sm" id="btn-new-apt">➕ Добавить</button>
        </div>
      </div>
      <div id="apt-chips-list" class="apt-chips-list mt-8 ${state.reorderMode ? "reorder-mode" : ""}">${aptListChips || '<span class="muted small">Пока нет объектов недвижимости</span>'}</div>
      ${canReorder && state.reorderMode ? `
        <div class="reorder-controls">
          <button type="button" class="btn btn-secondary btn-sm" id="btn-move-left" style="flex:1" ${state.apartments.findIndex(a => a.id === state.selectedAptId) <= 0 ? "disabled" : ""}>◀ Влево</button>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-move-right" style="flex:1" ${state.apartments.findIndex(a => a.id === state.selectedAptId) >= state.apartments.length - 1 ? "disabled" : ""}>Вправо ▶</button>
        </div>` : ""}
      ${canReorder ? `<div class="reorder-hint">${state.reorderMode ? "Перетаскивайте объекты или двигайте выбранный кнопками ◀ ▶" : "Зажмите и перетащите объект или нажмите ⇅"}</div>` : ""}
    </div>
    ${apt ? `
      <div class="card">
        <h3>${esc(propIcon(apt))} ${esc(propTitle(apt))}</h3>
        <div class="subtabs">
          <button class="subtab ${state.manageSubtab === "settings" ? "active" : ""}" data-m="settings">⚙️ Настройки</button>
          <button class="subtab ${state.manageSubtab === "initial" ? "active" : ""}" data-m="initial">📏 Начальные</button>
          <button class="subtab ${state.manageSubtab === "contract" ? "active" : ""}" data-m="contract">📋 Аренда${isPropertyRented(apt) ? " ✅" : ""}</button>
        </div>
        ${bodyHtml}
      </div>` : ""}
    ${state.user.role === "landlord" && apt ? renderExportCardHtml(apt) : ""}
  `;

  if (state.user.role === "landlord" && apt) {
    bindExportButtons(apt);
  }

  document.getElementById("btn-toggle-reorder")?.addEventListener("click", () => {
    haptic("light");
    state.reorderMode = !state.reorderMode;
    try {
      if (state.reorderMode) tg?.disableVerticalSwipes?.();
      else tg?.enableVerticalSwipes?.();
    } catch (_) {}
    renderApartments(root);
  });

  const chipsContainer = document.getElementById("apt-chips-list");
  if (canReorder && chipsContainer) {
    initPropertyDragAndDrop(chipsContainer, root);
  }

  document.getElementById("btn-move-left")?.addEventListener("click", () => moveSelectedApartment(-1, root));
  document.getElementById("btn-move-right")?.addEventListener("click", () => moveSelectedApartment(1, root));

  root.querySelectorAll("[data-apt]").forEach(btn => {
    btn.onclick = () => {
      if (chipsContainer?.dataset.justDragged === "1") return;
      selectApartment(Number(btn.dataset.apt));
    };
  });
  root.querySelectorAll("[data-m]").forEach(btn => {
    btn.onclick = () => { state.manageSubtab = btn.dataset.m; renderApartments(root); };
  });

  document.getElementById("btn-new-apt").onclick = openCreatePropertyModal;

  if (!apt) return;

  if (state.manageSubtab === "settings") {
    let editPropType = apt.property_type || "apartment";
    const saveBasicBtn = document.getElementById("btn-save-basic");
    const initBasicSnap = JSON.stringify({
      number: String(apt.number || "").trim(),
      property_type: editPropType,
    });
    const updateBasicDirty = () => {
      if (!saveBasicBtn) return;
      const curNum = (document.getElementById("s-number")?.value || "").trim();
      const curSnap = JSON.stringify({ number: curNum, property_type: editPropType });
      saveBasicBtn.disabled = !curNum || curSnap === initBasicSnap;
    };

    document.getElementById("s-number")?.addEventListener("input", updateBasicDirty);

    document.querySelectorAll("#edit-type-picker .type-btn").forEach(btn => {
      btn.onclick = () => {
        editPropType = btn.dataset.editPtype;
        document.querySelectorAll("#edit-type-picker .type-btn").forEach(b => b.classList.toggle("active", b === btn));
        const meta = getPropertyMeta(editPropType);
        document.getElementById("edit-prop-num-label").textContent = meta.numLabel;
        document.getElementById("s-number").placeholder = meta.placeholder;
        const lbl = document.getElementById("lbl-open-enabled-meters");
        if (lbl) lbl.textContent = `🚰 Имеющиеся счётчики воды и газа ${meta.inLocative}`;
        updateBasicDirty();
      };
    });

    saveBasicBtn?.addEventListener("click", async () => {
      if (saveBasicBtn.disabled) return;
      const number = document.getElementById("s-number").value.trim();
      if (!number) return showToast("Укажите номер или название объекта", "error");
      const body = { number, property_type: editPropType };
      if (editPropType !== "house" && enabled.includes("irrigation")) {
        const filtered = enabled.filter(m => m !== "irrigation");
        body.enabled_meters = filtered.length ? filtered : ["cold_water"];
        body.irrigation_mode = "parallel";
      }
      saveBasicBtn.disabled = true;
      try {
        await api(`/api/apartments/${apt.id}/settings`, { method: "PUT", body });
        showToast("✅ Настройки объекта сохранены");
        const me = await api("/api/me");
        state.apartments = me.apartments;
        renderAptSelector();
        await loadApartmentDetail(apt.id);
        renderApartments(root);
      } catch (err) {
        showToast("❌ " + err.message, "error");
        updateBasicDirty();
      }
    });

    document.getElementById("btn-open-enabled-meters")?.addEventListener("click", () => {
      openEnabledMetersModal(apt.id, editPropType);
    });
    document.getElementById("btn-meters-manage")?.addEventListener("click", () => openMetersModal(apt.id));
    document.getElementById("btn-open-days")?.addEventListener("click", () => openSubmissionDaysModal(apt.id));
    document.getElementById("btn-open-tariffs")?.addEventListener("click", () => openTariffsModal(apt.id));

    document.getElementById("btn-delete-property")?.addEventListener("click", () => {
      const rCount = state.aptDetail?.readings_count ?? 0;
      const tCount = tenants.length;
      const details = [
        `показания счётчиков (${rCount} шт.)`,
        "тарифы, начальные показания и данные договора",
      ];
      const tenantsWarn = tCount > 0
        ? `<p class="small muted">Наниматели (${tCount} чел.) будут откреплены и получат уведомление в Telegram.</p>`
        : "";

      openModal("🗑 Удалить объект недвижимости?", `
        <p>Удалить <b>${esc(propIcon(apt))} ${esc(propTitle(apt))}</b>?</p>
        <p class="small muted">Будут безвозвратно удалены: ${details.join(", ")}.</p>
        ${tenantsWarn}
        <p class="small muted">Если история ещё может пригодиться, сначала сделайте выгрузку в Excel или PDF.</p>
      `, [
        {
          label: "🗑 Удалить навсегда",
          cls: "btn-danger",
          onClick: async () => {
            try {
              const res = await api(`/api/apartments/${apt.id}`, { method: "DELETE" });
              showToast(`✅ Объект «${res.deleted || propShort(apt)}» удалён`);
              state.selectedAptId = null;
              state.aptDetail = null;
              state.historyMonth = "";
              await loadBootstrap(false);
            } catch (err) {
              showToast("❌ " + err.message, "error");
              return false;
            }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    });
  }

  const saveInitialBtn = document.getElementById("btn-save-initial");
  if (saveInitialBtn) {
    for (const m of METER_META) {
      attachIntMask(document.getElementById(`i-${m.id}`));
    }
    const getInitialSnapshot = () => {
      const snap = {};
      for (const m of METER_META) {
        const el = document.getElementById(`i-${m.id}`);
        if (el) snap[m.id] = el.value.trim();
      }
      return JSON.stringify(snap);
    };
    const initialSnap = getInitialSnapshot();
    const updateInitialDirty = () => {
      let allValid = true;
      for (const m of METER_META) {
        const el = document.getElementById(`i-${m.id}`);
        if (!el) continue;
        const p = parseIntSpaces(el.value);
        if (!p.empty && !p.ok) { allValid = false; break; }
      }
      saveInitialBtn.disabled = !allValid || getInitialSnapshot() === initialSnap;
    };
    for (const m of METER_META) {
      document.getElementById(`i-${m.id}`)?.addEventListener("input", updateInitialDirty);
    }

    const doSaveInitial = async (body) => {
      saveInitialBtn.disabled = true;
      try {
        await api(`/api/apartments/${apt.id}/initial-readings`, { method: "PUT", body });
        showToast("✅ Начальные показания сохранены");
        await loadApartmentDetail(apt.id);
        renderApartments(root);
      } catch (err) {
        showToast("❌ " + err.message, "error");
        updateInitialDirty();
      }
    };

    saveInitialBtn.addEventListener("click", async () => {
      if (saveInitialBtn.disabled) return;
      const body = {};
      for (const m of METER_META) {
        const el = document.getElementById(`i-${m.id}`);
        if (!el) continue;
        const parsed = parseIntSpaces(el.value);
        if (parsed.empty) {
          body[m.id] = null;
        } else if (!parsed.ok) {
          return showToast(`Некорректное начальное показание для ${m.plain}: ${parsed.error}`, "error");
        } else {
          body[m.id] = parsed.value;
        }
      }
      if ((state.aptDetail?.readings_count || 0) > 0) {
        openModal("⚠️ Изменение начальных показаний", `
          <p>По объекту уже подавались показания. Изменение начальных показаний повлияет на расчёт первого периода.</p>
          <p class="small muted">Подтвердить изменение?</p>
        `, [
          { label: "Сохранить", onClick: () => doSaveInitial(body) },
          { label: "Отмена", cls: "btn-secondary" },
        ]);
        return;
      }
      await doSaveInitial(body);
    });
  }

  attachDateField("c-start", "c-start-native");
  attachDateField("c-end", "c-end-native");

  const saveContractBtn = document.getElementById("btn-save-contract");
  if (saveContractBtn) {
    const getContractSnapshot = () => JSON.stringify({
      has_tenant: Boolean(document.getElementById("c-has")?.checked),
      agreement_no: (document.getElementById("c-no")?.value || "").trim(),
      start: (document.getElementById("c-start")?.value || "").trim(),
      end: (document.getElementById("c-end")?.value || "").trim(),
    });
    const initialContractSnap = getContractSnapshot();
    const updateContractDirty = () => {
      saveContractBtn.disabled = getContractSnapshot() === initialContractSnap;
    };
    document.getElementById("c-has")?.addEventListener("change", updateContractDirty);
    document.getElementById("c-no")?.addEventListener("input", updateContractDirty);
    document.getElementById("c-start")?.addEventListener("input", updateContractDirty);
    document.getElementById("c-end")?.addEventListener("input", updateContractDirty);

    saveContractBtn.addEventListener("click", async () => {
      if (saveContractBtn.disabled) return;
      const startDate = parseDisplayDate(document.getElementById("c-start").value);
      if (!startDate.ok) return showToast(`❌ Дата начала: ${startDate.error}`, "error");
      const endDate = parseDisplayDate(document.getElementById("c-end").value);
      if (!endDate.ok) return showToast(`❌ Дата окончания: ${endDate.error}`, "error");
      if (startDate.iso && endDate.iso && startDate.iso > endDate.iso) {
        return showToast("❌ Дата начала договора не может быть позже даты окончания", "error");
      }
      saveContractBtn.disabled = true;
      try {
        await api(`/api/apartments/${apt.id}/contract`, {
          method: "PUT",
          body: {
            has_tenant: document.getElementById("c-has").checked,
            agreement_no: document.getElementById("c-no").value.trim() || null,
            agreement_date_start: startDate.display,
            agreement_date_finish: endDate.display,
          },
        });
        showToast("✅ Договор сохранён");
        await loadApartmentDetail(apt.id);
        renderApartments(root);
      } catch (err) {
        showToast("❌ " + err.message, "error");
        updateContractDirty();
      }
    });
  }

  document.getElementById("btn-del-contract")?.addEventListener("click", () => {
    const runDeleteContract = async (unlinkTenants) => {
      try {
        await api(`/api/apartments/${apt.id}/contract?unlink_tenants=${unlinkTenants ? "true" : "false"}`, {
          method: "DELETE",
        });
        showToast(unlinkTenants ? "✅ Договор удалён, наниматели откреплены" : "✅ Договор удалён");
        await loadApartmentDetail(apt.id);
        renderApartments(root);
      } catch (err) { showToast("❌ " + err.message, "error"); return false; }
    };

    if (tenants.length > 0) {
      openModal("🗑 Удалить договор?", `
        <p>К объекту «${esc(propShort(apt))}» привязаны наниматели (${tenants.length} чел.).</p>
        <p class="small muted">Выберите, как поступить с доступом нанимателей к боту:</p>
      `, [
        {
          label: "Удалить только договор\n(доступ нанимателя к боту сохранится)",
          cls: "btn-secondary",
          onClick: () => runDeleteContract(false),
        },
        {
          label: "Удалить договор и открепить нанимателей",
          cls: "btn-danger",
          onClick: () => runDeleteContract(true),
        },
        { label: "Отмена", cls: "btn-secondary" },
      ], { stackActions: true });
    } else {
      openModal("🗑 Удалить договор?", `<p>Очистить данные договора для объекта «${esc(propShort(apt))}»?</p>`, [
        { label: "Удалить", cls: "btn-danger", onClick: () => runDeleteContract(false) },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    }
  });

  document.getElementById("btn-add-tenant")?.addEventListener("click", () => {
    openAddTenantModal(apt.id);
  });

  root.querySelectorAll(".del-tenant-btn").forEach(btn => {
    btn.onclick = () => {
      const tid = Number(btn.dataset.tid);
      openModal("Удалить нанимателя?", `<p>Отозвать доступ у ID ${tid} к объекту «${esc(propShort(apt))}»?</p>`, [
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
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    };
  });
}

function openAddTenantModal(defaultAptId = null, onSuccess = null) {
  const showAptPicker = !defaultAptId && state.apartments.length > 0;
  const aptPickerHtml = showAptPicker
    ? `
      <div class="field">
        <label>Объект недвижимости</label>
        <select id="nt-apt-id">
          ${state.apartments.map(a => `<option value="${a.id}" ${a.id === state.selectedAptId ? "selected" : ""}>${esc(propIcon(a))} ${esc(propShort(a))}</option>`).join("")}
        </select>
      </div>`
    : "";

  openModal("👤 Добавить нанимателя", `
    ${aptPickerHtml}
    <div class="field">
      <label>Telegram ID</label>
      <input type="number" id="nt-id" placeholder="Например: 123456789" />
      <div class="small muted">Telegram ID можно взять в боте <a href="https://t.me/userinfobot" target="_blank" rel="noopener" class="tg-link" data-tg-link="https://t.me/userinfobot">@userinfobot</a></div>
    </div>
    <div class="field"><label>Имя</label><input type="text" id="nt-name" placeholder="Введите имя" /></div>
    <div class="field"><label>Телефон (необязательно)</label><input type="tel" inputmode="tel" id="nt-phone" class="phone-mask" placeholder="+7-___-___-____" /></div>
    <p class="small muted">Бот сможет отправить приглашение только если наниматель ранее уже нажимал /start.</p>
  `, [
    {
      label: "Добавить",
      onClick: async () => {
        const targetAptId = defaultAptId || Number(document.getElementById("nt-apt-id")?.value);
        if (!targetAptId) { showToast("Выберите объект недвижимости", "error"); return false; }
        const tgId = Number(document.getElementById("nt-id").value);
        const name = document.getElementById("nt-name").value.trim();
        if (!Number.isSafeInteger(tgId) || tgId <= 0) { showToast("Введите корректный Telegram ID", "error"); return false; }
        if (!name) { showToast("Введите имя нанимателя", "error"); return false; }
        const ph = readMaskedPhone("nt-phone");
        if (!ph.ok) { showToast(ph.error, "error"); return false; }
        return await submitAddTenant(targetAptId, {
          telegram_id: tgId,
          name,
          phone: ph.value,
          force_no_contract: false,
        }, onSuccess);
      },
    },
    { label: "Отмена", cls: "btn-secondary" },
  ]);
}

async function submitAddTenant(aptId, payload, onSuccess = null) {
  try {
    const res = await api(`/api/apartments/${aptId}/tenants`, { method: "POST", body: payload });
    if (res.needs_confirm) {
      setTimeout(() => {
        openModal("⚠️ Договор не заполнен", `<p>${esc(res.warning)}</p>`, [
          { label: "Добавить", onClick: () => submitAddTenant(aptId, { ...payload, force_no_contract: true }, onSuccess) },
          { label: "Отмена", cls: "btn-secondary" },
        ]);
      }, 0);
      return true;
    }
    showToast(
      res.notified
        ? "✅ Наниматель добавлен, приглашение отправлено"
        : "✅ Наниматель добавлен (попросите его нажать /start в боте)"
    );
    if (state.selectedAptId === aptId) {
      await loadApartmentDetail(aptId);
    }
    if (onSuccess) {
      await onSuccess();
    } else {
      await renderTab();
    }
    return true;
  } catch (err) {
    showToast("❌ " + err.message, "error");
    return false;
  }
}

// ── People (Люди — семьи + наниматели, MEDIUM-12 race-protected) ──────
async function renderFamilies(root, renderSeq = state.tabRenderSeq) {
  const data = await api("/api/families");
  if (renderSeq !== state.tabRenderSeq || state.activeTab !== "families") return;

  const tenants = Array.isArray(data.tenants) ? data.tenants : [];
  const tenantsCardHtml = tenants.length
    ? `
      <div class="card">
        <div class="row-between">
          <h3>👤 Наниматели</h3>
          <span class="pill">${tenants.length}</span>
        </div>
        <div class="mt-8">
          ${tenants.map(t => {
            const aptsInfo = (t.apartments || []).map(a => {
              const term = a.agreement_date_finish ? `до ${fmtDate(a.agreement_date_finish)}` : "бессрочно";
              return `${esc(a.display_name)} (${term})`;
            }).join(", ");
            const firstAptId = t.apartments?.[0]?.id || "";
            return `
              <div class="table-row">
                <div>
                  <div class="bold">${esc(t.name || "—")}</div>
                  <div class="small muted">ID: ${t.telegram_id}${t.phone ? " · " + esc(t.phone) : ""}</div>
                  <div class="small muted">🏠 ${aptsInfo}</div>
                </div>
                <div class="row-nowrap">
                  <button class="btn btn-secondary btn-sm edit-tenant-people-btn" data-tid="${t.telegram_id}" data-tname="${esc(t.name || "")}" data-tphone="${esc(t.phone || "")}" title="Редактировать">✏️</button>
                  <button class="btn btn-secondary btn-sm btn-del-icon del-tenant-people-btn" data-tid="${t.telegram_id}" data-tname="${esc(t.name || t.telegram_id)}" data-first-apt="${firstAptId}" title="Удалить"><span class="del-x">❌</span></button>
                </div>
              </div>`;
          }).join("")}
        </div>
        ${state.apartments.length ? `
          <div class="row-gap mt-12">
            <button class="btn btn-secondary btn-sm" id="btn-people-add-tenant">👤 + Наниматель</button>
          </div>` : ""}
      </div>`
    : "";

  root.innerHTML = `
    <div class="card"><div class="row-between"><h2>👨‍👩‍👧‍👦 Люди</h2>${data.can_create ? `<button class="btn btn-sm" id="btn-new-fam">➕ Создать семью</button>` : ""}</div></div>
    ${data.families.map(f => `
      <div class="card">
        <div class="row-between"><h3>${esc(f.name)}</h3><span class="pill">🌆 ${f.apartments_count}</span></div>
        <div class="mt-8">
          ${f.owners.length ? f.owners.map(o => `
            <div class="table-row">
              <div>
                <div class="bold">${esc(o.name || "—")}${o.is_admin ? ' <span class="pill">Админ</span>' : ""}</div>
                <div class="small muted">ID: ${o.telegram_id}${o.phone ? " · " + esc(o.phone) : ""}</div>
              </div>
              <div class="row-nowrap">
                <button class="btn btn-secondary btn-sm edit-owner-btn" data-fid="${f.id}" data-oid="${o.telegram_id}" data-oname="${esc(o.name || "")}" data-ophone="${esc(o.phone || "")}" title="Редактировать">✏️</button>
                <button class="btn btn-secondary btn-sm btn-del-icon del-owner-btn" data-fid="${f.id}" data-oid="${o.telegram_id}" data-oname="${esc(o.name || o.telegram_id)}" title="Удалить"><span class="del-x">❌</span></button>
              </div>
            </div>`).join("") : `<p class="small muted">Владельцев нет.</p>`}
        </div>
        <div class="row-gap mt-12">
          <button class="btn btn-secondary btn-sm add-owner-btn" data-fid="${f.id}">🔑 + Владелец</button>
          <button class="btn btn-secondary btn-sm ren-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}">✏️ Имя семьи</button>
          ${data.can_create ? `<button class="btn btn-secondary btn-sm btn-del-icon del-fam-btn" data-fid="${f.id}" data-name="${esc(f.name)}" title="Удалить семью"><span class="del-x">❌</span></button>` : ""}
        </div>
      </div>`).join("")}
    ${tenantsCardHtml}
  `;

  document.getElementById("btn-new-fam")?.addEventListener("click", () => {
    openModal("➕ Новая семья", `<div class="field"><label>Название</label><input type="text" id="nf-name" placeholder="Например: Семья Ивановых" /></div>`, [
      {
        label: "Создать",
        onClick: async () => {
          const name = document.getElementById("nf-name").value.trim();
          if (!name) { showToast("Введите название семьи", "error"); return false; }
          try {
            await api("/api/families", { method: "POST", body: { name } });
            showToast("✅ Семья создана");
            await renderFamilies(root, state.tabRenderSeq);
          } catch (err) { showToast("❌ " + err.message, "error"); return false; }
        },
      },
      { label: "Отмена", cls: "btn-secondary" },
    ]);
  });

  root.querySelectorAll(".ren-fam-btn").forEach(btn => {
    btn.onclick = () => {
      const initialFamName = String(btn.dataset.name || "").trim();
      openModal("✏️ Переименовать семью", `<div class="field"><label>Название семьи</label><input type="text" id="rf-name" value="${esc(btn.dataset.name)}" /></div>`, [
        {
          id: "modal-save-fam-name",
          label: "Сохранить",
          disabled: true,
          onClick: async () => {
            const name = document.getElementById("rf-name").value.trim();
            if (!name) { showToast("Название не может быть пустым", "error"); return false; }
            try {
              await api(`/api/families/${btn.dataset.fid}`, { method: "PUT", body: { name } });
              showToast("✅ Переименовано");
              await renderFamilies(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
      const rfInput = document.getElementById("rf-name");
      const saveBtn = document.getElementById("modal-save-fam-name");
      rfInput?.addEventListener("input", () => {
        const v = rfInput.value.trim();
        if (saveBtn) saveBtn.disabled = !v || v === initialFamName;
      });
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
        {
          label: "Добавить",
          onClick: async () => {
            const tgId = Number(document.getElementById("no-id").value);
            const name = document.getElementById("no-name").value.trim();
            if (!Number.isSafeInteger(tgId) || tgId <= 0) { showToast("Введите корректный Telegram ID", "error"); return false; }
            if (!name) { showToast("Введите имя владельца", "error"); return false; }
            const ph = readMaskedPhone("no-phone");
            if (!ph.ok) { showToast(ph.error, "error"); return false; }
            try {
              await api(`/api/families/${btn.dataset.fid}/owners`, {
                method: "POST",
                body: { telegram_id: tgId, name, phone: ph.value },
              });
              showToast("✅ Владелец добавлен");
              await renderFamilies(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    };
  });

  root.querySelectorAll(".edit-owner-btn").forEach(btn => {
    btn.onclick = () => {
      const initialOwnerName = String(btn.dataset.oname || "").trim();
      const initialOwnerPhone = formatRuPhone(btn.dataset.ophone || "");
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
        {
          id: "modal-save-owner",
          label: "Сохранить",
          disabled: true,
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
              await renderFamilies(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
      const nameEl = document.getElementById("eo-name");
      const phoneEl = document.getElementById("eo-phone");
      const saveBtn = document.getElementById("modal-save-owner");
      const updateOwnerDirty = () => {
        const curName = (nameEl?.value || "").trim();
        const curPhone = formatRuPhone(phoneEl?.value || "");
        const changed = curName !== initialOwnerName || curPhone !== initialOwnerPhone;
        if (saveBtn) saveBtn.disabled = !curName || !changed;
      };
      nameEl?.addEventListener("input", updateOwnerDirty);
      phoneEl?.addEventListener("input", updateOwnerDirty);
    };
  });

  root.querySelectorAll(".del-owner-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("Удалить владельца?", `<p>Удалить владельца <b>${esc(btn.dataset.oname)}</b> (ID ${btn.dataset.oid})?</p>`, [
        {
          label: "Удалить", cls: "btn-danger",
          onClick: async () => {
            try {
              await api(`/api/families/${btn.dataset.fid}/owners/${btn.dataset.oid}`, { method: "DELETE" });
              showToast("✅ Владелец удалён");
              await renderFamilies(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    };
  });

  root.querySelectorAll(".del-fam-btn").forEach(btn => {
    btn.onclick = () => {
      openModal("🗑 Удалить семью?", `<p>Удалить «${esc(btn.dataset.name)}» вместе со всеми объектами недвижимости, показаниями и привязанными пользователями?</p>`, [
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
        { label: "Отмена", cls: "btn-secondary" },
      ]);
    };
  });

  // Tenants section handlers in «Люди»
  document.getElementById("btn-people-add-tenant")?.addEventListener("click", () => {
    openAddTenantModal(null, () => renderFamilies(root, state.tabRenderSeq));
  });

  root.querySelectorAll(".edit-tenant-people-btn").forEach(btn => {
    btn.onclick = () => {
      const tid = Number(btn.dataset.tid);
      const initName = String(btn.dataset.tname || "").trim();
      const initPhone = formatRuPhone(btn.dataset.tphone || "");
      openModal("✏️ Редактировать нанимателя", `
        <div class="field">
          <label>Имя</label>
          <input type="text" id="et-name" maxlength="120" value="${esc(btn.dataset.tname)}" placeholder="Введите имя" />
        </div>
        <div class="field">
          <label>Телефон (необязательно)</label>
          <input type="tel" inputmode="tel" id="et-phone" class="phone-mask" value="${esc(btn.dataset.tphone)}" placeholder="+7-___-___-____" />
        </div>
      `, [
        {
          id: "modal-save-tenant",
          label: "Сохранить",
          disabled: true,
          onClick: async () => {
            const name = document.getElementById("et-name").value.trim();
            if (!name) { showToast("Имя не может быть пустым", "error"); return false; }
            const ph = readMaskedPhone("et-phone");
            if (!ph.ok) { showToast(ph.error, "error"); return false; }
            try {
              await api(`/api/tenants/${tid}`, { method: "PUT", body: { name, phone: ph.value } });
              showToast("✅ Данные нанимателя обновлены");
              if (state.selectedAptId) await loadApartmentDetail(state.selectedAptId);
              await renderFamilies(root, state.tabRenderSeq);
            } catch (err) { showToast("❌ " + err.message, "error"); return false; }
          },
        },
        { label: "Отмена", cls: "btn-secondary" },
      ]);
      const nameEl = document.getElementById("et-name");
      const phoneEl = document.getElementById("et-phone");
      const saveBtn = document.getElementById("modal-save-tenant");
      const updateDirty = () => {
        const cn = (nameEl?.value || "").trim();
        const cp = formatRuPhone(phoneEl?.value || "");
        if (saveBtn) saveBtn.disabled = !cn || (cn === initName && cp === initPhone);
      };
      nameEl?.addEventListener("input", updateDirty);
      phoneEl?.addEventListener("input", updateDirty);
    };
  });

  root.querySelectorAll(".del-tenant-people-btn").forEach(btn => {
    btn.onclick = () => {
      const tid = Number(btn.dataset.tid);
      const tObj = tenants.find(x => x.telegram_id === tid);
      const linkedApts = tObj?.apartments || [];
      if (!linkedApts.length) return;

      if (linkedApts.length === 1) {
        const a = linkedApts[0];
        openModal("Удалить нанимателя?", `<p>Отозвать доступ у <b>${esc(btn.dataset.tname)}</b> (ID ${tid}) к объекту «${esc(a.display_name)}»?</p>`, [
          {
            label: "Удалить", cls: "btn-danger",
            onClick: async () => {
              try {
                await api(`/api/apartments/${a.id}/tenants/${tid}`, { method: "DELETE" });
                showToast("✅ Наниматель удалён");
                if (state.selectedAptId === a.id) await loadApartmentDetail(a.id);
                await renderFamilies(root, state.tabRenderSeq);
              } catch (err) { showToast("❌ " + err.message, "error"); return false; }
            },
          },
          { label: "Отмена", cls: "btn-secondary" },
        ]);
      } else {
        openModal("Открепить нанимателя", `
          <p>Наниматель <b>${esc(btn.dataset.tname)}</b> привязан к нескольким объектам. Выберите объект:</p>
          <div class="field mt-8">
            <select id="del-t-apt-sel">
              ${linkedApts.map(a => `<option value="${a.id}">${esc(a.display_name)}</option>`).join("")}
            </select>
          </div>
        `, [
          {
            label: "Открепить", cls: "btn-danger",
            onClick: async () => {
              const aid = Number(document.getElementById("del-t-apt-sel").value);
              try {
                await api(`/api/apartments/${aid}/tenants/${tid}`, { method: "DELETE" });
                showToast("✅ Наниматель откреплён");
                if (state.selectedAptId === aid) await loadApartmentDetail(aid);
                await renderFamilies(root, state.tabRenderSeq);
              } catch (err) { showToast("❌ " + err.message, "error"); return false; }
            },
          },
          { label: "Отмена", cls: "btn-secondary" },
        ]);
      }
    };
  });
}

// ── Management / Admin (Управление, MEDIUM-12 race-protected) ─────────
async function renderAdmin(root, renderSeq = state.tabRenderSeq) {
  const [usersRes, auditRes] = await Promise.all([api("/api/admin/users"), api("/api/admin/audit")]);
  if (renderSeq !== state.tabRenderSeq || state.activeTab !== "admin") return;

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

loadBootstrap(true);
