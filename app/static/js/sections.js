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

// Every row repeats "View PDF", "Approve", "Reject"…; name the
// invoice so each control makes sense out of context.
function labelInvoiceRows(root) {
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
  populateStatementCompanySelects(root);
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

function renderSectionTable({ tab, containerId, columns, actions: actionsFn, empty }) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const rows = sectionInvoices(tab);
  const context = sectionRenderContext();
  const rowSignatures = rows.map(invoice => JSON.stringify([
    invoice,
    expandedInvoiceIds.has(String(invoice.id)),
  ]));
  const signature = `${context}\n${rowSignatures.join("\n")}`;
  const previous = renderedSectionState.get(containerId);
  if (previous && previous.signature === signature) return;
  if (!rows.length) {
    const [emptyTitle, emptyDetail] = empty;
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
      nodes.forEach(node => labelInvoiceRows(node));
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
  labelInvoiceRows(container);
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

function pdfLinkButton(invoice) {
  return `<a class="action-link" href="/api/invoices/${invoice.id}/pdf" ` +
    `target="_blank" rel="noopener">View PDF</a>`;
}

function paymentRunActions(invoice) {
  return `
    ${pdfLinkButton(invoice)}
    <button data-action="pay" data-id="${invoice.id}">Record payment</button>
  `;
}

function approvalActions(level) {
  return invoice => `
    ${pdfLinkButton(invoice)}
    <button data-action="approve" data-level="${level}" data-id="${invoice.id}">Approve</button>
    ${invoice.hold_level === level ? "" : `
      <button data-action="hold" data-level="${level}" data-id="${invoice.id}" class="secondary">Record query</button>
    `}
    <button data-action="reject" data-level="${level}" data-id="${invoice.id}" class="danger">Reject</button>
  `;
}

const PAYMENT_ROUTE_COLUMN = { label: "Payment route", value: i => i.payment_method || "—" };

// One entry per invoice queue. A queue lists the invoices whose
// status is in SECTION_STATUSES[tab] (narrowed by `filter`) in the
// table `#${tab}-table`. The tables, the nav counts and the empty
// states all come from this list.
const SECTIONS = [
  {
    tab: "needs-review",
    filter: isFlaggedInvoice,
    extraColumns: [
      { label: "Return stage", value: i => i.review_return_status || "Requires a dedicated resolution", className: "cell-note" },
    ],
    actions: invoice => `
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
    empty: ["Nothing flagged", "Documents the AI cannot route confidently, or that someone flags, will appear here for Purchase Ledger review."],
  },
  {
    tab: "po-matching",
    extraColumns: [
      { label: "PO number", value: i => i.po_number || "—", className: "cell-ref" },
    ],
    actions: invoice => `
      ${pdfLinkButton(invoice)}
      ${currentUser && currentUser.role === "purchasing" ? "" : `
        <button data-action="po-match" data-id="${invoice.id}">Mark matched</button>
        <button data-action="po-query" data-id="${invoice.id}">Record query</button>
        <button data-action="po-reject" data-id="${invoice.id}" class="danger">Reject</button>
      `}
    `,
    empty: ["No invoices awaiting PO matching", "Invoices with a purchase order number arrive here after confirmation."],
  },
  {
    tab: "sage-registration",
    actions: invoice => `
      ${pdfLinkButton(invoice)}
      <button data-action="register-sage" data-id="${invoice.id}">Register in Sage</button>
    `,
    empty: ["Nothing to register in Sage", "Confirmed nominal invoices appear here until their Sage registration and IRJ are confirmed."],
  },
  {
    tab: "approver1",
    actions: approvalActions(1),
    empty: ["No approvals waiting", "Invoices routed to you as first approver will appear here."],
  },
  {
    tab: "approver2",
    actions: approvalActions(2),
    empty: ["No approvals waiting", "Invoices that need a second approval will appear here."],
  },
  {
    tab: "on-hold",
    // The approval statuses are listed too, but only held ones belong here.
    filter: invoice => invoice.status.includes("On Hold") || Boolean(invoice.hold_level),
    extraColumns: [
      { label: "Payment method", value: i => i.payment_method || "—" },
    ],
    actions: invoice => `
      ${pdfLinkButton(invoice)}
      ${invoice.status === "Payment Routing Issue / On Hold"
        ? `<button data-action="retry-payment-route" data-id="${invoice.id}">Retry routing</button>`
        : `<button data-action="resume" data-id="${invoice.id}">${invoice.status === "Workflow Issue / On Hold" ? "Resolve issue" : "Resume approval"}</button>`}
    `,
    empty: ["Nothing on hold", "Approval queries and routing issues will appear here until they are resolved."],
  },
  {
    tab: "approved",
    extraColumns: [
      { label: "Payment method", value: i => i.payment_method || "Awaiting automatic routing" },
    ],
    actions: pdfLinkButton,
    empty: ["No approved invoices waiting", "Approved invoices pass through here on their way to a payment run."],
  },
  {
    tab: "payment-bacs",
    extraColumns: [PAYMENT_ROUTE_COLUMN],
    actions: paymentRunActions,
    empty: ["No BACS payments due", "Invoices routed for BACS payment will appear here."],
  },
  {
    tab: "payment-bankline",
    extraColumns: [PAYMENT_ROUTE_COLUMN],
    actions: paymentRunActions,
    empty: ["No Bankline payments due", "Invoices routed for Bankline payment will appear here."],
  },
  {
    tab: "payment-foreign-poa",
    extraColumns: [PAYMENT_ROUTE_COLUMN],
    actions: paymentRunActions,
    empty: ["No foreign payments due", "Foreign currency invoices awaiting payment or allocation will appear here."],
  },
  {
    tab: "reconciliation",
    extraColumns: [
      { label: "Payment date", value: i => formatUkDate(i.payment_date), className: "cell-date" },
      { label: "Paid by", value: i => i.paid_by || "—" },
    ],
    actions: invoice => `
      ${pdfLinkButton(invoice)}
      <button data-action="reconcile" data-id="${invoice.id}">Mark reconciled</button>
    `,
    empty: ["Nothing to reconcile", "Paid invoices wait here until they appear on a bank statement."],
  },
  {
    tab: "complete",
    extraColumns: [
      { label: "Marked reconciled", value: i => formatUkTimestamp(i.reconciled_at), className: "cell-date" },
      { label: "Appeared on bank statement", value: i => formatUkDate(i.reconciliation_date), className: "cell-date" },
      { label: "Completed by", value: i => i.reconciled_by || i.foreign_allocated_by || "—" },
    ],
    actions: pdfLinkButton,
    empty: ["No completed documents yet", "Reconciled invoices and filed statements will appear here."],
  },
  {
    tab: "rejected",
    actions: pdfLinkButton,
    empty: ["No rejected documents", "Rejected invoices and cancelled duplicates will appear here."],
  },
];
for (const section of SECTIONS) {
  section.containerId = `${section.tab}-table`;
  section.columns = sectionColumns(section.tab, section.extraColumns);
}
const SECTION_BY_TAB = new Map(SECTIONS.map(section => [section.tab, section]));

// The invoices a queue (or its nav count) shows.
function sectionInvoices(tab) {
  if (tab === "incoming") return incomingInvoices();
  const statuses = SECTION_STATUSES[tab];
  const filter = SECTION_BY_TAB.get(tab)?.filter;
  return invoices.filter(
    invoice => statuses.includes(invoice.status) && (!filter || filter(invoice))
  );
}

function renderAllSections() {
  for (const section of SECTIONS) renderSectionTable(section);
}

// One listener per queue handles every row: action buttons, and
// expanding or collapsing a row by click or Enter / Space.
function handleSectionEvent(event, container) {
  const button = event.type === "click" && event.target.closest("[data-action]");
  if (button && container.contains(button)) {
    handleRowAction(button);
    return;
  }
  const row = event.target.closest("tr[data-expand-invoice]");
  if (!row || !container.contains(row)) return;
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
    container.querySelector(`[data-expand-invoice="${CSS.escape(id)}"]`)?.focus();
  }
}

for (const section of SECTIONS) {
  const container = document.getElementById(section.containerId);
  container.addEventListener("click", event => handleSectionEvent(event, container));
  container.addEventListener("keydown", event => handleSectionEvent(event, container));
}

// Each row action asks for whatever it needs, then sends it. A
// result of false means nothing was sent (cancelled, or the action
// only navigated), so the invoice list is not reloaded.
const ROW_ACTIONS = {
  "po-match": ({ id }) =>
    invoiceAction(`/api/invoices/${id}/po-match`, { matched: true, notes: null }),

  "open-review": async ({ id }) => {
    openedFlaggedInvoiceId = id;
    showFlaggedNext = true;
    document.querySelector('#section-nav button[data-tab="incoming"]').click();
    await refreshPicker();
    return false;
  },

  "file-statement": async ({ id, button }) => {
    const company = button.closest("tr")
      ?.querySelector("[data-statement-company]")?.value;
    if (!company) {
      throw new Error("Select the statement company first.");
    }
    await invoiceAction(`/api/invoices/${id}/file-statement`, { company });
    showToast(`Statement filed to Statements/${company}.`);
  },

  "mark-invoice": async ({ id }) => {
    await invoiceAction(`/api/invoices/${id}/mark-as-invoice`, {});
    showToast("Document returned to invoice review.");
  },

  "accept-review": ({ id }) =>
    invoiceAction(`/api/invoices/${id}/review-decision`, { accepted: true, reason: null }),

  "reject-flagged": async ({ id, subject }) => {
    const reason = await promptDialog({
      context: subject,
      title: "Reject invoice",
      label: "Reason for rejecting this invoice",
      required: true,
      confirmLabel: "Reject invoice",
      tone: "danger",
    });
    if (!reason) return false;
    await invoiceAction(`/api/invoices/${id}/reject-flagged`, { reason });
  },

  "po-query": async ({ id, subject }) => {
    const query = await formDialog({
      context: subject,
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
    if (query === null) return false;
    await invoiceAction(`/api/invoices/${id}/po-match`, {
      matched: false,
      notes: query.notes,
      query_category: query.category || null,
      purchasing_contact: query.contact || null,
    });
  },

  "po-reject": async ({ id, subject }) => {
    const reason = await promptDialog({
      context: subject,
      title: "Reject PO invoice",
      label: "Reason for rejecting this PO invoice",
      required: true,
      confirmLabel: "Reject invoice",
      tone: "danger",
    });
    if (!reason) return false;
    await invoiceAction(`/api/invoices/${id}/reject`, { reason });
  },

  "register-sage": async ({ id, invoice }) => {
    if (!invoice) throw new Error("Invoice could not be found.");
    const irjNumber = await collectSageRegistration(invoice);
    if (!irjNumber) return false;
    await invoiceAction(`/api/invoices/${id}/register-sage`, { irj_number: irjNumber });
  },

  "approve": async ({ id, button, subject }) => {
    const comments = await promptDialog({
      context: subject,
      title: "Approve invoice",
      label: "Approval comments (optional)",
      confirmLabel: "Approve",
    });
    if (comments === null) return false;
    await invoiceAction(`/api/invoices/${id}/approve`, {
      level: Number(button.dataset.level),
      decision: "approved",
      comments: comments || null,
    });
  },

  "reject": async ({ id, button, subject }) => {
    const comments = await promptDialog({
      context: subject,
      title: "Reject invoice",
      label: "Reason for rejection",
      confirmLabel: "Reject invoice",
      tone: "danger",
    });
    if (comments === null) return false;
    await invoiceAction(`/api/invoices/${id}/approve`, {
      level: Number(button.dataset.level),
      decision: "rejected",
      comments: comments || null,
    });
  },

  "hold": async ({ id, button, subject }) => {
    const comments = await promptDialog({
      context: subject,
      title: "Place on hold / record query",
      label: "Reason for placing this invoice on hold / query",
      required: true,
      confirmLabel: "Place on hold",
    });
    if (!comments) return false;
    await invoiceAction(`/api/invoices/${id}/approve`, {
      level: Number(button.dataset.level),
      decision: "on_hold",
      comments,
    });
  },

  "resume": async ({ id, label, subject }) => {
    const notes = await promptDialog({
      context: subject,
      title: label.trim() || "Resume approval",
      label: "Resolution notes for resuming approval (optional)",
      confirmLabel: "Resume",
    });
    if (notes === null) return false;
    await invoiceAction(`/api/invoices/${id}/resume-approval`, { resolution_notes: notes || null });
  },

  "retry-payment-route": ({ id }) =>
    invoiceAction(`/api/invoices/${id}/retry-payment-route`, {}),

  "pay": async ({ id, invoice }) => {
    if (!invoice) throw new Error("Invoice could not be found.");
    const payment = await collectDomesticPayment(invoice);
    if (!payment) return false;
    await invoiceAction(`/api/invoices/${id}/pay`, payment);
  },

  "reconcile": async ({ id, subject }) => {
    const reconciliation = await formDialog({
      context: subject,
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
    if (reconciliation === null || !reconciliation.date) return false;
    await invoiceAction(`/api/invoices/${id}/reconcile`, {
      reconciliation_date: reconciliation.date,
      notes: reconciliation.notes || null,
    });
  },
};

async function handleRowAction(button) {
  const action = ROW_ACTIONS[button.dataset.action];
  const id = button.dataset.id;
  const label = button.textContent;
  const invoice = invoices.find(item => String(item.id) === String(id));
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Working…";
  try {
    const result = action
      ? await action({ id, button, label, invoice, subject: invoiceAccessibleName(invoice) })
      : undefined;
    if (result !== false) await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = label;
      // A dialog may have taken focus; give it back to the row
      // action unless the user has already moved elsewhere.
      if (document.activeElement === document.body) button.focus();
    }
  }
}
