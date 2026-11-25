document.getElementById("section-nav").addEventListener("click", event => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  storeValue(`${SCROLL_STORAGE_PREFIX}${currentTab}`, String(window.scrollY));
  activateTab(button.dataset.tab);
  if (isDrawerLayout()) closeDrawer({ restoreFocus: false });
});

function applyRoleVisibility(role) {
  const allowedTabs = ROLE_TABS[role] || Object.keys(SECTION_STATUSES);
  document.querySelectorAll("#section-nav button[data-tab]").forEach(button => {
    const isAllowed = allowedTabs.includes(button.dataset.tab);
    button.style.display = isAllowed ? "" : "none";
  });
  // Hide section headings whose queues are all hidden for this role.
  document.querySelectorAll("#section-nav .nav-group").forEach(group => {
    const visible = [...group.querySelectorAll("button[data-tab]")]
      .some(button => button.style.display !== "none");
    group.hidden = !visible;
  });
  // Purchasing can view PO Matching, but only Purchase Ledger and
  // admin may record matches, so explain why there are no actions.
  document.getElementById("po-matching-readonly-note").hidden =
    role !== "purchasing";
  const linkedTab = new URLSearchParams(window.location.search).get("tab");
  const desiredTab = linkedTab && allowedTabs.includes(linkedTab)
    ? linkedTab
    : allowedTabs.includes(currentTab) ? currentTab : allowedTabs[0];
  activateTab(desiredTab);
}

document.getElementById("logout-button").addEventListener("click", async () => {
  if (authConfig.microsoft_enabled && !authConfig.local_enabled) {
    window.location.href = "/api/auth/microsoft/logout";
    return;
  }
  await fetch("/api/auth/logout", { method: "POST" });
  if (activityPollTimer !== null) window.clearInterval(activityPollTimer);
  if (invoicePollTimer !== null) window.clearInterval(invoicePollTimer);
  if (activityEventSource !== null) activityEventSource.close();
  activityPollTimer = null;
  invoicePollTimer = null;
  activityEventSource = null;
  currentUser = null;
  // Force a full re-render for the next user, even if the
  // invoice list is identical (action buttons depend on role).
  invoices = [];
  renderedInvoiceSnapshot = null;
  renderedSectionState.clear();
  appRoot.classList.remove("nav-open");
  syncNavigationState();
  appRoot.classList.add("hidden");
  loginScreen.classList.remove("hidden");
});

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!authConfig.local_enabled) return;
  const errorEl = document.getElementById("login-error");
  errorEl.textContent = "";
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  const submitButton = event.target.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  submitButton.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || "Invalid username or password.");
    }
    await enterApp();
  } catch (error) {
    errorEl.textContent = error.message;
  } finally {
    submitButton.disabled = false;
    submitButton.removeAttribute("aria-busy");
  }
});

async function loadAuthConfig() {
  const response = await fetch("/api/auth/config");
  if (!response.ok) return;
  authConfig = await response.json();
  const microsoftButton = document.getElementById("microsoft-login-button");
  const localFields = document.getElementById("local-login-fields");
  microsoftButton.classList.toggle(
    "hidden", !authConfig.microsoft_enabled
  );
  localFields.classList.toggle("hidden", !authConfig.local_enabled);
  document.getElementById("login-username").required =
    authConfig.local_enabled;
  document.getElementById("login-password").required =
    authConfig.local_enabled;
}

async function enterApp() {
  const response = await fetch("/api/auth/me");
  if (!response.ok) {
    loginScreen.classList.remove("hidden");
    appRoot.classList.add("hidden");
    return;
  }
  currentUser = await response.json();
  loginScreen.classList.add("hidden");
  appRoot.classList.remove("hidden");
  document.getElementById("user-chip-label").textContent =
    `${currentUser.display_name} (${currentUser.role})`;
  document.getElementById("user-avatar").textContent =
    String(currentUser.display_name || currentUser.username || "?")
      .split(/\s+/)
      .filter(part => /^[A-Za-z]/.test(part))
      .slice(0, 2)
      .map(part => part[0].toUpperCase())
      .join("") || "?";
  document.getElementById("user-chip").title =
    `${currentUser.display_name} (${currentUser.role})`;
  applyRoleVisibility(currentUser.role);
  await Promise.all([
    loadCompanies(),
    loadSearchSuppliers(),
    loadInvoices(),
  ]);
  await loadSuppliers();
  if (currentUser.role === "admin") {
    await loadAdminPanel();
  }
  await initActivityCursor();
  stopPolling();
  if (!document.hidden) {
    connectLiveUpdates();
    startPolling();
  }
}

// Sticky header gains a shadow once the page scrolls under it.
const topbar = document.querySelector(".topbar");
const syncTopbarShadow = () => {
  topbar.classList.toggle("is-scrolled", window.scrollY > 4);
};
window.addEventListener("scroll", syncTopbarShadow, { passive: true });
syncTopbarShadow();

window.addEventListener("beforeunload", () => {
  if (activityEventSource !== null) activityEventSource.close();
});

loadAuthConfig().then(enterApp);
