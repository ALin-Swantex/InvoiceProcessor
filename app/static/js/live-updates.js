window.addEventListener("beforeunload", () => {
  storeValue(`${SCROLL_STORAGE_PREFIX}${currentTab}`, String(window.scrollY));
});

async function pollActivity() {
  try {
    const url = lastActivityId === null
      ? "/api/activity"
      : `/api/activity?since_id=${lastActivityId}`;
    const response = await fetch(url);
    if (!response.ok) return;
    const events = await response.json();
    if (!events.length) return;
    const currentRole = currentUser ? currentUser.role : null;
    let sawInvoiceEvent = false;
    for (const event of events) {
      lastActivityId = event.id;
      if (event.target_role === currentRole || event.target_role === "all") {
        showToast(event.message, event.event_type.includes("error") || event.event_type === "rejected");
      }
      if (event.invoice_id) sawInvoiceEvent = true;
    }
    if (sawInvoiceEvent) await loadInvoices();
  } catch (error) {
    // Network errors are ignored; polling retries on the next tick.
  }
}

async function initActivityCursor() {
  // Start from the newest event so existing history is not
  // replayed as toasts (/api/activity returns the oldest first).
  try {
    const response = await fetch("/api/activity/latest");
    if (!response.ok) { lastActivityId = 0; return; }
    const body = await response.json();
    lastActivityId = Number(body.latest_id) || 0;
  } catch (error) {
    lastActivityId = 0;
  }
}

function connectLiveUpdates() {
  if (activityEventSource !== null) activityEventSource.close();
  activityEventSource = new EventSource(
    `/api/activity/stream?since_id=${lastActivityId || 0}`
  );
  activityEventSource.addEventListener("invoice-update", async () => {
    await pollActivity();
    if (
      currentUser?.role === "admin" &&
      document.getElementById("admin-bi-metrics").open
    ) {
      if (metricsRefreshTimer !== null) {
        window.clearTimeout(metricsRefreshTimer);
      }
      metricsRefreshTimer = window.setTimeout(loadMetrics, 3000);
    }
  });
}

function startPolling() {
  if (activityPollTimer !== null) window.clearInterval(activityPollTimer);
  if (invoicePollTimer !== null) window.clearInterval(invoicePollTimer);
  activityPollTimer = window.setInterval(pollActivity, 30000);
  invoicePollTimer = window.setInterval(loadInvoices, 120000);
}

function stopPolling() {
  if (activityPollTimer !== null) window.clearInterval(activityPollTimer);
  if (invoicePollTimer !== null) window.clearInterval(invoicePollTimer);
  activityPollTimer = null;
  invoicePollTimer = null;
}

// Background tabs stop polling and close the live-update stream;
// returning to the tab refreshes once and resumes both.
document.addEventListener("visibilitychange", () => {
  if (!currentUser) return;
  if (document.visibilityState === "visible") {
    loadInvoices();
    pollActivity().then(() => {
      if (currentUser && !document.hidden) connectLiveUpdates();
    });
    startPolling();
  } else {
    stopPolling();
    if (activityEventSource !== null) activityEventSource.close();
    activityEventSource = null;
  }
});
