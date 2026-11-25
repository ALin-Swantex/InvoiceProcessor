function formatMetricMoney(value, currency) {
  try {
    return new Intl.NumberFormat("en-GB", {
      style: "currency",
      currency: currency || "GBP",
    }).format(Number(value));
  } catch (error) {
    return `${currency || "GBP"} ${Number(value).toFixed(2)}`;
  }
}

function metricValueTable(rows, firstHeading) {
  if (!rows.length) return '<p class="metric-empty">No pending invoices.</p>';
  return '<table class="section-table"><thead><tr>' +
    `<th scope="col">${escapeHtml(firstHeading)}</th><th scope="col" class="num">Invoices</th><th scope="col" class="num">Value</th>` +
    '</tr></thead><tbody>' +
    rows.map(row => `<tr><td>${escapeHtml(row.name)}</td>` +
      `<td class="num">${escapeHtml(row.count)}</td>` +
      `<td class="num">${escapeHtml(formatMoney(row.value, row.currency))}</td></tr>`
    ).join("") + '</tbody></table>';
}

function dueDateTable(rows, emptyMessage = "Nothing due.") {
  if (!rows.length) return `<p class="metric-empty">${escapeHtml(emptyMessage)}</p>`;
  return '<table class="section-table"><thead><tr>' +
    '<th scope="col">IRJ</th><th scope="col">Company</th><th scope="col">Supplier</th><th scope="col">Due</th><th scope="col" class="num">Value</th>' +
    '</tr></thead><tbody>' +
    rows.map(row => `<tr><td class="cell-irj">${escapeHtml(row.irj_number || "—")}</td>` +
      `<td>${escapeHtml(row.company)}</td><td>${escapeHtml(row.supplier)}</td>` +
      `<td class="cell-date">${escapeHtml(formatUkDate(row.due_date))}</td>` +
      `<td class="num">${escapeHtml(formatMoney(row.value, row.currency))}</td></tr>`
    ).join("") + '</tbody></table>';
}

async function loadMetrics() {
  const dashboard = document.getElementById("metrics-dashboard");
  const granularity = document.getElementById("metrics-granularity").value;
  dashboard.className = "";
  dashboard.innerHTML = loadingStateHtml("Loading metrics…");
  try {
    const response = await fetch(
      `/api/admin/metrics?granularity=${encodeURIComponent(granularity)}`
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Metrics could not be loaded.");
    }
    const metrics = await response.json();
    const maximum = Math.max(1, ...metrics.volume.map(row => row.count));
    const totalVolume = metrics.volume.reduce((sum, row) => sum + row.count, 0);
    dashboard.className = "metrics-grid";
    dashboard.innerHTML = `
                  <section class="metric-panel wide">
                    <h4>Invoice volume <span class="metric-summary">${totalVolume}</span></h4>
                    <div class="metric-bars">
                      ${metrics.volume.map(row => `
                        <div class="metric-bar-row">
                          <span>${escapeHtml(row.period)}</span>
                          <div class="metric-bar-track"><div class="metric-bar-fill"
                            style="width: ${Math.round((row.count / maximum) * 100)}%"></div></div>
                          <strong>${escapeHtml(row.count)}</strong>
                        </div>
                      `).join("")}
                    </div>
                  </section>
                  <section class="metric-panel">
                    <h4>Overdue invoices (${metrics.due_dates.overdue.length})</h4>
                    ${dueDateTable(metrics.due_dates.overdue, "No overdue invoices.")}
                  </section>
                  <section class="metric-panel">
                    <h4>Due within 7 days (${metrics.due_dates.approaching.length})</h4>
                    ${dueDateTable(metrics.due_dates.approaching, "Nothing falls due in the next 7 days.")}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Total pending invoice value by company</h4>
                    ${metricValueTable(metrics.pending_by_company, "Company")}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Spend by supplier and company</h4>
                    ${metricValueTable(metrics.spend_by_supplier_company, "Company — Supplier")}
                  </section>
                  ${metrics.due_dates.unavailable_count ? `
                    <p class="admin-help">${metrics.due_dates.unavailable_count} pending invoice(s)
                    have no calculable due date because the invoice date or deterministic payment
                    terms are unavailable.</p>
                  ` : ""}
                `;
  } catch (error) {
    dashboard.className = "";
    dashboard.innerHTML = emptyStateHtml("Metrics could not be loaded", error.message, "error");
  }
}

function operationsQueueTable(items, status) {
  if (!items.length) return `<p class="metric-empty">No ${escapeHtml(status)} items.</p>`;
  return '<table class="section-table"><thead><tr>' +
    '<th scope="col">Received</th><th scope="col">Message</th><th scope="col" class="num">Attempts</th><th scope="col">Error</th><th scope="col"><span class="visually-hidden">Actions</span></th>' +
    '</tr></thead><tbody>' + items.map(item => `<tr>` +
      `<td>${escapeHtml(formatUkTimestamp(item.received_at))}</td>` +
      `<td class="cell-file"><span class="cell-clamp" title="${escapeAttr(item.message_id)}">${escapeHtml(item.message_id)}</span></td>` +
      `<td class="num">${escapeHtml(item.attempts)}</td>` +
      `<td>${escapeHtml(item.last_error || "—")}</td>` +
      `<td>${status === "failed" ? `<button type="button" class="secondary" ` +
        `data-retry-notification="${item.id}" aria-label="Retry queue item ${escapeAttr(item.message_id)}">Retry</button>` : ""}</td>` +
      `</tr>`).join("") + '</tbody></table>';
}

async function loadOperations() {
  const dashboard = document.getElementById("operations-dashboard");
  dashboard.className = "";
  dashboard.innerHTML = loadingStateHtml("Loading worker and queue status…");
  try {
    const operations = await fetchAdminJson("/api/admin/operations");
    const worker = operations.worker;
    const workerStatus = worker
      ? `${worker.status} · last seen ${formatUkTimestamp(worker.last_seen_at)} ` +
        `(${worker.age_seconds}s ago)`
      : "No heartbeat recorded";
    dashboard.className = "metrics-grid";
    dashboard.innerHTML = `
                  <section class="metric-panel wide">
                    <h4>Outlook invoice worker</h4>
                    <p><strong>${escapeHtml(workerStatus)}</strong></p>
                    ${worker && worker.last_success_at
          ? `<p>Last successful cycle: ${escapeHtml(formatUkTimestamp(worker.last_success_at))}</p>`
          : ""}
                    ${worker && worker.last_error
          ? `<p class="admin-help">Last error: ${escapeHtml(worker.last_error)}</p>`
          : ""}
                    ${operations.alerts.length
          ? `<ul>${operations.alerts.map(alert => `<li>${escapeHtml(alert)}</li>`).join("")}</ul>`
          : '<p>No active monitoring alerts.</p>'}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Failed queue items (${operations.queue.failed.length})</h4>
                    ${operationsQueueTable(operations.queue.failed, "failed")}
                  </section>
                  <section class="metric-panel">
                    <h4>Pending (${operations.queue.pending.length})</h4>
                    ${operationsQueueTable(operations.queue.pending, "pending")}
                  </section>
                  <section class="metric-panel">
                    <h4>Processing (${operations.queue.processing.length})</h4>
                    ${operationsQueueTable(operations.queue.processing, "processing")}
                  </section>`;
    dashboard.querySelectorAll("[data-retry-notification]").forEach(button => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await postJson(
            `/api/admin/outlook-queue/${button.dataset.retryNotification}/retry`,
            {}
          );
          showToast("Queue item returned to pending.");
          await loadOperations();
        } catch (error) {
          button.disabled = false;
          showToast(error.message, true);
        }
      });
    });
  } catch (error) {
    dashboard.className = "";
    dashboard.innerHTML = emptyStateHtml("Monitoring status could not be loaded", error.message, "error");
  }
}

document.getElementById("metrics-granularity").addEventListener(
  "change",
  loadMetrics
);
