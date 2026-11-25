const SECTION_STATUSES = {
  "incoming": ["Awaiting AI Extraction"],
  "needs-review": ["Needs Review"],
  "po-matching": ["Awaiting PO Matching", "PO Query / Matching Issue"],
  "sage-registration": ["Awaiting Sage Registration"],
  "approver1": ["Awaiting Approval 1"],
  "approver2": ["Awaiting Approval 2"],
  "on-hold": [
    "Approval Query / On Hold",
    "Awaiting Approval 1",
    "Awaiting Approval 2",
    "Payment Routing Issue / On Hold",
    "Workflow Issue / On Hold",
  ],
  "approved": ["Approved"],
  "payment-bacs": ["Approved for Payment - BACS"],
  "payment-bankline": ["Approved for Payment - Bankline"],
  "payment-foreign-poa": ["Approved for Payment - Foreign POA", "Foreign Payment / Awaiting Allocation"],
  "reconciliation": ["Paid / Awaiting Bank Reconciliation"],
  "complete": ["Reconciled / Complete", "Statement Filed"],
  "rejected": ["Rejected", "Cancelled - Duplicate"],
};
// Which nav tabs each signed-in role may view. "admin" is a
// config panel, not an invoice-status tab, and is only ever
// shown to the admin role. Every other tab maps 1:1 onto
// MANUAL_VS_AUTOMATED.md's manual decision steps.
const ROLE_TABS = {
  "admin": ["search", "incoming", "statements", "needs-review", "po-matching", "sage-registration", "approver1", "approver2", "on-hold", "approved", "payment-bacs", "payment-bankline", "payment-foreign-poa", "reconciliation", "complete", "rejected", "admin"],
  "purchase_ledger": ["search", "incoming", "statements", "needs-review", "po-matching", "sage-registration", "on-hold", "approved", "payment-bacs", "payment-bankline", "payment-foreign-poa", "reconciliation", "complete", "rejected"],
  "approver1": ["search", "statements", "approver1"],
  "approver2": ["search", "statements", "approver2"],
  "purchasing": ["search", "statements", "po-matching"],
};

const picker = document.getElementById("invoice-picker");
const pdfFrame = document.getElementById("pdf-frame");
const pdfEmpty = document.getElementById("pdf-empty");
const documentBadge = document.getElementById("document-badge");
const processingBadge = document.getElementById("processing-badge");
const toastContainer = document.getElementById("toast-container");
const loginScreen = document.getElementById("login-screen");
const appRoot = document.getElementById("app-root");
let invoices = [];
let displayedInvoiceId = null;
// The company the review form should show. Remembered separately
// because the invoice can render before /api/companies has filled
// the dropdown, and rebuilding the options would otherwise blank it.
let previewCompanyWanted = "";
const ACTIVE_TAB_STORAGE_KEY = "invoice-processor-active-tab";
const SCROLL_STORAGE_PREFIX = "invoice-processor-scroll-";
let currentTab = storedValue(ACTIVE_TAB_STORAGE_KEY) || "search";
let lastActivityId = null;
let currentUser = null;
let authConfig = { microsoft_enabled: false, local_enabled: true };
let invoiceCompanies = [];
let sharePointCompanyFolders = new Map();
let adminCompanies = [];
let adminSuppliers = [];
let adminMatrix = [];
let adminSupplierTerms = [];
let adminMatrixFilterTimer = null;
let irjConfigurations = new Map();
let supplierRequestSequence = 0;
let invoiceRefreshSequence = 0;
let renderedInvoiceSnapshot = null;
let activityPollTimer = null;
let invoicePollTimer = null;
let metricsRefreshTimer = null;
let activityEventSource = null;
let openedFlaggedInvoiceId = null;
// True only right after "Review" or an upload opens a flagged
// invoice, so a later refresh keeps whatever the picker shows.
let showFlaggedNext = false;
const expandedInvoiceIds = new Set();

function storedValue(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

function storeValue(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (error) {
    // The app remains usable when browser storage is disabled.
  }
}

// ---------------------------------------------------------
// Theme, navigation drawer and page header
// ---------------------------------------------------------
const THEME_STORAGE_KEY = "invoice-processor-theme";
const SIDEBAR_STORAGE_KEY = "invoice-processor-sidebar-collapsed";
const themeQuery = window.matchMedia
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;
const drawerQuery = window.matchMedia
  ? window.matchMedia("(max-width: 960px)")
  : null;
const sidebar = document.querySelector(".sidebar");
const navToggle = document.getElementById("nav-toggle");

function effectiveTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return themeQuery && themeQuery.matches ? "dark" : "light";
}

function syncThemeControls() {
  const theme = effectiveTheme();
  document.documentElement.dataset.effectiveTheme = theme;
  const label = theme === "dark"
    ? "Switch to light theme"
    : "Switch to dark theme";
  document.querySelectorAll("[data-theme-toggle]").forEach(button => {
    button.setAttribute("aria-label", label);
    button.title = label;
  });
}

document.querySelectorAll("[data-theme-toggle]").forEach(button => {
  button.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storeValue(THEME_STORAGE_KEY, next);
    syncThemeControls();
  });
});
if (themeQuery && themeQuery.addEventListener) {
  themeQuery.addEventListener("change", syncThemeControls);
}

function isDrawerLayout() {
  return Boolean(drawerQuery && drawerQuery.matches);
}

function syncNavigationState() {
  const drawer = isDrawerLayout();
  const open = drawer
    ? appRoot.classList.contains("nav-open")
    : !appRoot.classList.contains("sidebar-collapsed");
  navToggle.setAttribute("aria-expanded", String(open));
  navToggle.setAttribute(
    "aria-label",
    open ? "Hide navigation" : "Show navigation"
  );
  // A hidden sidebar must not be reachable with the keyboard, and
  // while the drawer is open the page behind it is not either.
  sidebar.inert = !open;
  document.querySelector("main").inert = drawer && open;
  document.body.style.overflow = drawer && open ? "hidden" : "";
}

function closeDrawer({ restoreFocus = true } = {}) {
  if (!appRoot.classList.contains("nav-open")) return;
  appRoot.classList.remove("nav-open");
  syncNavigationState();
  if (restoreFocus) navToggle.focus();
}

navToggle.addEventListener("click", () => {
  if (isDrawerLayout()) {
    appRoot.classList.toggle("nav-open");
    syncNavigationState();
    if (appRoot.classList.contains("nav-open")) {
      (sidebar.querySelector("button.active") ||
        sidebar.querySelector("#section-nav button"))?.focus();
    }
  } else {
    const collapsed = appRoot.classList.toggle("sidebar-collapsed");
    storeValue(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
    syncNavigationState();
  }
});
document.getElementById("nav-close").addEventListener(
  "click",
  () => closeDrawer()
);
document.getElementById("nav-scrim").addEventListener(
  "click",
  () => closeDrawer()
);
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && appRoot.classList.contains("nav-open")) {
    closeDrawer();
  }
});
if (drawerQuery && drawerQuery.addEventListener) {
  drawerQuery.addEventListener("change", () => {
    appRoot.classList.remove("nav-open");
    syncNavigationState();
  });
}
if (storedValue(SIDEBAR_STORAGE_KEY) === "1") {
  appRoot.classList.add("sidebar-collapsed");
}
syncThemeControls();
syncNavigationState();

function updatePageHeader(button) {
  const label = [...button.childNodes]
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent)
    .join("")
    .trim();
  const group = button.closest(".nav-group")
    ?.querySelector(".nav-group-label")?.textContent || "";
  document.getElementById("page-title").textContent = label;
  document.getElementById("page-group").textContent = group;
  document.title = `${label} · Swantex Invoice Processing`;
}

function activateTab(tab) {
  const button = document.querySelector(
    `#section-nav button[data-tab="${tab}"]`
  );
  if (!button || button.style.display === "none") return false;
  currentTab = tab;
  storeValue(ACTIVE_TAB_STORAGE_KEY, tab);
  document.querySelectorAll("#section-nav button").forEach(item => {
    item.classList.toggle("active", item === button);
    if (item === button) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  });
  updatePageHeader(button);
  document.querySelectorAll(".tab-panel").forEach(panel => {
    panel.classList.toggle("active", panel.dataset.tabPanel === tab);
  });
  const restoreScrollPosition = () => window.requestAnimationFrame(() => {
    const savedPosition = Number(
      storedValue(`${SCROLL_STORAGE_PREFIX}${tab}`) || 0
    );
    window.scrollTo(0, Number.isFinite(savedPosition) ? savedPosition : 0);
  });
  if (tab === "statements") {
    loadStatements().finally(restoreScrollPosition);
  } else {
    restoreScrollPosition();
  }
  return true;
}

function setValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value || "";
}

function applyPreviewConfidence(invoice) {
  const confidences = fieldConfidences(invoice);
  const fields = {
    "preview-company": "company",
    "preview-supplier": "supplier",
    "preview-supplier-invoice": "supplier_invoice_number",
    "preview-po": "purchase_order_number",
    "preview-invoice-date": "invoice_date",
    "preview-value": "invoice_value",
    "preview-currency": "currency",
  };
  for (const [elementId, fieldName] of Object.entries(fields)) {
    const element = document.getElementById(elementId);
    if (!element) continue;
    const confidence = confidences[fieldName];
    element.title = confidence == null
      ? "No field confidence was recorded."
      : `AI confidence: ${Math.round(Number(confidence) * 100)}%`;
    markFieldConfidence(element, confidence);
  }
}

// Low-confidence fields get a warning style plus a text badge
// so the cue does not rely on colour alone.
function markFieldConfidence(element, confidence) {
  const label = element.closest("label");
  if (!label) return;
  const low = confidence != null && Number(confidence) < 0.8;
  label.classList.toggle("low-confidence", low);
  let note = label.querySelector(".confidence-note");
  if (low) {
    if (!note) {
      note = document.createElement("span");
      note.className = "confidence-note";
      note.id = `${element.id}-confidence-note`;
      label.appendChild(note);
    }
    note.textContent =
      `Low AI confidence ${Math.round(Number(confidence) * 100)}% — check`;
    element.setAttribute("aria-describedby", note.id);
  } else {
    if (note) note.textContent = "";
    element.removeAttribute("aria-describedby");
  }
}

function showToast(message, isError) {
  const toast = document.createElement("div");
  toast.className = "toast" + (isError ? " error" : "");
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = isError ? "!" : "✓";
  const text = document.createElement("div");
  text.className = "toast-message";
  if (isError) {
    const prefix = document.createElement("span");
    prefix.className = "visually-hidden";
    prefix.textContent = "Error: ";
    text.appendChild(prefix);
  }
  text.appendChild(document.createTextNode(message));
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss notification");
  close.textContent = "×";
  toast.append(icon, text, close);
  let removed = false;
  const dismiss = () => {
    if (removed) return;
    removed = true;
    toast.classList.add("leaving");
    window.setTimeout(() => toast.remove(), 180);
  };
  close.addEventListener("click", dismiss);
  toastContainer.appendChild(toast);
  // Keep the stack short: drop the oldest beyond four.
  const live = [...toastContainer.querySelectorAll(".toast:not(.leaving)")];
  live.slice(0, Math.max(0, live.length - 4)).forEach(old => old.querySelector(".toast-close")?.click());
  // Errors stay a little longer so they can be read; hovering or
  // focusing a toast pauses its timer.
  const lifetime = isError ? 8000 : 5000;
  let timer = window.setTimeout(dismiss, lifetime);
  const pause = () => window.clearTimeout(timer);
  const resume = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(dismiss, 2500);
  };
  toast.addEventListener("mouseenter", pause);
  toast.addEventListener("mouseleave", resume);
  toast.addEventListener("focusin", pause);
  toast.addEventListener("focusout", resume);
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// escapeHtml() leaves quotes alone, which is fine for element
// text but not for attribute values built from file names or
// supplier names. Use this for every title / aria-label.
function escapeAttr(value) {
  return escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

// Date-only values (YYYY-MM-DD) are shown as DD/MM/YYYY to match
// formatUkTimestamp(). Anything else is shown unchanged.
function formatUkDate(value) {
  if (!value) return "—";
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
  return match ? `${match[3]}/${match[2]}/${match[1]}` : String(value);
}

// Today's date as YYYY-MM-DD in the browser's own time zone
// (toISOString() would give the UTC date).
function localTodayIsoDate() {
  const now = new Date();
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
}

function formatMoney(value, currency) {
  if (value === null || value === undefined || value === "") return "—";
  if (!Number.isFinite(Number(value))) return String(value);
  return formatMetricMoney(value, currency);
}

// Short, unique description of an invoice for screen-reader
// labels, e.g. "IRJ 004514, Bright Paper Co".
function invoiceAccessibleName(invoice) {
  if (!invoice) return "";
  return [
    invoice.irj_number ? `IRJ ${invoice.irj_number}` : invoice.original_filename,
    invoice.supplier,
  ].filter(Boolean).join(", ");
}

// Disables a button and shows a spinner while an async task runs.
async function withBusyButton(button, task) {
  if (!button) return task();
  if (button.getAttribute("aria-busy") === "true") return undefined;
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = wasDisabled;
  }
}

// Inline error under an admin form; cleared on the next submit.
function setFormError(form, message) {
  let box = form.nextElementSibling;
  if (!box || !box.classList.contains("form-error")) {
    box = document.createElement("p");
    box.className = "form-error";
    box.setAttribute("role", "alert");
    form.after(box);
  }
  box.textContent = message || "";
  box.hidden = !message;
}

async function readResponseBody(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

async function fetchAdminJson(url) {
  const response = await fetch(url);
  const body = await readResponseBody(response);
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" && detail !== "Internal Server Error"
      ? `: ${detail}` : "";
    throw new Error(`${url} failed (HTTP ${response.status})${message}`);
  }
  return body;
}

async function sendJson(url, method, body) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const detail = await readResponseBody(response);
  if (!response.ok) {
    throw new Error(detail.detail || `Request failed (${response.status}).`);
  }
  return detail;
}

// An invoice action's reply is the updated invoice. Show it straight
// away instead of waiting for the full list to reload.
async function invoiceAction(url, body) {
  const record = await sendJson(url, "POST", body);
  showUpdatedInvoice(record);
  return record;
}

function showUpdatedInvoice(record) {
  if (!record || typeof record !== "object") return;
  if (!Number.isInteger(record.id) || typeof record.status !== "string") return;
  if (!("message_id" in record)) return;  // not a full invoice record
  const index = invoices.findIndex(invoice => invoice.id === record.id);
  if (index === -1) return;
  invoices[index] = record;
  renderAllSections();
  updateCounts();
}

async function postJson(url, body) {
  return sendJson(url, "POST", body);
}

async function putJson(url, body) {
  return sendJson(url, "PUT", body);
}

// ---------------------------------------------------------
// Authentication + admin config panel
// ---------------------------------------------------------

function splitCommaList(value) {
  return (value || "")
    .split(",")
    .map(part => part.trim())
    .filter(Boolean);
}

function includeCurrentOption(options, value, label = value) {
  if (!value || options.some(([optionValue]) => optionValue === value)) {
    return options;
  }
  return [...options, [value, label]];
}

function replaceSelectOptions(select, options) {
  const selected = select.value;
  select.replaceChildren();
  for (const [value, label] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  if (options.some(([value]) => value === selected)) {
    select.value = selected;
  }
}
