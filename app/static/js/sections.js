// Per-table render state so a refreshed invoice snapshot only
// touches tables (and rows) whose data actually changed. Unchanged
// rows keep their DOM nodes, so an expanded PDF iframe is not
// reloaded when some other invoice changes.
const renderedSectionState = new Map();

function sectionRenderContext() {
  return JSON.stringify([
    currentUser ? currentUser.role : null,
    invoiceCompanies.map(company => company.name),
  ]);
}

// One consistent colour per lifecycle stage for status pills.
function statusTone(status) {
  const value = String(status || "");
  if (/^(Rejected|Cancelled)/.test(value)) return "danger";
  if (/On Hold|Query|Issue|Needs Review/.test(value)) return "warn";
  if (/^(Reconciled|Statement Filed)/.test(value)) return "neutral";
  if (/^(Approved|Paid)/.test(value)) return "success";
  if (/PO Matching|Sage Registration/.test(value)) return "violet";
  return "info";
}

function statusPillHtml(status) {
  return `<span class="status-pill" data-tone="${statusTone(status)}">` +
    `${escapeHtml(status)}</span>`;
}

function invoiceRowHtml(invoice, columns, actionsFn) {
  const expanded = expandedInvoiceIds.has(String(invoice.id));
  let html = `<tr class="invoice-summary-row" data-expand-invoice="${invoice.id}" ` +
    `tabindex="0" aria-expanded="${expanded}" title="Click to view invoice analysis">`;
  for (const column of columns) {
    const label = escapeAttr(column.label);
    const raw = column.value(invoice);
    const value = escapeHtml(raw);
    if (column.boxed && (!raw || raw === "—")) {
      html += `<td class="invoice-description-cell" data-label="${label}">` +
        `<span class="cell-empty" aria-label="None">—</span></td>`;
    } else if (column.boxed) {
      // Clamped to a few lines in the table; the full text is in
      // the expanded row and the tooltip.
      html += `<td class="invoice-description-cell" data-label="${label}">` +
        `<div class="mini-description"${raw && raw !== "—" ? ` title="${escapeAttr(raw)}"` : ""}>` +
        `<span class="clamp-lines">${value}</span></div></td>`;
    } else if (column.pill) {
      html += `<td class="cell-status" data-label="${label}">${statusPillHtml(raw)}</td>`;
    } else {
      const secondary = column.secondary ? column.secondary(invoice) : "";
      const tag = column.tag ? column.tag(invoice) : "";
      const body = column.clamp
        ? `<span class="cell-clamp" title="${escapeAttr(raw)}">${value}</span>`
        : column.mono
          ? `<span class="${column.mono(invoice) ? "cell-mono" : "cell-muted"}">${value}</span>`
          : value;
      html += `<td${column.className ? ` class="${column.className}"` : ""} ` +
        `data-label="${label}">${body}` +
        (secondary ? `<span class="cell-secondary" title="${escapeAttr(secondary)}">${escapeHtml(secondary)}</span>` : "") +
        (tag ? `<span class="cell-tag">${escapeHtml(tag)}</span>` : "") +
        `</td>`;
    }
  }
  html += `<td class="row-actions"><div class="row-action-buttons">` +
    `${actionsFn(invoice)}</div>` +
    `<span class="invoice-expand-hint">${expanded ? "Hide" : "Show"} details</span></td>`;
  html += "</tr>";
  if (expanded) {
    html += `<tr class="invoice-analysis-row"><td colspan="${columns.length + 1}" ` +
      `class="invoice-analysis-cell">${invoiceAnalysisHtml(invoice)}</td></tr>`;
  }
  return html;
}

function elementsMatching(root, selector) {
  const matches = [...root.querySelectorAll(selector)];
  if (root.matches && root.matches(selector)) matches.unshift(root);
  return matches;
}

function bindInvoiceRowEvents(root, containerId) {
  // Every row repeats "View PDF", "Approve", "Reject"…; name the
  // invoice so each control makes sense out of context.
  elementsMatching(root, "tr[data-expand-invoice]").forEach(row => {
    const invoice = invoices.find(
      item => String(item.id) === String(row.dataset.expandInvoice)
    );
    const name = invoiceAccessibleName(invoice);
    if (!name) return;
    row.querySelectorAll(".row-action-buttons button, .row-action-buttons a").forEach(control => {
      const newTab = control.tagName === "A" && control.target === "_blank"
        ? " (opens in a new tab)" : "";
      control.setAttribute("aria-label", `${control.textContent.trim()} — ${name}${newTab}`);
    });
    row.querySelectorAll(".row-action-buttons select").forEach(select => {
      select.setAttribute("aria-label", `Statement company for ${name}`);
    });
  });
  elementsMatching(root, "[data-action]").forEach(button => {
    button.addEventListener("click", () => handleRowAction(button));
  });
  populateStatementCompanySelects(root);
  elementsMatching(root, "[data-expand-invoice]").forEach(row => {
    const toggle = event => {
      if (event.target.closest("button, a, input, select, textarea")) return;
      if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
      event.preventDefault();
      const id = String(row.dataset.expandInvoice);
      if (expandedInvoiceIds.has(id)) {
        expandedInvoiceIds.delete(id);
      } else {
        expandedInvoiceIds.add(id);
      }
      renderAllSections();
      if (event.type === "keydown") {
        document.getElementById(containerId)
          ?.querySelector(`[data-expand-invoice="${CSS.escape(id)}"]`)
          ?.focus();
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", toggle);
  });
}

function invoiceRowNodesFrom(tbody) {
  const rowNodes = new Map();
  for (const row of tbody.querySelectorAll(":scope > tr[data-expand-invoice]")) {
    const nodes = [row];
    const next = row.nextElementSibling;
    if (next && next.classList.contains("invoice-analysis-row")) nodes.push(next);
    rowNodes.set(String(row.dataset.expandInvoice), nodes);
  }
  return rowNodes;
}

function renderSectionTable(
  containerId,
  statuses,
  columns,
  actionsFn,
  rowFilter = () => true
) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const rows = invoices.filter(
    i => statuses.includes(i.status) && rowFilter(i)
  );
  const context = sectionRenderContext();
  const rowSignatures = rows.map(invoice => JSON.stringify([
    invoice,
    expandedInvoiceIds.has(String(invoice.id)),
  ]));
  const signature = `${context}\n${rowSignatures.join("\n")}`;
  const previous = renderedSectionState.get(containerId);
  if (previous && previous.signature === signature) return;
  if (!rows.length) {
    const [emptyTitle, emptyDetail] = SECTION_EMPTY_COPY[containerId] ||
      ["No documents in this section.", ""];
    container.innerHTML = emptyStateHtml(emptyTitle, emptyDetail);
    renderedSectionState.set(containerId, { signature, tbody: null, rows: new Map() });
    return;
  }
  if (
    previous &&
    previous.context === context &&
    previous.tbody &&
    container.contains(previous.tbody)
  ) {
    // Patch only the rows whose invoice (or expanded state)
    // changed; retained rows are never detached.
    const tbody = previous.tbody;
    const nextRows = new Map();
    const desired = [];
    rows.forEach((invoice, index) => {
      const id = String(invoice.id);
      const existing = previous.rows.get(id);
      if (existing && existing.signature === rowSignatures[index]) {
        nextRows.set(id, existing);
        desired.push(existing.nodes);
        return;
      }
      const template = document.createElement("template");
      template.innerHTML = invoiceRowHtml(invoice, columns, actionsFn);
      const nodes = [...template.content.children];
      nodes.forEach(node => bindInvoiceRowEvents(node, containerId));
      const entry = { signature: rowSignatures[index], nodes };
      nextRows.set(id, entry);
      desired.push(nodes);
    });
    for (const [id, entry] of previous.rows) {
      if (nextRows.get(id) !== entry) entry.nodes.forEach(node => node.remove());
    }
    let cursor = tbody.firstChild;
    for (const nodes of desired) {
      for (const node of nodes) {
        if (node === cursor) {
          cursor = cursor.nextSibling;
        } else {
          tbody.insertBefore(node, cursor);
        }
      }
    }
    renderedSectionState.set(containerId, {
      signature, context, tbody, rows: nextRows,
    });
    return;
  }
  const tableName = container.closest(".card")
    ?.querySelector(".card-header h2")?.textContent.trim() || "Invoices";
  let html = '<div class="table-wrap"><table class="section-table invoice-table" ' +
    `aria-label="${escapeAttr(tableName)}"><thead><tr>`;
  for (const column of columns) {
    const headerClass = column.pill ? "cell-status" : column.boxed
      ? "invoice-description-cell" : column.className;
    html += `<th scope="col"${headerClass ? ` class="${headerClass}"` : ""}>${column.label}</th>`;
  }
  html += '<th scope="col" class="col-actions">Actions</th></tr></thead><tbody>';
  for (const invoice of rows) {
    html += invoiceRowHtml(invoice, columns, actionsFn);
  }
  html += "</tbody></table></div>";
  container.innerHTML = html;
  bindInvoiceRowEvents(container, containerId);
  const tbody = container.querySelector("tbody");
  const rowNodes = invoiceRowNodesFrom(tbody);
  const renderedRows = new Map();
  rows.forEach((invoice, index) => {
    const id = String(invoice.id);
    renderedRows.set(id, {
      signature: rowSignatures[index],
      nodes: rowNodes.get(id) || [],
    });
  });
  renderedSectionState.set(containerId, {
    signature, context, tbody, rows: renderedRows,
  });
}

function invoiceDescription(invoice) {
  const notes = [];
  if (invoice.status === "Needs Review") {
    notes.push(`Flagged: ${invoice.review_reason || invoice.ai_review_warnings || "Requires review"}`);
  }
  if (invoice.po_query_notes) {
    const details = [invoice.po_query_category, invoice.po_query_contact]
      .filter(Boolean)
      .join(" · ");
    notes.push(`PO query: ${invoice.po_query_notes}${details ? ` (${details})` : ""}`);
  }
  if (invoice.hold_reason) {
    notes.push(`Approval query: ${invoice.hold_reason}`);
  }
  if (
    invoice.approver1_comments &&
    invoice.approver1_comments !== invoice.hold_reason
  ) {
    notes.push(
      `${invoice.approver1_name || "Approval"}: ${invoice.approver1_comments}`
    );
  }
  if (
    invoice.approver2_comments &&
    invoice.approver2_comments !== invoice.hold_reason
  ) {
    notes.push(
      `${invoice.approver2_name || "Approval"}: ${invoice.approver2_comments}`
    );
  }
  if (invoice.rejection_reason) {
    notes.push(`Rejected: ${invoice.rejection_reason}`);
  }
  if (invoice.cancellation_reason) {
    notes.push(`Cancelled: ${invoice.cancellation_reason}`);
  }
  if (invoice.reconciliation_notes) {
    notes.push(`Reconciliation: ${invoice.reconciliation_notes}`);
  }
  return notes.length ? notes.join("\n\n") : "—";
}

// IRJ and file name share a cell, as do supplier and company, and
// the document type is a tag, so the Actions column fits beside
// the data on laptop screens.
const BASE_COLUMNS = [
  {
    label: "IRJ / File",
    value: i => i.irj_number || "No IRJ yet",
    secondary: i => i.original_filename,
    tag: i => i.document_type === "statement" ? "Statement" : "",
    className: "cell-id",
    mono: i => Boolean(i.irj_number),
  },
  {
    label: "Supplier / Company",
    value: i => i.supplier || "—",
    secondary: i => i.company || "",
    className: "cell-party",
    clamp: true,
  },
  { label: "Status", value: i => i.status, pill: true },
  { label: "Description", value: invoiceDescription, boxed: true },
];

// Queues that only ever hold one status do not need a Status
// column; the status is still shown in the expanded row.
function sectionColumns(section, extra = []) {
  const base = SECTION_STATUSES[section].length > 1
    ? BASE_COLUMNS
    : BASE_COLUMNS.filter(column => !column.pill);
  return [...base, ...extra];
}

const SECTION_EMPTY_COPY = {
  "needs-review-table": ["Nothing flagged", "Documents the AI cannot route confidently, or that someone flags, will appear here for Purchase Ledger review."],
  "po-matching-table": ["No invoices awaiting PO matching", "Invoices with a purchase order number arrive here after confirmation."],
  "sage-registration-table": ["Nothing to register in Sage", "Confirmed nominal invoices appear here until their Sage registration and IRJ are confirmed."],
  "approver1-table": ["No approvals waiting", "Invoices routed to you as first approver will appear here."],
  "approver2-table": ["No approvals waiting", "Invoices that need a second approval will appear here."],
  "on-hold-table": ["Nothing on hold", "Approval queries and routing issues will appear here until they are resolved."],
  "approved-table": ["No approved invoices waiting", "Approved invoices pass through here on their way to a payment run."],
  "payment-bacs-table": ["No BACS payments due", "Invoices routed for BACS payment will appear here."],
  "payment-bankline-table": ["No Bankline payments due", "Invoices routed for Bankline payment will appear here."],
  "payment-foreign-poa-table": ["No foreign payments due", "Foreign currency invoices awaiting payment or allocation will appear here."],
  "reconciliation-table": ["Nothing to reconcile", "Paid invoices wait here until they appear on a bank statement."],
  "complete-table": ["No completed documents yet", "Reconciled invoices and filed statements will appear here."],
  "rejected-table": ["No rejected documents", "Rejected invoices and cancelled duplicates will appear here."],
};

function pdfLinkButton(invoice) {
  return `<a class="action-link" href="/api/invoices/${invoice.id}/pdf" ` +
    `target="_blank" rel="noopener">View PDF</a>`;
}

function renderAllSections() {
  renderSectionTable(
    "needs-review-table",
    SECTION_STATUSES["needs-review"],
    sectionColumns("needs-review", [
      { label: "Return stage", value: i => i.review_return_status || "Requires a dedicated resolution", className: "cell-note" },
    ]),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  ${invoice.document_type === "statement" ? `
                    <button data-action="mark-invoice" data-id="${invoice.id}" class="secondary">Treat as invoice</button>
                  ` : `
                    <button data-action="open-review" data-id="${invoice.id}" class="secondary">Review</button>
                  `}
                  <select data-statement-company="${invoice.id}" aria-label="Statement company"></select>
                  <button data-action="file-statement" data-id="${invoice.id}">File as statement</button>
                  ${invoice.review_return_status ? `
                    <button data-action="accept-review" data-id="${invoice.id}">Accept</button>
                  ` : ""}
                  ${invoice.document_type === "invoice" ? `
                    <button data-action="reject-flagged" data-id="${invoice.id}" class="danger">Reject</button>
                  ` : ""}
                `,
    isFlaggedInvoice
  );
  renderSectionTable(
    "po-matching-table",
    SECTION_STATUSES["po-matching"],
    sectionColumns("po-matching", [
      { label: "PO number", value: i => i.po_number || "—", className: "cell-ref" },
    ]),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  ${currentUser && currentUser.role === "purchasing" ? "" : `
                    <button data-action="po-match" data-id="${invoice.id}">Mark matched</button>
                    <button data-action="po-query" data-id="${invoice.id}">Record query</button>
                    <button data-action="po-reject" data-id="${invoice.id}" class="danger">Reject</button>
                  `}
                `
  );
  renderSectionTable(
    "sage-registration-table",
    SECTION_STATUSES["sage-registration"],
    sectionColumns("sage-registration"),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="register-sage" data-id="${invoice.id}">Register in Sage</button>
                `
  );
  renderSectionTable(
    "approver1-table",
    SECTION_STATUSES["approver1"],
    sectionColumns("approver1"),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="approve" data-level="1" data-id="${invoice.id}">Approve</button>
                  ${invoice.hold_level === 1 ? "" : `
                    <button data-action="hold" data-level="1" data-id="${invoice.id}" class="secondary">Record query</button>
                  `}
                  <button data-action="reject" data-level="1" data-id="${invoice.id}" class="danger">Reject</button>
                `
  );
  renderSectionTable(
    "approver2-table",
    SECTION_STATUSES["approver2"],
    sectionColumns("approver2"),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="approve" data-level="2" data-id="${invoice.id}">Approve</button>
                  ${invoice.hold_level === 2 ? "" : `
                    <button data-action="hold" data-level="2" data-id="${invoice.id}" class="secondary">Record query</button>
                  `}
                  <button data-action="reject" data-level="2" data-id="${invoice.id}" class="danger">Reject</button>
                `
  );
  renderSectionTable(
    "on-hold-table",
    SECTION_STATUSES["on-hold"],
    sectionColumns("on-hold", [
      { label: "Payment method", value: i => i.payment_method || "—" },
    ]),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  ${invoice.status === "Payment Routing Issue / On Hold"
        ? `<button data-action="retry-payment-route" data-id="${invoice.id}">Retry routing</button>`
        : `<button data-action="resume" data-id="${invoice.id}">${invoice.status === "Workflow Issue / On Hold" ? "Resolve issue" : "Resume approval"}</button>`}
                `,
    invoice => invoice.status.includes("On Hold") || Boolean(invoice.hold_level)
  );
  renderSectionTable(
    "approved-table",
    SECTION_STATUSES["approved"],
    sectionColumns("approved", [
      { label: "Payment method", value: i => i.payment_method || "Awaiting automatic routing" },
    ]),
    invoice => `
                  ${pdfLinkButton(invoice)}
                `
  );
  for (const [containerId, section] of [
    ["payment-bacs-table", "payment-bacs"],
    ["payment-bankline-table", "payment-bankline"],
    ["payment-foreign-poa-table", "payment-foreign-poa"],
  ]) {
    renderSectionTable(
      containerId,
      SECTION_STATUSES[section],
      sectionColumns(section, [
        { label: "Payment route", value: i => i.payment_method || "—" },
      ]),
      invoice => `
                    ${pdfLinkButton(invoice)}
                    <button data-action="pay" data-id="${invoice.id}">Record payment</button>
                  `
    );
  }
  renderSectionTable(
    "reconciliation-table",
    SECTION_STATUSES["reconciliation"],
    sectionColumns("reconciliation", [
      { label: "Payment date", value: i => formatUkDate(i.payment_date), className: "cell-date" },
      { label: "Paid by", value: i => i.paid_by || "—" },
    ]),
    invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="reconcile" data-id="${invoice.id}">Mark reconciled</button>
                `
  );
  renderSectionTable(
    "complete-table",
    SECTION_STATUSES["complete"],
    sectionColumns("complete", [
      { label: "Marked reconciled", value: i => formatUkTimestamp(i.reconciled_at), className: "cell-date" },
      { label: "Appeared on bank statement", value: i => formatUkDate(i.reconciliation_date), className: "cell-date" },
      { label: "Completed by", value: i => i.reconciled_by || i.foreign_allocated_by || "—" },
    ]),
    invoice => pdfLinkButton(invoice)
  );
  renderSectionTable(
    "rejected-table",
    SECTION_STATUSES["rejected"],
    sectionColumns("rejected"),
    invoice => pdfLinkButton(invoice)
  );
}

async function handleRowAction(button) {
  const action = button.dataset.action;
  const id = button.dataset.id;
  const originalText = button.textContent;
  const dialogSubject = invoiceAccessibleName(
    invoices.find(item => String(item.id) === String(id))
  );
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Working…";
  try {
    if (action === "po-match") {
      await postJson(`/api/invoices/${id}/po-match`, { matched: true, notes: null });
    } else if (action === "open-review") {
      openedFlaggedInvoiceId = id;
      showFlaggedNext = true;
      document.querySelector('#section-nav button[data-tab="incoming"]').click();
      await refreshPicker();
      return;
    } else if (action === "file-statement") {
      const company = button.closest("tr")
        ?.querySelector("[data-statement-company]")?.value;
      if (!company) {
        throw new Error("Select the statement company first.");
      }
      await postJson(`/api/invoices/${id}/file-statement`, { company });
      showToast(`Statement filed to Statements/${company}.`);
    } else if (action === "mark-invoice") {
      await postJson(`/api/invoices/${id}/mark-as-invoice`, {});
      showToast("Document returned to invoice review.");
    } else if (action === "accept-review") {
      await postJson(`/api/invoices/${id}/review-decision`, {
        accepted: true,
        reason: null,
      });
    } else if (action === "reject-flagged") {
      const reason = await promptDialog({
        context: dialogSubject,
        title: "Reject invoice",
        label: "Reason for rejecting this invoice",
        required: true,
        confirmLabel: "Reject invoice",
        tone: "danger",
      });
      if (!reason) return;
      await postJson(`/api/invoices/${id}/reject-flagged`, { reason });
    } else if (action === "po-query") {
      const query = await formDialog({
        context: dialogSubject,
        title: "Record PO query",
        message: "The query is routed back for resolution before the invoice can be matched.",
        confirmLabel: "Record query",
        fields: [
          { name: "notes", label: "Describe the PO matching issue", multiline: true },
          {
            name: "category",
            label: "Query category",
            placeholder: "e.g. price discrepancy, missing PO, quantity mismatch",
          },
          {
            name: "contact",
            label: "Purchasing contact to route this query to (optional)",
          },
        ],
      });
      if (query === null) return;
      const notes = query.notes;
      const queryCategory = query.category;
      const purchasingContact = query.contact;
      await postJson(`/api/invoices/${id}/po-match`, {
        matched: false,
        notes,
        query_category: queryCategory || null,
        purchasing_contact: purchasingContact || null,
      });
    } else if (action === "po-reject") {
      const reason = await promptDialog({
        context: dialogSubject,
        title: "Reject PO invoice",
        label: "Reason for rejecting this PO invoice",
        required: true,
        confirmLabel: "Reject invoice",
        tone: "danger",
      });
      if (!reason) return;
      await postJson(`/api/invoices/${id}/reject`, { reason });
    } else if (action === "register-sage") {
      const invoice = invoices.find(item => String(item.id) === String(id));
      if (!invoice) throw new Error("Invoice could not be found.");
      const irjNumber = await collectSageRegistration(invoice);
      if (!irjNumber) return;
      await postJson(`/api/invoices/${id}/register-sage`, {
        irj_number: irjNumber,
      });
    } else if (action === "approve" || action === "reject") {
      const comments = await promptDialog(action === "reject"
        ? {
            context: dialogSubject,
            title: "Reject invoice",
            label: "Reason for rejection",
            confirmLabel: "Reject invoice",
            tone: "danger",
          }
        : {
            context: dialogSubject,
            title: "Approve invoice",
            label: "Approval comments (optional)",
            confirmLabel: "Approve",
          });
      if (comments === null) return;
      await postJson(`/api/invoices/${id}/approve`, {
        level: Number(button.dataset.level),
        decision: action === "approve" ? "approved" : "rejected",
        comments: comments || null,
      });
    } else if (action === "hold") {
      const comments = await promptDialog({
        context: dialogSubject,
        title: "Place on hold / record query",
        label: "Reason for placing this invoice on hold / query",
        required: true,
        confirmLabel: "Place on hold",
      });
      if (!comments) return;
      await postJson(`/api/invoices/${id}/approve`, {
        level: Number(button.dataset.level),
        decision: "on_hold",
        comments,
      });
    } else if (action === "resume") {
      const notes = await promptDialog({
        context: dialogSubject,
        title: originalText.trim() || "Resume approval",
        label: "Resolution notes for resuming approval (optional)",
        confirmLabel: "Resume",
      });
      if (notes === null) return;
      await postJson(`/api/invoices/${id}/resume-approval`, { resolution_notes: notes || null });
    } else if (action === "retry-payment-route") {
      await postJson(`/api/invoices/${id}/retry-payment-route`, {});
    } else if (action === "pay") {
      const invoice = invoices.find(item => String(item.id) === String(id));
      if (!invoice) throw new Error("Invoice could not be found.");
      const payment = await collectDomesticPayment(invoice);
      if (!payment) return;
      await postJson(`/api/invoices/${id}/pay`, payment);
    } else if (action === "reconcile") {
      const reconciliation = await formDialog({
        context: dialogSubject,
        title: "Mark reconciled",
        confirmLabel: "Mark reconciled",
        fields: [
          {
            name: "date",
            label: "Date shown on bank statement",
            type: "date",
            value: localTodayIsoDate(),
            required: true,
          },
          { name: "notes", label: "Reconciliation notes (optional)", multiline: true },
        ],
      });
      if (reconciliation === null) return;
      const reconciliationDate = reconciliation.date;
      if (!reconciliationDate) return;
      const notes = reconciliation.notes;
      await postJson(`/api/invoices/${id}/reconcile`, {
        reconciliation_date: reconciliationDate,
        notes: notes || null,
      });
    }
    await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalText;
      // A dialog may have taken focus; give it back to the row
      // action unless the user has already moved elsewhere.
      if (document.activeElement === document.body) button.focus();
    }
  }
}
