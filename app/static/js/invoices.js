async function showInvoice(invoice) {
  documentBadge.textContent = invoice.original_filename;
  processingBadge.textContent = invoice.status;
  setValue("source-sender", invoice.sender_address || invoice.sender_name);
  setValue("source-received", invoice.received_at);
  setValue("source-subject", invoice.subject);
  setValue("processing-status", invoice.status);
  const confidence = invoice.ai_confidence;
  setValue(
    "ai-confidence",
    confidence == null ? "" : Number(confidence).toFixed(2)
  );
  document.getElementById("ai-confidence-percent").textContent =
    confidence == null ? "—" : `${Math.round(Number(confidence) * 100)}%`;
  setValue("preview-irj", invoice.irj_number);
  setValue(
    "review-warnings",
    invoice.ai_review_warnings || invoice.review_reason
  );
  setValue("extraction-model", invoice.extraction_model);
  setValue("extraction-prompt-version", invoice.extraction_prompt_version);
  setValue("routing-explanation", invoice.routing_explanation);
  const duplicateWarning = document.getElementById("duplicate-warning");
  const overrideButton = document.getElementById("override-duplicate-button");
  const cancelDuplicateButton = document.getElementById("cancel-duplicate-button");
  const deleteInvoiceButton = document.getElementById("delete-invoice-button");
  const retryApprovalRouteButton = document.getElementById("retry-approval-route-button");
  if (invoice.duplicate_of_invoice_id) {
    const originalInvoice = invoices.find(
      (candidate) => candidate.id === invoice.duplicate_of_invoice_id
    );
    const duplicateReference = originalInvoice?.irj_number
      ? `IRJ ${originalInvoice.irj_number}`
      : `invoice #${invoice.duplicate_of_invoice_id} (IRJ not yet assigned)`;
    duplicateWarning.style.display = "grid";
    document.getElementById("duplicate-warning-text").textContent =
      invoice.review_reason ||
      `It's a duplicate of ${duplicateReference}.`;
    overrideButton.style.display = "";
    cancelDuplicateButton.style.display = "";
  } else {
    duplicateWarning.style.display = "none";
    overrideButton.style.display = "none";
    cancelDuplicateButton.style.display = "none";
  }
  retryApprovalRouteButton.style.display =
    invoice.status === "Needs Review" &&
    invoice.invoice_type === "nominal" &&
    invoice.sage_registered_at
      ? ""
      : "none";
  const mayDeleteDuplicate =
    invoice.status === "Needs Review" && Boolean(invoice.duplicate_of_invoice_id);
  deleteInvoiceButton.style.display = mayDeleteDuplicate ? "" : "none";
  deleteInvoiceButton.disabled = !mayDeleteDuplicate;
  if (displayedInvoiceId !== invoice.id) {
    // Only (re)populate editable AI fields when the
    // displayed invoice actually changes. showInvoice() is also
    // called on every refresh (loadInvoices polls every 120s
    // and after invoice activity seen by the 30s activity poll
    // or live events); without this guard it would keep stomping on
    // whatever the user is actively typing into these fields
    // with the invoice's last-saved (often blank) values.
    previewCompanyWanted = invoice.company || "";
    setValue("preview-company", previewCompanyWanted);
    displayedInvoiceId = invoice.id;
    // Fill every field for this invoice before waiting on the
    // supplier list: Confirm sends whatever the form holds to
    // displayedInvoiceId, so it must never hold the previous
    // invoice's values. The supplier stays empty (Confirm refuses)
    // until its list has loaded.
    setValue("preview-supplier-invoice", invoice.supplier_invoice_number);
    setValue("preview-po", invoice.po_number);
    setValue("preview-invoice-date", invoice.invoice_date);
    setValue("preview-value", invoice.invoice_value);
    setValue("preview-currency", invoice.currency || "GBP");
    setValue("correction-reason", invoice.correction_reason);
    applyPreviewConfidence(invoice);
    pdfFrame.src = `/api/invoices/${invoice.id}/pdf`;
    const supplierSelect = document.getElementById("preview-supplier");
    supplierSelect.innerHTML = '<option value="">Loading suppliers…</option>';
    supplierSelect.disabled = true;
    supplierSelect.dataset.loading = "true";
    try {
      await loadSuppliers(invoice.company || "", invoice.supplier || "");
    } catch (error) {
      // Network failure: handled below like a failed response.
    }
    if (displayedInvoiceId !== invoice.id) return;
    if (supplierSelect.dataset.loading) {
      // The list did not load; fill the form again on the next refresh.
      displayedInvoiceId = null;
    } else {
      // Not awaited: the PDF panel must not wait on this check.
      offerCompanyChoice(invoice).catch(() => {});
    }
  }
  pdfFrame.style.display = "block";
  pdfEmpty.style.display = "none";
}

// When the extracted company has no approval route for the
// supplier, ask which of the companies that do is invoiced.
// Asked once per invoice per page load, so reopening it does not
// ask again over a choice already made.
const companyChoiceOffered = new Set();
async function offerCompanyChoice(invoice) {
  if (!invoice.supplier || companyChoiceOffered.has(invoice.id)) return;
  const response = await fetch(`/api/invoices/${invoice.id}/company-options`);
  if (!response.ok) return;
  const options = await response.json();
  if (options.has_route || !options.companies.length) return;
  if (displayedInvoiceId !== invoice.id) return;
  companyChoiceOffered.add(invoice.id);
  const dialog = document.getElementById("company-choice-dialog");
  document.getElementById("company-choice-context").textContent =
    (invoice.company
      ? `${options.supplier} has no approval route under ${invoice.company}.`
      : `The company could not be read from this ${options.supplier} invoice.`) +
    " Check who the invoice is addressed to and choose the company.";
  document.getElementById("company-choice-pdf").src =
    `/api/invoices/${invoice.id}/pdf`;
  const container = document.getElementById("company-choice-options");
  container.innerHTML = "";
  for (const company of options.companies) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = company;
    button.onclick = async () => {
      dialog.close();
      if (displayedInvoiceId !== invoice.id) return;
      previewCompanyWanted = company;
      setValue("preview-company", company);
      await loadSuppliers(company, options.supplier);
    };
    container.appendChild(button);
  }
  dialog.showModal();
}

function isFlaggedInvoice(invoice) {
  return invoice.status === "Needs Review";
}

function incomingInvoices() {
  return invoices.filter(i =>
    i.document_type !== "statement" &&
    SECTION_STATUSES["incoming"].includes(i.status)
  );
}

function clearInvoicePreview() {
  picker.innerHTML = '<option value="">No invoices awaiting review</option>';
  displayedInvoiceId = null;
  documentBadge.textContent = "No invoice selected";
  processingBadge.textContent = "Awaiting invoice";
  [
    "source-sender",
    "source-received",
    "source-subject",
    "processing-status",
    "ai-confidence",
    "preview-irj",
    "review-warnings",
    "extraction-model",
    "extraction-prompt-version",
    "routing-explanation",
    "correction-reason",
    "preview-company",
    "preview-supplier-invoice",
    "preview-po",
    "preview-invoice-date",
    "preview-value",
  ].forEach(id => setValue(id, ""));
  setValue("preview-currency", "GBP");
  document.getElementById("ai-confidence-percent").textContent = "—";
  document.getElementById("preview-supplier").innerHTML =
    '<option value="">Select a company first…</option>';
  document.getElementById("preview-supplier").disabled = true;
  document.getElementById("duplicate-warning").style.display = "none";
  document.getElementById("override-duplicate-button").style.display = "none";
  document.getElementById("cancel-duplicate-button").style.display = "none";
  document.getElementById("retry-approval-route-button").style.display = "none";
  document.getElementById("delete-invoice-button").disabled = true;
  document.getElementById("supplier-match-prompt").style.display = "none";
  document.getElementById("confirm-supplier-match").dataset.supplier = "";
  document.querySelectorAll(".review-card label.low-confidence").forEach(label => {
    const control = label.querySelector("input, select");
    if (control) markFieldConfidence(control, null);
  });
  pdfFrame.removeAttribute("src");
  pdfFrame.style.display = "none";
  pdfEmpty.style.display = "grid";
}

async function refreshPicker() {
  const candidates = incomingInvoices();
  const openedFlaggedInvoice = invoices.find(
    invoice =>
      String(invoice.id) === String(openedFlaggedInvoiceId) &&
      isFlaggedInvoice(invoice) &&
      invoice.document_type === "invoice"
  );
  if (openedFlaggedInvoice) {
    candidates.unshift(openedFlaggedInvoice);
  } else {
    openedFlaggedInvoiceId = null;
  }
  if (!candidates.length) {
    clearInvoicePreview();
    return;
  }
  const selectedId = picker.value;
  picker.innerHTML = "";
  for (const invoice of candidates) {
    const option = document.createElement("option");
    option.value = invoice.id;
    option.textContent =
      `${openedFlaggedInvoice === invoice ? "[Flagged] " : ""}` +
      `${invoice.original_filename} — ${invoice.subject || "No subject"}`;
    picker.appendChild(option);
  }
  const flagged = candidates.find(
    invoice => String(invoice.id) === String(openedFlaggedInvoiceId)
  );
  const current = candidates.find(
    invoice => String(invoice.id) === selectedId
  );
  const selected = (showFlaggedNext && flagged) || current || flagged || candidates[0];
  showFlaggedNext = false;
  picker.value = String(selected.id);
  await showInvoice(selected);
}

picker.addEventListener("change", async () => {
  const selected = invoices.find(
    invoice => String(invoice.id) === picker.value
  );
  if (!selected) return;
  try {
    await showInvoice(selected);
  } catch (error) {
    showToast(error.message, true);
  }
});

async function loadInvoices() {
  const requestSequence = ++invoiceRefreshSequence;
  try {
    const response = await fetch(
      "/api/invoices?limit=500",
      { cache: "no-store" }
    );
    if (!response.ok) {
      showToast("Received invoices could not be loaded.", true);
      replaceLoadingSkeletons();
      return;
    }
    const refreshedInvoices = await response.json();
    if (requestSequence !== invoiceRefreshSequence) return;
    const refreshedSnapshot = JSON.stringify(refreshedInvoices);
    if (refreshedSnapshot === renderedInvoiceSnapshot) return;
    renderedInvoiceSnapshot = refreshedSnapshot;
    invoices = refreshedInvoices;
    renderAllSections();
    updateCounts();
    await refreshPicker();
  } catch (error) {
    showToast(
      "Received invoices could not be loaded. Retrying automatically.",
      true
    );
    replaceLoadingSkeletons();
  }
}

// Tables show skeleton rows until their first render. If the very
// first load fails, swap them for an explanation instead of
// leaving a loading state on screen indefinitely.
function replaceLoadingSkeletons() {
  document.querySelectorAll(".tab-panel .content > .skeleton").forEach(skeleton => {
    skeleton.parentElement.innerHTML = emptyStateHtml(
      "Invoices could not be loaded",
      "The list will refresh automatically. Check your connection if this persists."
    );
  });
}

function emptyStateHtml(title, detail, tone = "") {
  const icon = tone === "error"
    ? '<span class="empty-state-icon" aria-hidden="true">!</span>'
    : '<span class="empty-state-icon" aria-hidden="true"><svg><use href="#icon-inbox"/></svg></span>';
  return `<div class="empty-state${tone === "error" ? " is-error" : ""}"${tone === "error" ? ' role="alert"' : ""}>` +
    icon +
    `<strong>${escapeHtml(title)}</strong>` +
    (detail ? `<p>${escapeHtml(detail)}</p>` : "") +
    "</div>";
}

function loadingStateHtml(message) {
  return '<div class="loading-state" role="status">' +
    `<span class="spinner" aria-hidden="true"></span>${escapeHtml(message)}</div>`;
}

async function loadCompanies() {
  const response = await fetch("/api/companies");
  if (!response.ok) return;
  const companies = await response.json();
  invoiceCompanies = companies;
  const select = document.getElementById("preview-company");
  const keepCompany = select.value || previewCompanyWanted;
  const searchSelect = document.getElementById("invoice-search-company");
  const keepSearchCompany = searchSelect.value;
  select.innerHTML = '<option value="">Select company…</option>';
  searchSelect.innerHTML = '<option value="">All companies</option>';
  for (const company of companies) {
    const option = document.createElement("option");
    option.value = company.name;
    option.textContent = company.name;
    select.appendChild(option);
    const searchOption = option.cloneNode(true);
    searchSelect.appendChild(searchOption);
  }
  // Restore selections that existed before the options were rebuilt.
  if (keepCompany) select.value = keepCompany;
  if (keepSearchCompany) searchSelect.value = keepSearchCompany;
  // The statement-company selects in the section tables are
  // filled from invoiceCompanies; the company names are part of
  // the render context, so this only rebuilds tables when the
  // list changed. Skip it until invoices have loaded.
  if (renderedInvoiceSnapshot !== null) renderAllSections();
}

async function loadSearchSuppliers(company = "") {
  const select = document.getElementById("invoice-search-supplier");
  const response = await fetch(
    company
      ? `/api/suppliers?company=${encodeURIComponent(company)}`
      : "/api/suppliers"
  );
  if (!response.ok) return;
  const suppliers = await response.json();
  select.innerHTML = '<option value="">All suppliers</option>';
  for (const supplier of suppliers) {
    const option = document.createElement("option");
    option.value = supplier.name;
    option.textContent = supplier.name;
    select.appendChild(option);
  }
}

document.getElementById("invoice-search-company").addEventListener(
  "change",
  event => loadSearchSuppliers(event.target.value)
);

async function loadSuppliers(
  company = document.getElementById("preview-company").value,
  selectedSupplier = document.getElementById("preview-supplier").value
) {
  const requestSequence = ++supplierRequestSequence;
  const select = document.getElementById("preview-supplier");
  if (!company) {
    select.innerHTML = '<option value="">Select a company first…</option>';
    select.disabled = true;
    delete select.dataset.loading;
    hideSupplierMatchPrompt();
    return;
  }
  const response = await fetch(
    `/api/suppliers?company=${encodeURIComponent(company)}`
  );
  if (!response.ok) return;
  const suppliers = await response.json();
  if (requestSequence !== supplierRequestSequence) return;
  delete select.dataset.loading;
  select.disabled = false;
  select.innerHTML = '<option value="">Select supplier…</option>';
  for (const supplier of suppliers) {
    const option = document.createElement("option");
    option.value = supplier.name;
    option.textContent = supplier.name;
    select.appendChild(option);
  }
  const configuredSupplier = suppliers.find(supplier =>
    supplier.name.localeCompare(
      selectedSupplier,
      undefined,
      { sensitivity: "accent" }
    ) === 0 ||
    (supplier.aliases || []).some(alias =>
      alias.localeCompare(
        selectedSupplier,
        undefined,
        { sensitivity: "accent" }
      ) === 0
    )
  );
  if (configuredSupplier) {
    select.value = configuredSupplier.name;
    hideSupplierMatchPrompt();
  } else if (selectedSupplier) {
    select.value = "";
    showSupplierMatchPrompt(selectedSupplier, company, suppliers);
  } else {
    select.value = "";
    hideSupplierMatchPrompt();
  }
}

document.getElementById("preview-company").addEventListener(
  "change",
  event => {
    previewCompanyWanted = event.target.value;
    return loadSuppliers(event.target.value, "");
  }
);
document.getElementById("preview-supplier").addEventListener(
  "change",
  event => {
    if (event.target.value) hideSupplierMatchPrompt();
  }
);

function normalizedSupplierName(value) {
  return String(value || "")
    .toLocaleLowerCase()
    .replace(/&/g, " and ")
    .replace(/\b(limited|ltd|plc|inc|llc|company|co)\b/g, "")
    .replace(/[^a-z0-9]/g, "");
}

function supplierNameSimilarity(left, right) {
  const a = normalizedSupplierName(left);
  const b = normalizedSupplierName(right);
  if (!a || !b) return 0;
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const pairs = value => {
    const counts = new Map();
    for (let index = 0; index < value.length - 1; index += 1) {
      const pair = value.slice(index, index + 2);
      counts.set(pair, (counts.get(pair) || 0) + 1);
    }
    return counts;
  };
  const aPairs = pairs(a);
  const bPairs = pairs(b);
  let overlap = 0;
  for (const [pair, count] of aPairs) {
    overlap += Math.min(count, bPairs.get(pair) || 0);
  }
  return (2 * overlap) / (a.length + b.length - 2);
}

function suggestedSupplier(extractedSupplier, suppliers) {
  let best = null;
  let bestScore = 0;
  for (const supplier of suppliers) {
    for (const candidate of [supplier.name, ...(supplier.aliases || [])]) {
      const score = supplierNameSimilarity(extractedSupplier, candidate);
      if (score > bestScore) {
        best = supplier;
        bestScore = score;
      }
    }
  }
  return bestScore >= 0.62 ? best : null;
}

function hideSupplierMatchPrompt() {
  const prompt = document.getElementById("supplier-match-prompt");
  prompt.style.display = "none";
  document.getElementById("confirm-supplier-match").dataset.supplier = "";
}

function showSupplierMatchPrompt(extractedSupplier, company, suppliers) {
  const prompt = document.getElementById("supplier-match-prompt");
  const text = document.getElementById("supplier-match-text");
  const confirm = document.getElementById("confirm-supplier-match");
  const suggestion = suggestedSupplier(extractedSupplier, suppliers);
  if (suggestion) {
    text.textContent =
      `We extracted “${extractedSupplier}”. Is this “${suggestion.name}” for ${company}?`;
    confirm.textContent = `Yes — use ${suggestion.name}`;
    confirm.dataset.supplier = suggestion.name;
    confirm.style.display = "";
  } else {
    text.textContent =
      `“${extractedSupplier}” is not registered for ${company}. Select the correct supplier above or ask an administrator to add it.`;
    confirm.dataset.supplier = "";
    confirm.style.display = "none";
  }
  prompt.style.display = "grid";
}

document.getElementById("confirm-supplier-match").addEventListener(
  "click",
  event => {
    const supplier = event.currentTarget.dataset.supplier;
    if (!supplier) return;
    document.getElementById("preview-supplier").value = supplier;
    hideSupplierMatchPrompt();
    showToast(`Supplier confirmed as ${supplier}.`);
  }
);

function updateCounts() {
  for (const tab of Object.keys(SECTION_STATUSES)) {
    const count = tab === "incoming"
      ? incomingInvoices().length
      : invoices.filter(i =>
          SECTION_STATUSES[tab].includes(i.status) &&
          (tab !== "on-hold" || i.status.includes("On Hold") || i.hold_level)
        ).length;
    const el = document.getElementById(`count-${tab}`);
    if (el) {
      el.textContent = String(count);
      el.classList.toggle("has-items", count > 0);
    }
  }
}

function fieldConfidences(invoice) {
  if (!invoice.ai_field_confidences) return {};
  try {
    const parsed = JSON.parse(invoice.ai_field_confidences);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function analysisValue(value) {
  return value === null || value === undefined || value === ""
    ? "—"
    : escapeHtml(value);
}

function formatUkTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-GB", {
    timeZone: "Europe/London",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

function analysisField(label, value, confidence = null) {
  const confidenceBadge = confidence === null || confidence === undefined
    ? ""
    : `<span class="field-confidence${Number(confidence) < 0.8 ? " low" : ""}" ` +
      `title="AI confidence">${Math.round(Number(confidence) * 100)}%</span>`;
  return `<dt>${escapeHtml(label)}</dt><dd>${analysisValue(value)}${confidenceBadge}</dd>`;
}

function invoiceHistoryHtml(invoice) {
  const entries = [];
  if (invoice.review_reason) {
    entries.push(["Flagged / review", invoice.review_reason]);
  }
  if (invoice.po_query_notes) {
    const context = [
      invoice.po_query_category && `Category: ${invoice.po_query_category}`,
      invoice.po_query_contact && `Contact: ${invoice.po_query_contact}`,
    ].filter(Boolean).join(" · ");
    entries.push([
      "PO query",
      `${invoice.po_query_notes}${context ? `\n${context}` : ""}`,
    ]);
  }
  if (invoice.hold_reason) {
    entries.push(["Approval query / on hold", invoice.hold_reason]);
  }
  if (
    invoice.approver1_comments &&
    invoice.approver1_comments !== invoice.hold_reason
  ) {
    entries.push([
      `${invoice.approver1_name || "Approval"} note`,
      invoice.approver1_comments,
    ]);
  }
  if (
    invoice.approver2_comments &&
    invoice.approver2_comments !== invoice.hold_reason
  ) {
    entries.push([
      `${invoice.approver2_name || "Approval"} note`,
      invoice.approver2_comments,
    ]);
  }
  if (invoice.reconciliation_notes) {
    entries.push(["Reconciliation note", invoice.reconciliation_notes]);
  }
  if (invoice.rejection_reason) {
    entries.push(["Rejection reason", invoice.rejection_reason]);
  }
  if (invoice.cancellation_reason) {
    entries.push(["Cancellation reason", invoice.cancellation_reason]);
  }
  if (!entries.length) {
    return '<div class="mini-description">No queries or notes recorded.</div>';
  }
  return `<div class="analysis-history">${entries.map(([label, value]) =>
    `<div class="analysis-history-item"><strong>${escapeHtml(label)}</strong>${escapeHtml(value)}</div>`
  ).join("")}</div>`;
}

function invoiceAuditHtml(invoice) {
  const events = Array.isArray(invoice.audit_trail)
    ? invoice.audit_trail
    : [];
  if (!events.length) {
    return '<div class="mini-description">No audit events recorded.</div>';
  }
  return `<div class="analysis-history">${events.map(event => {
    const timestamp = event.created_at
      ? formatUkTimestamp(event.created_at)
      : "Unknown time";
    return `<div class="analysis-history-item">` +
      `<strong>${escapeHtml(timestamp)} · ${escapeHtml(event.event_type)}</strong>` +
      `${escapeHtml(event.message)}</div>`;
  }).join("")}</div>`;
}

function correctionFeedbackHtml(invoice) {
  if (!invoice.corrected_fields_json) return "";
  try {
    const corrections = JSON.parse(invoice.corrected_fields_json);
    const rows = Object.entries(corrections).map(([field, values]) =>
      `<div class="analysis-history-item"><strong>${escapeHtml(field.replaceAll("_", " "))}</strong>` +
      `${analysisValue(values.original)} → ${analysisValue(values.corrected)}</div>`
    ).join("");
    return `<h4>Human correction feedback</h4><div class="analysis-history">${rows}</div>`;
  } catch (error) {
    return "";
  }
}

// Search results pass includePdf = false: a search can match
// hundreds of invoices, and each embedded PDF would load at once.
function invoiceAnalysisHtml(invoice, includePdf = true) {
  const confidences = fieldConfidences(invoice);
  const overallConfidence = invoice.ai_confidence === null ||
      invoice.ai_confidence === undefined
    ? "—"
    : `${Math.round(Number(invoice.ai_confidence) * 100)}%`;
  const warnings = invoice.ai_review_warnings || invoice.review_reason;
  return `
                <div class="invoice-analysis">
                  ${includePdf ? `<iframe
                    class="invoice-analysis-pdf"
                    src="/api/invoices/${invoice.id}/pdf"
                    title="${invoice.document_type === "statement" ? "Statement" : "Invoice"} ${escapeAttr(invoice.irj_number || invoice.original_filename)} PDF"
                  ></iframe>` : ""}
                  <section class="invoice-analysis-fields" aria-label="Invoice analysis">
                    <h3>${invoice.document_type === "statement" ? "Statement classification" : "Extracted invoice analysis"}</h3>
                    <h4>Workflow</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Status", invoice.status)}
                      ${analysisField("Document type", invoice.document_type || "invoice")}
                      ${analysisField(
            "Classification confidence",
            invoice.document_classification_confidence == null
              ? null
              : `${Math.round(Number(invoice.document_classification_confidence) * 100)}%`
          )}
                      ${analysisField("IRJ number", invoice.irj_number)}
                      ${analysisField("Invoice type", invoice.invoice_type)}
                      ${analysisField("Overall confidence", overallConfidence)}
                      ${analysisField("Extraction model", invoice.extraction_model)}
                      ${analysisField("Extraction version", invoice.extraction_prompt_version)}
                      ${analysisField("Routing explanation", invoice.routing_explanation)}
                    </dl>
                    <h4>Extracted fields</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Company", invoice.company, confidences.company)}
                      ${analysisField("Supplier", invoice.supplier, confidences.supplier)}
                      ${analysisField("Supplier invoice no.", invoice.supplier_invoice_number, confidences.supplier_invoice_number)}
                      ${analysisField("PO number", invoice.po_number, confidences.purchase_order_number)}
                      ${analysisField("Invoice date", invoice.invoice_date ? formatUkDate(invoice.invoice_date) : null, confidences.invoice_date)}
                      ${analysisField("Invoice value", invoice.invoice_value == null || invoice.invoice_value === "" ? null : formatMoney(invoice.invoice_value, invoice.currency), confidences.invoice_value)}
                      ${analysisField("Currency", invoice.currency, confidences.currency)}
                    </dl>
                    ${correctionFeedbackHtml(invoice)}
                    ${invoice.reviewed_by ? `<dl class="invoice-analysis-list">
                      ${analysisField("Reviewed by", invoice.reviewed_by)}
                      ${analysisField("Reviewed at", formatUkTimestamp(invoice.reviewed_at))}
                      ${analysisField("Correction reason", invoice.correction_reason)}
                    </dl>` : ""}
                    <h4>Source</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Filename", invoice.original_filename)}
                      ${analysisField("Sender", invoice.sender_address || invoice.sender_name)}
                      ${analysisField("Received", formatUkTimestamp(invoice.received_at))}
                      ${analysisField("Subject", invoice.subject)}
                    </dl>
                    ${invoice.approver1_name ? `
                      <h4>Processing</h4>
                      <dl class="invoice-analysis-list">
                        ${analysisField("Approver 1", invoice.approver1_name)}
                        ${analysisField("Approver 1 decision", invoice.approver1_decision)}
                        ${analysisField("Approver 1 date", formatUkTimestamp(invoice.approver1_date))}
                        ${analysisField("Approver 2", invoice.approver2_name)}
                        ${analysisField("Approver 2 decision", invoice.approver2_decision)}
                        ${analysisField("Approver 2 date", formatUkTimestamp(invoice.approver2_date))}
                      </dl>
                    ` : ""}
                    ${invoice.payment_date || invoice.reconciliation_date || invoice.reconciled_at ? `
                      <h4>Payment and reconciliation</h4>
                      <dl class="invoice-analysis-list">
                        ${analysisField("Payment date", invoice.payment_date ? formatUkDate(invoice.payment_date) : null)}
                        ${analysisField("Payment method", invoice.payment_method)}
                        ${analysisField("Payment reference", invoice.payment_reference)}
                        ${analysisField("Paid by", invoice.paid_by)}
                        ${analysisField("Bank statement date", invoice.reconciliation_date ? formatUkDate(invoice.reconciliation_date) : null)}
                        ${analysisField("Marked reconciled", formatUkTimestamp(invoice.reconciled_at))}
                        ${analysisField("Reconciled by", invoice.reconciled_by)}
                      </dl>
                    ` : ""}
                    <h4>Description and history</h4>
                    ${invoiceHistoryHtml(invoice)}
                    ${Array.isArray(invoice.audit_trail) ? `
                      <h4>Audit trail</h4>
                      ${invoiceAuditHtml(invoice)}
                    ` : ""}
                    ${warnings ? `<div class="analysis-warning">${escapeHtml(warnings)}</div>` : ""}
                  </section>
                </div>
              `;
}

document.getElementById("flag-review-button").addEventListener("click", async () => {
  const invoiceId = picker.value;
  if (!invoiceId) return;
  const reason = await promptDialog({
    title: "Flag for review",
    label: "Reason for flagging this invoice for review",
    required: true,
    confirmLabel: "Flag for review",
  });
  if (!reason) return;
  try {
    await postJson(`/api/invoices/${invoiceId}/flag-review`, { reason });
    await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById("delete-invoice-button").addEventListener("click", async () => {
  const invoice = invoices.find(item => String(item.id) === picker.value);
  if (!invoice) return;
  if (!await confirmDialog({
    title: "Delete duplicate invoice?",
    message:
      `Permanently delete "${invoice.original_filename}" from the application ` +
      "and SharePoint? This cannot be undone.",
    confirmLabel: "Delete permanently",
    tone: "danger",
  })) return;
  try {
    await sendJson(`/api/invoices/${invoice.id}`, "DELETE");
    displayedInvoiceId = null;
    pdfFrame.removeAttribute("src");
    pdfFrame.style.display = "none";
    pdfEmpty.style.display = "grid";
    showToast("Invoice permanently deleted.");
    await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById("confirm-invoice-button").addEventListener("click", async event => {
  await withBusyButton(event.currentTarget, () => submitConfirm(false));
});

document.getElementById("override-duplicate-button").addEventListener("click", async event => {
  await withBusyButton(event.currentTarget, () => submitConfirm(true));
});

document.getElementById("cancel-duplicate-button").addEventListener("click", async () => {
  const invoiceId = picker.value;
  if (!invoiceId || !await confirmDialog({
    title: "Cancel duplicate invoice?",
    message: "Confirm this invoice is a duplicate and cancel it?",
    confirmLabel: "Cancel invoice",
    cancelLabel: "Keep invoice",
    tone: "danger",
  })) return;
  try {
    await postJson(`/api/invoices/${invoiceId}/cancel-duplicate`, {});
    showToast("Duplicate invoice cancelled.");
    await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById("retry-approval-route-button").addEventListener("click", async event => {
  const invoice = invoices.find(item => String(item.id) === picker.value);
  if (!invoice || !invoice.irj_number) return;
  await withBusyButton(event.currentTarget, async () => {
    try {
      await postJson(`/api/invoices/${invoice.id}/register-sage`, {
        irj_number: invoice.irj_number,
      });
      showToast("Approver route checked again.");
      await loadInvoices();
    } catch (error) {
      showToast(error.message, true);
    }
  });
});

async function submitConfirm(overrideDuplicate) {
  const invoiceId = displayedInvoiceId;
  if (!invoiceId) return;
  const company = document.getElementById("preview-company").value;
  const supplier = document.getElementById("preview-supplier").value;
  if (!company || !supplier) {
    showToast("Company and supplier are required.", true);
    return;
  }
  const body = {
    company,
    supplier,
    supplier_invoice_number: document.getElementById("preview-supplier-invoice").value || null,
    purchase_order_number: document.getElementById("preview-po").value || null,
    invoice_date: document.getElementById("preview-invoice-date").value || null,
    invoice_value: document.getElementById("preview-value").value
      ? Number(document.getElementById("preview-value").value)
      : null,
    currency: document.getElementById("preview-currency").value || "GBP",
    override_duplicate: overrideDuplicate,
    correction_reason: document.getElementById("correction-reason").value || null,
  };
  try {
    await postJson(`/api/invoices/${invoiceId}/confirm`, body);
    openedFlaggedInvoiceId = null;
    showToast("Invoice routed successfully.");
    await loadInvoices();
  } catch (error) {
    showToast(error.message, true);
  }
}

document.getElementById("manual-upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const statusEl = document.getElementById("manual-upload-status");
  const fileInput = document.getElementById("manual-upload-file");
  const file = fileInput.files[0];
  statusEl.className = "";
  statusEl.textContent = "";
  if (!file) {
    statusEl.className = "error";
    statusEl.textContent = "Choose a PDF file first.";
    return;
  }
  const formData = new FormData();
  formData.append("file", file);
  formData.append("sender_name", document.getElementById("manual-upload-sender-name").value);
  formData.append("sender_address", document.getElementById("manual-upload-sender-address").value);
  formData.append("subject", document.getElementById("manual-upload-subject").value);
  const submitButton = document.getElementById("manual-upload-submit");
  submitButton.disabled = true;
  submitButton.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/invoices/manual-upload", { method: "POST", body: formData });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `Upload failed (${response.status}).`);
    }
    const record = await response.json();
    const destination = record.status === "Needs Review"
      ? "Flagged"
      : record.status === "Awaiting AI Extraction"
        ? "Incoming Invoices"
        : record.status;
    statusEl.className = "success";
    statusEl.textContent = `Uploaded '${record.original_filename}' to ${destination}.`;
    event.target.reset();
    showToast(`Invoice manually added to ${destination}.`);
    if (record.status === "Needs Review") {
      openedFlaggedInvoiceId = record.id;
      showFlaggedNext = true;
    }
    await loadInvoices();
    if (record.status === "Needs Review") {
      activateTab("needs-review");
    }
  } catch (error) {
    statusEl.className = "error";
    statusEl.textContent = error.message;
  } finally {
    submitButton.disabled = false;
    submitButton.removeAttribute("aria-busy");
  }
});
