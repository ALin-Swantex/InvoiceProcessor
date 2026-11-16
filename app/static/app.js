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

            function activateTab(tab) {
              const button = document.querySelector(
                `#section-nav button[data-tab="${tab}"]`
              );
              if (!button || button.style.display === "none") return false;
              currentTab = tab;
              storeValue(ACTIVE_TAB_STORAGE_KEY, tab);
              document.querySelectorAll("#section-nav button").forEach(item => {
                item.classList.toggle("active", item === button);
              });
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
                element.style.outline = confidence != null && Number(confidence) < 0.8
                  ? "2px solid #d97706"
                  : "";
              }
            }

            function showToast(message, isError) {
              const toast = document.createElement("div");
              toast.className = "toast" + (isError ? " error" : "");
              toast.textContent = message;
              toastContainer.appendChild(toast);
              setTimeout(() => toast.remove(), 5000);
            }

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
                // called on every periodic refresh (loadInvoices runs every
                // 5s); without this guard it would keep stomping on
                // whatever the user is actively typing into these fields
                // with the invoice's last-saved (often blank) values.
                setValue("preview-company", invoice.company || "");
                displayedInvoiceId = invoice.id;
                await loadSuppliers(invoice.company || "", invoice.supplier || "");
                if (displayedInvoiceId !== invoice.id) return;
                setValue("preview-supplier-invoice", invoice.supplier_invoice_number);
                setValue("preview-po", invoice.po_number);
                setValue("preview-invoice-date", invoice.invoice_date);
                setValue("preview-value", invoice.invoice_value);
                setValue("preview-currency", invoice.currency || "GBP");
                setValue("correction-reason", invoice.correction_reason);
                applyPreviewConfidence(invoice);
                pdfFrame.src = `/api/invoices/${invoice.id}/pdf`;
              }
              pdfFrame.style.display = "block";
              pdfEmpty.style.display = "none";
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
              const selected = candidates.find(
                invoice => String(invoice.id) === String(openedFlaggedInvoiceId)
              ) || candidates.find(
                invoice => String(invoice.id) === selectedId
              ) || candidates[0];
              picker.value = String(selected.id);
              await showInvoice(selected);
            }

            picker.addEventListener("change", async () => {
              const selected = invoices.find(
                invoice => String(invoice.id) === picker.value
              );
              if (selected) await showInvoice(selected);
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
              }
            }

            async function loadCompanies() {
              const response = await fetch("/api/companies");
              if (!response.ok) return;
              const companies = await response.json();
              invoiceCompanies = companies;
              const select = document.getElementById("preview-company");
              select.innerHTML = '<option value="">Select company…</option>';
              const searchSelect = document.getElementById("invoice-search-company");
              searchSelect.innerHTML = '<option value="">All companies</option>';
              for (const company of companies) {
                const option = document.createElement("option");
                option.value = company.name;
                option.textContent = company.name;
                select.appendChild(option);
                const searchOption = option.cloneNode(true);
                searchSelect.appendChild(searchOption);
              }
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
                hideSupplierMatchPrompt();
                return;
              }
              const response = await fetch(
                `/api/suppliers?company=${encodeURIComponent(company)}`
              );
              if (!response.ok) return;
              const suppliers = await response.json();
              if (requestSequence !== supplierRequestSequence) return;
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
              event => loadSuppliers(event.target.value, "")
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
                if (el) el.textContent = String(count);
              }
            }

            function escapeHtml(value) {
              const div = document.createElement("div");
              div.textContent = value == null ? "" : String(value);
              return div.innerHTML;
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
                : `<span class="field-confidence">${Math.round(Number(confidence) * 100)}%</span>`;
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

            function invoiceAnalysisHtml(invoice) {
              const confidences = fieldConfidences(invoice);
              const overallConfidence = invoice.ai_confidence === null ||
                  invoice.ai_confidence === undefined
                ? "—"
                : `${Math.round(Number(invoice.ai_confidence) * 100)}%`;
              const warnings = invoice.ai_review_warnings || invoice.review_reason;
              return `
                <div class="invoice-analysis">
                  <iframe
                    class="invoice-analysis-pdf"
                    src="/api/invoices/${invoice.id}/pdf"
                    title="${invoice.document_type === "statement" ? "Statement" : "Invoice"} ${escapeHtml(invoice.irj_number || invoice.original_filename)} PDF"
                  ></iframe>
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
                      ${analysisField("Invoice date", invoice.invoice_date, confidences.invoice_date)}
                      ${analysisField("Invoice value", invoice.invoice_value, confidences.invoice_value)}
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
                        ${analysisField("Payment date", invoice.payment_date)}
                        ${analysisField("Payment method", invoice.payment_method)}
                        ${analysisField("Payment reference", invoice.payment_reference)}
                        ${analysisField("Paid by", invoice.paid_by)}
                        ${analysisField("Bank statement date", invoice.reconciliation_date)}
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

            function invoiceRowHtml(invoice, columns, actionsFn) {
              const expanded = expandedInvoiceIds.has(String(invoice.id));
              let html = `<tr class="invoice-summary-row" data-expand-invoice="${invoice.id}" ` +
                `tabindex="0" aria-expanded="${expanded}" title="Click to view invoice analysis">`;
              for (const column of columns) {
                const value = escapeHtml(column.value(invoice));
                html += column.boxed
                  ? `<td class="invoice-description-cell"><div class="mini-description">${value}</div></td>`
                  : `<td>${value}</td>`;
              }
              html += `<td class="row-actions"><div class="row-action-buttons">` +
                `${actionsFn(invoice)}</div>` +
                `<span class="invoice-expand-hint">${expanded ? "Hide" : "View"} PDF and extracted fields</span></td>`;
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
                container.innerHTML = '<div class="empty-state">No documents in this section.</div>';
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
              let html = '<table class="section-table"><thead><tr>';
              for (const column of columns) html += `<th>${column.label}</th>`;
              html += "<th>Actions</th></tr></thead><tbody>";
              for (const invoice of rows) {
                html += invoiceRowHtml(invoice, columns, actionsFn);
              }
              html += "</tbody></table>";
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

            const BASE_COLUMNS = [
              { label: "Type", value: i => i.document_type === "statement" ? "Statement" : "Invoice" },
              { label: "IRJ", value: i => i.irj_number || "—" },
              { label: "Company", value: i => i.company || "—" },
              { label: "Supplier", value: i => i.supplier || "—" },
              { label: "File", value: i => i.original_filename },
              { label: "Status", value: i => i.status },
              { label: "Description", value: invoiceDescription, boxed: true },
            ];

            function pdfLinkButton(invoice) {
              return `<a class="action-link" href="/api/invoices/${invoice.id}/pdf" ` +
                `target="_blank" rel="noopener">View PDF</a>`;
            }

            document.getElementById("invoice-search-form").addEventListener(
              "submit",
              async event => {
                event.preventDefault();
                const query = document.getElementById("invoice-search-irj").value.trim();
                const company = document.getElementById("invoice-search-company").value;
                const supplier = document.getElementById("invoice-search-supplier").value;
                const result = document.getElementById("invoice-search-result");
                result.className = "empty-state";
                result.textContent = "Searching…";
                try {
                  if (!query && !company && !supplier) {
                    throw new Error("Select a company or supplier, or enter an IRJ number.");
                  }
                  if (query && (query.length !== 6 || !/^\d+$/.test(query))) {
                    throw new Error("An IRJ number must contain exactly six digits.");
                  }
                  const params = new URLSearchParams();
                  if (query) params.set("irj_number", query);
                  if (company) params.set("company", company);
                  if (supplier) params.set("supplier", supplier);
                  const response = await fetch(
                    `/api/invoice-search/filter?${params}`
                  );
                  if (!response.ok) {
                    const body = await response.json().catch(() => ({}));
                    throw new Error(body.detail || "Invoice search failed.");
                  }
                  const matches = await response.json();
                  if (!matches.length) {
                    result.textContent = "No matching invoices were found.";
                    return;
                  }
                  result.className = "";
                  result.innerHTML = matches.map(invoice =>
                    `<div class="metric-panel"><div class="actions">${pdfLinkButton(invoice)}</div>` +
                    `${invoiceAnalysisHtml(invoice)}</div>`
                  ).join("");
                } catch (error) {
                  result.textContent = error.message;
                  showToast(error.message, true);
                }
              }
            );

            let statementLibrary = {};

            function renderStatementList(company) {
              const container = document.getElementById("statement-list");
              if (!company) {
                container.className = "empty-state";
                container.textContent = "Select a company to view its statements.";
                return;
              }
              const statements = statementLibrary[company] || [];
              if (!statements.length) {
                container.className = "empty-state";
                container.textContent = `No PDF statements were found for ${company}.`;
                return;
              }
              container.className = "";
              container.innerHTML =
                '<table class="section-table"><thead><tr>' +
                "<th>Statement</th><th>Modified</th><th>Size</th><th>Action</th>" +
                "</tr></thead><tbody>" +
                statements.map(statement => `
                  <tr>
                    <td>${escapeHtml(statement.name)}</td>
                    <td>${escapeHtml(statement.lastModifiedDateTime || statement.createdDateTime || "—")}</td>
                    <td>${statement.size == null ? "—" : `${Math.ceil(Number(statement.size) / 1024)} KB`}</td>
                    <td><a class="action-link" href="/api/statements/${encodeURIComponent(statement.id)}/pdf" target="_blank" rel="noopener">View PDF</a></td>
                  </tr>
                `).join("") +
                "</tbody></table>";
            }

            async function loadStatements() {
              const companySelect = document.getElementById("statement-company");
              const container = document.getElementById("statement-list");
              container.className = "empty-state";
              container.textContent = "Loading statements from SharePoint…";
              const response = await fetch("/api/statements");
              if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                container.textContent =
                  body.detail || "Statements could not be loaded from SharePoint.";
                return;
              }
              statementLibrary = await response.json();
              const selected = companySelect.value;
              companySelect.innerHTML = '<option value="">Select company…</option>';
              for (const company of Object.keys(statementLibrary)) {
                const option = document.createElement("option");
                option.value = company;
                option.textContent = company;
                companySelect.appendChild(option);
              }
              companySelect.value = Object.hasOwn(statementLibrary, selected)
                ? selected
                : "";
              renderStatementList(companySelect.value);
            }

            document.getElementById("statement-company").addEventListener(
              "change",
              event => renderStatementList(event.target.value)
            );

            function populateStatementCompanySelects(container) {
              container.querySelectorAll("[data-statement-company]").forEach(select => {
                const placeholder = document.createElement("option");
                placeholder.value = "";
                placeholder.textContent = "Select company…";
                select.appendChild(placeholder);
                for (const company of invoiceCompanies) {
                  const option = document.createElement("option");
                  option.value = company.name;
                  option.textContent = company.name;
                  select.appendChild(option);
                }
              });
            }

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
              if (!rows.length) return '<div class="empty-state">No matching invoices.</div>';
              return '<table class="section-table"><thead><tr>' +
                `<th>${escapeHtml(firstHeading)}</th><th>Invoices</th><th>Value</th>` +
                '</tr></thead><tbody>' +
                rows.map(row => `<tr><td>${escapeHtml(row.name)}</td>` +
                  `<td>${escapeHtml(row.count)}</td>` +
                  `<td>${escapeHtml(formatMetricMoney(row.value, row.currency))}</td></tr>`
                ).join("") + '</tbody></table>';
            }

            function dueDateTable(rows) {
              if (!rows.length) return '<div class="empty-state">None.</div>';
              return '<table class="section-table"><thead><tr>' +
                '<th>IRJ</th><th>Company</th><th>Supplier</th><th>Due</th><th>Value</th>' +
                '</tr></thead><tbody>' +
                rows.map(row => `<tr><td>${escapeHtml(row.irj_number || "—")}</td>` +
                  `<td>${escapeHtml(row.company)}</td><td>${escapeHtml(row.supplier)}</td>` +
                  `<td>${escapeHtml(row.due_date)}</td>` +
                  `<td>${escapeHtml(row.value == null ? "—" : formatMetricMoney(row.value, row.currency))}</td></tr>`
                ).join("") + '</tbody></table>';
            }

            async function loadMetrics() {
              const dashboard = document.getElementById("metrics-dashboard");
              const granularity = document.getElementById("metrics-granularity").value;
              dashboard.className = "empty-state";
              dashboard.textContent = "Loading metrics…";
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
                    ${dueDateTable(metrics.due_dates.overdue)}
                  </section>
                  <section class="metric-panel">
                    <h4>Due within 7 days (${metrics.due_dates.approaching.length})</h4>
                    ${dueDateTable(metrics.due_dates.approaching)}
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
                dashboard.className = "empty-state";
                dashboard.textContent = error.message;
              }
            }

            function operationsQueueTable(items, status) {
              if (!items.length) return '<div class="empty-state">None.</div>';
              return '<table class="section-table"><thead><tr>' +
                '<th>Received</th><th>Message</th><th>Attempts</th><th>Error</th><th></th>' +
                '</tr></thead><tbody>' + items.map(item => `<tr>` +
                  `<td>${escapeHtml(formatUkTimestamp(item.received_at))}</td>` +
                  `<td title="${escapeHtml(item.message_id)}">${escapeHtml(item.message_id)}</td>` +
                  `<td>${escapeHtml(item.attempts)}</td>` +
                  `<td>${escapeHtml(item.last_error || "—")}</td>` +
                  `<td>${status === "failed" ? `<button type="button" class="secondary" ` +
                    `data-retry-notification="${item.id}">Retry</button>` : ""}</td>` +
                  `</tr>`).join("") + '</tbody></table>';
            }

            async function loadOperations() {
              const dashboard = document.getElementById("operations-dashboard");
              dashboard.className = "empty-state";
              dashboard.textContent = "Loading worker and queue status…";
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
                dashboard.className = "empty-state";
                dashboard.textContent = error.message;
              }
            }

            function renderAllSections() {
              renderSectionTable(
                "needs-review-table",
                SECTION_STATUSES["needs-review"],
                [
                  ...BASE_COLUMNS,
                  { label: "Return stage", value: i => i.review_return_status || "Requires a dedicated resolution" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  ${invoice.document_type === "statement" ? `
                    <button data-action="mark-invoice" data-id="${invoice.id}" class="secondary">Treat as invoice</button>
                  ` : `
                    <button data-action="open-review" data-id="${invoice.id}" class="secondary">Open invoice review</button>
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
                [
                  ...BASE_COLUMNS,
                  { label: "PO number", value: i => i.po_number || "—" },
                ],
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
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="register-sage" data-id="${invoice.id}" data-irj="${escapeHtml(invoice.irj_number || "")}">Confirm Sage registration / IRJ</button>
                `
              );
              renderSectionTable(
                "approver1-table",
                SECTION_STATUSES["approver1"],
                BASE_COLUMNS,
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
                BASE_COLUMNS,
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
                [
                  ...BASE_COLUMNS,
                  { label: "Issue / payment note", value: i => i.hold_reason || "—" },
                  { label: "Payment method", value: i => i.payment_method || "—" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  ${invoice.status === "Payment Routing Issue / On Hold"
                    ? `<button data-action="retry-payment-route" data-id="${invoice.id}">Retry payment routing</button>`
                    : `<button data-action="resume" data-id="${invoice.id}">${invoice.status === "Workflow Issue / On Hold" ? "Resolve issue" : "Resume approval"}</button>`}
                `,
                invoice => invoice.status.includes("On Hold") || Boolean(invoice.hold_level)
              );
              renderSectionTable(
                "approved-table",
                SECTION_STATUSES["approved"],
                [
                  ...BASE_COLUMNS,
                  { label: "Payment method", value: i => i.payment_method || "Awaiting automatic routing" },
                ],
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
                  [
                    ...BASE_COLUMNS,
                    { label: "Payment route", value: i => i.payment_method || "—" },
                  ],
                  invoice => `
                    ${pdfLinkButton(invoice)}
                    <button data-action="pay" data-id="${invoice.id}">Record payment</button>
                  `
                );
              }
              renderSectionTable(
                "reconciliation-table",
                SECTION_STATUSES["reconciliation"],
                [
                  ...BASE_COLUMNS,
                  { label: "Payment date", value: i => i.payment_date || "—" },
                  { label: "Paid by", value: i => i.paid_by || "—" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="reconcile" data-id="${invoice.id}">Mark reconciled</button>
                `
              );
              renderSectionTable(
                "complete-table",
                SECTION_STATUSES["complete"],
                [
                  ...BASE_COLUMNS,
                  { label: "Marked reconciled", value: i => i.reconciled_at ? i.reconciled_at.slice(0, 10) : "—" },
                  { label: "Appeared on bank statement", value: i => i.reconciliation_date || "—" },
                  { label: "Completed by", value: i => i.reconciled_by || i.foreign_allocated_by || "—" },
                ],
                invoice => pdfLinkButton(invoice)
              );
              renderSectionTable(
                "rejected-table",
                SECTION_STATUSES["rejected"],
                BASE_COLUMNS,
                invoice => pdfLinkButton(invoice)
              );
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

            async function postJson(url, body) {
              return sendJson(url, "POST", body);
            }

            async function putJson(url, body) {
              return sendJson(url, "PUT", body);
            }

            async function collectDomesticPayment(invoice) {
              const response = await fetch(
                `/api/supplier-terms?company=${encodeURIComponent(invoice.company || "")}` +
                `&supplier=${encodeURIComponent(invoice.supplier || "")}`
              );
              const terms = response.ok ? await response.json() : {};
              const dialog = document.getElementById("payment-dialog");
              const form = document.getElementById("payment-form");
              const method = document.getElementById("payment-method");
              const account = document.getElementById("payment-supplier-account");
              const profiles = terms.profiles || [];
              account.innerHTML = profiles.length
                ? profiles.map(profile =>
                    `<option value="${escapeHtml(profile.supplier_account_number || "")}">` +
                    `${escapeHtml(profile.supplier_account_number || "No account number")} — ` +
                    `${escapeHtml(profile.bank_account || "No bank specified")}` +
                    `</option>`
                  ).join("")
                : '<option value="">No configured supplier account</option>';
              account.disabled = !profiles.length;
              const applyProfile = () => {
                const profile = profiles.find(item => item.supplier_account_number === account.value) || profiles[0] || {};
                method.value = invoice.payment_method || "";
                document.getElementById("payment-terms").value = profile.payment_terms_notice || "";
                document.getElementById("payment-bank-account").value = profile.bank_account || "";
              };
              account.onchange = applyProfile;
              applyProfile();
              document.getElementById("payment-date").value = new Date().toISOString().slice(0, 10);
              document.getElementById("payment-reference").value = "";
              document.getElementById("payment-supplier-context").textContent =
                invoice.supplier || "Supplier";

              return new Promise(resolve => {
                let settled = false;
                const finish = value => {
                  if (settled) return;
                  settled = true;
                  resolve(value);
                };
                form.onsubmit = event => {
                  event.preventDefault();
                  const payment = {
                    payment_date: document.getElementById("payment-date").value,
                    supplier_account_number: account.value || null,
                    payment_reference: document.getElementById("payment-reference").value,
                    payment_method: method.value,
                  };
                  dialog.close();
                  finish(payment);
                };
                document.getElementById("payment-cancel").onclick = () => dialog.close();
                dialog.onclose = () => finish(null);
                dialog.showModal();
              });
            }

            async function collectSageRegistration(invoice) {
              const dialog = document.getElementById("sage-registration-dialog");
              const form = document.getElementById("sage-registration-form");
              const irjInput = document.getElementById("sage-registration-irj");
              const errorEl = document.getElementById("sage-registration-error");
              document.getElementById("sage-registration-context").textContent =
                `${invoice.supplier || "Supplier"} — ${invoice.original_filename}`;
              irjInput.value = invoice.irj_number || "";
              const irjSetting = irjConfigurations.get(invoice.company);
              const isManual = irjSetting ? irjSetting.mode === "manual" : false;
              irjInput.readOnly = !isManual && Boolean(invoice.irj_number);
              irjInput.title = isManual
                ? "Enter the IRJ from the paper records."
                : "This IRJ was allocated automatically.";
              errorEl.textContent = "";

              return new Promise(resolve => {
                let settled = false;
                const finish = value => {
                  if (settled) return;
                  settled = true;
                  resolve(value);
                };
                form.onsubmit = event => {
                  event.preventDefault();
                  const irjNumber = irjInput.value.trim();
                  if (!/^\d{6}$/.test(irjNumber)) {
                    errorEl.textContent = "Enter exactly six digits.";
                    return;
                  }
                  dialog.close();
                  finish(irjNumber);
                };
                document.getElementById("sage-registration-cancel").onclick = () => dialog.close();
                dialog.onclose = () => finish(null);
                dialog.showModal();
                irjInput.focus();
              });
            }

            async function handleRowAction(button) {
              const action = button.dataset.action;
              const id = button.dataset.id;
              const originalText = button.textContent;
              button.disabled = true;
              button.setAttribute("aria-busy", "true");
              button.textContent = "Working…";
              try {
                if (action === "po-match") {
                  await postJson(`/api/invoices/${id}/po-match`, { matched: true, notes: null });
                } else if (action === "open-review") {
                  openedFlaggedInvoiceId = id;
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
                  const reason = window.prompt("Reason for rejecting this invoice:");
                  if (!reason) return;
                  await postJson(`/api/invoices/${id}/reject-flagged`, { reason });
                } else if (action === "po-query") {
                  const notes = window.prompt("Describe the PO matching issue:");
                  if (notes === null) return;
                  const queryCategory = window.prompt(
                    "Query category (e.g. price discrepancy, missing PO, quantity mismatch):"
                  );
                  const purchasingContact = window.prompt("Purchasing contact to route this query to (optional):");
                  await postJson(`/api/invoices/${id}/po-match`, {
                    matched: false,
                    notes,
                    query_category: queryCategory || null,
                    purchasing_contact: purchasingContact || null,
                  });
                } else if (action === "po-reject") {
                  const reason = window.prompt("Reason for rejecting this PO invoice:");
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
                  const comments = window.prompt(
                    action === "reject" ? "Reason for rejection:" : "Approval comments (optional):"
                  );
                  if (comments === null && action === "reject") return;
                  await postJson(`/api/invoices/${id}/approve`, {
                    level: Number(button.dataset.level),
                    decision: action === "approve" ? "approved" : "rejected",
                    comments: comments || null,
                  });
                } else if (action === "hold") {
                  const comments = window.prompt("Reason for placing this invoice on hold / query (required):");
                  if (!comments) return;
                  await postJson(`/api/invoices/${id}/approve`, {
                    level: Number(button.dataset.level),
                    decision: "on_hold",
                    comments,
                  });
                } else if (action === "resume") {
                  const notes = window.prompt("Resolution notes for resuming approval (optional):");
                  await postJson(`/api/invoices/${id}/resume-approval`, { resolution_notes: notes || null });
                } else if (action === "retry-payment-route") {
                  await postJson(`/api/invoices/${id}/retry-payment-route`, {});
                } else if (action === "select-payment-route") {
                  await postJson(`/api/invoices/${id}/payment-route`, {
                    route: button.dataset.route,
                  });
                } else if (action === "pay") {
                  const invoice = invoices.find(item => String(item.id) === String(id));
                  if (!invoice) throw new Error("Invoice could not be found.");
                  const payment = await collectDomesticPayment(invoice);
                  if (!payment) return;
                  await postJson(`/api/invoices/${id}/pay`, payment);
                } else if (action === "reconcile") {
                  const reconciliationDate = window.prompt("Date shown on bank statement (YYYY-MM-DD):", new Date().toISOString().slice(0, 10));
                  if (!reconciliationDate) return;
                  const notes = window.prompt("Reconciliation notes (optional):");
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
                }
              }
            }

            document.getElementById("flag-review-button").addEventListener("click", async () => {
              const invoiceId = picker.value;
              if (!invoiceId) return;
              const reason = window.prompt("Reason for flagging this invoice for review:");
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
              if (!window.confirm(
                `Permanently delete "${invoice.original_filename}" from the application ` +
                "and SharePoint? This cannot be undone."
              )) return;
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

            document.getElementById("confirm-invoice-button").addEventListener("click", async () => {
              await submitConfirm(false);
            });

            document.getElementById("override-duplicate-button").addEventListener("click", async () => {
              await submitConfirm(true);
            });

            document.getElementById("cancel-duplicate-button").addEventListener("click", async () => {
              const invoiceId = picker.value;
              if (!invoiceId || !window.confirm("Confirm this invoice is a duplicate and cancel it?")) return;
              try {
                await postJson(`/api/invoices/${invoiceId}/cancel-duplicate`, {});
                showToast("Duplicate invoice cancelled.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("retry-approval-route-button").addEventListener("click", async () => {
              const invoice = invoices.find(item => String(item.id) === picker.value);
              if (!invoice || !invoice.irj_number) return;
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
              }
            });

            document.getElementById("section-nav").addEventListener("click", event => {
              const button = event.target.closest("button[data-tab]");
              if (!button) return;
              storeValue(`${SCROLL_STORAGE_PREFIX}${currentTab}`, String(window.scrollY));
              activateTab(button.dataset.tab);
            });

            function applyRoleVisibility(role) {
              const allowedTabs = ROLE_TABS[role] || Object.keys(SECTION_STATUSES);
              document.querySelectorAll("#section-nav button[data-tab]").forEach(button => {
                const isAllowed = allowedTabs.includes(button.dataset.tab);
                button.style.display = isAllowed ? "" : "none";
              });
              const linkedTab = new URLSearchParams(window.location.search).get("tab");
              const desiredTab = linkedTab && allowedTabs.includes(linkedTab)
                ? linkedTab
                : allowedTabs.includes(currentTab) ? currentTab : allowedTabs[0];
              activateTab(desiredTab);
            }

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

            // ---------------------------------------------------------
            // Authentication + admin config panel
            // ---------------------------------------------------------

            function splitCommaList(value) {
              return (value || "")
                .split(",")
                .map(part => part.trim())
                .filter(Boolean);
            }

            function renderSimpleTable(
              container,
              columns,
              rows,
              onDelete,
              idKey = "id",
              onEdit = null
            ) {
              if (!rows.length) {
                container.innerHTML = '<p class="empty">Nothing here yet.</p>';
                return;
              }
              const hasActions = Boolean(onDelete || onEdit);
              const head = columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("") +
                (hasActions ? "<th>Actions</th>" : "");
              const body = rows
                .map((row, index) => {
                  const cells = columns
                    .map(c => `<td>${escapeHtml(c.value(row) ?? "—")}</td>`)
                    .join("");
                  const actionCell = hasActions
                    ? `<td><div class="row-actions">` +
                      (onEdit
                        ? `<button class="secondary" data-edit-index="${index}">Edit</button>`
                        : "") +
                      (onDelete
                        ? `<button class="danger" data-delete-index="${index}">Delete</button>`
                        : "") +
                      `</div></td>`
                    : "";
                  return `<tr>${cells}${actionCell}</tr>`;
                })
                .join("");
              container.innerHTML =
                `<div class="admin-table-wrap"><table class="admin-table">` +
                `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>` +
                `</table></div>`;
              if (onDelete) {
                container.querySelectorAll("[data-delete-index]").forEach(button => {
                  button.addEventListener("click", () => {
                    const row = rows[Number(button.dataset.deleteIndex)];
                    onDelete(row[idKey]);
                  });
                });
              }
              if (onEdit) {
                container.querySelectorAll("[data-edit-index]").forEach(button => {
                  button.addEventListener(
                    "click",
                    () => onEdit(rows[Number(button.dataset.editIndex)])
                  );
                });
              }
            }

            function companyGroupLabel(company) {
              if (company === "*") return "All invoice companies";
              return company || "Unassigned suppliers";
            }

            function renderCompanyGroupedTables(
              container,
              groups,
              columns,
              onDelete,
              idKey = "id",
              onEdit = null
            ) {
              container.replaceChildren();
              container.classList.add("admin-company-groups");
              const entries = [...groups.entries()].sort(([left], [right]) =>
                companyGroupLabel(left).localeCompare(companyGroupLabel(right))
              );
              if (!entries.length) {
                container.innerHTML = '<p class="empty">Nothing here yet.</p>';
                return;
              }
              for (const [company, rows] of entries) {
                const details = document.createElement("details");
                details.className = "admin-company-group";
                const summary = document.createElement("summary");
                summary.textContent =
                  `${companyGroupLabel(company)} (${rows.length})`;
                const content = document.createElement("div");
                content.className = "admin-company-group-content";
                details.append(summary, content);
                container.appendChild(details);
                renderSimpleTable(
                  content,
                  columns,
                  rows,
                  onDelete,
                  idKey,
                  onEdit
                );
              }
            }

            function rowsGroupedByCompany(rows) {
              const groups = new Map();
              for (const row of rows) {
                const company = row.company || "";
                if (!groups.has(company)) groups.set(company, []);
                groups.get(company).push(row);
              }
              return groups;
            }

            function suppliersGroupedByCompany(suppliers, matrix, supplierTerms) {
              const associations = new Map(
                suppliers.map(supplier => [supplier.name, new Set()])
              );
              for (const supplier of suppliers) {
                if (supplier.default_company) {
                  associations.get(supplier.name).add(supplier.default_company);
                }
              }
              for (const record of [...matrix, ...supplierTerms]) {
                if (associations.has(record.supplier)) {
                  associations.get(record.supplier).add(record.company || "");
                }
              }
              const groups = new Map();
              for (const supplier of suppliers) {
                const companies = associations.get(supplier.name);
                if (!companies.size) companies.add("");
                for (const company of companies) {
                  if (!groups.has(company)) groups.set(company, []);
                  groups.get(company).push(supplier);
                }
              }
              return groups;
            }

            function openAdminEditor(title, fields, onSave) {
              const dialog = document.getElementById("admin-edit-dialog");
              const form = document.getElementById("admin-edit-form");
              const container = document.getElementById("admin-edit-fields");
              document.getElementById("admin-edit-title").textContent = title;
              container.replaceChildren();
              const controls = new Map();
              for (const field of fields) {
                const label = document.createElement("label");
                label.textContent = field.label;
                if (field.fullWidth) label.classList.add("full-width");
                let control;
                if (field.type === "select") {
                  control = document.createElement("select");
                  for (const [value, optionLabel] of field.options || []) {
                    const option = document.createElement("option");
                    option.value = value;
                    option.textContent = optionLabel;
                    control.appendChild(option);
                  }
                } else if (field.type === "textarea") {
                  control = document.createElement("textarea");
                } else {
                  control = document.createElement("input");
                  control.type = field.type || "text";
                }
                control.value = field.value ?? "";
                control.required = Boolean(field.required);
                control.autocomplete = field.type === "password" ? "new-password" : "off";
                label.appendChild(control);
                container.appendChild(label);
                controls.set(field.key, { control, field });
              }
              document.getElementById("admin-edit-cancel").onclick = () => dialog.close();
              form.onsubmit = async event => {
                event.preventDefault();
                const values = {};
                for (const [key, { control, field }] of controls) {
                  if (field.omitWhenBlank && !control.value) continue;
                  values[key] = field.nullWhenBlank && !control.value
                    ? null
                    : control.value;
                }
                try {
                  await onSave(values);
                  dialog.close();
                } catch (error) {
                  showToast(error.message, true);
                }
              };
              dialog.showModal();
            }

            function companyOptions(includeAll = false) {
              return [
                ["", "Select invoice company…"],
                ...(includeAll ? [["*", "All invoice companies"]] : []),
                ...adminCompanies.map(company => [company.name, company.name]),
              ];
            }

            function supplierOptions() {
              return [
                ["", "Select supplier company…"],
                ...adminSuppliers.map(supplier => [supplier.name, supplier.name]),
              ];
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

            function supplierBelongsToCompany(
              supplier,
              company,
              matrix = adminMatrix,
              supplierTerms = adminSupplierTerms
            ) {
              if (!company || company === "*") return true;
              const supplierName = supplier.name.trim().toLocaleLowerCase();
              const companyName = company.trim().toLocaleLowerCase();
              if (
                supplier.default_company?.trim().toLocaleLowerCase() === companyName
              ) return true;
              return [...matrix, ...supplierTerms].some(record =>
                record.supplier.trim().toLocaleLowerCase() === supplierName &&
                (
                  record.company === "*" ||
                  record.company.trim().toLocaleLowerCase() === companyName
                )
              );
            }

            function refreshAdminMatrixSupplierOptions(selectedSupplier = "") {
              const company = document.getElementById("admin-matrix-company").value;
              const suppliers = adminSuppliers.filter(supplier =>
                supplierBelongsToCompany(supplier, company)
              );
              replaceSelectOptions(
                document.getElementById("admin-matrix-supplier"),
                [
                  [
                    "",
                    company
                      ? "Select supplier company…"
                      : "Select an invoice company first…",
                  ],
                  ...suppliers.map(supplier => [supplier.name, supplier.name]),
                ]
              );
              const select = document.getElementById("admin-matrix-supplier");
              select.disabled = !company;
              if (
                selectedSupplier &&
                suppliers.some(supplier => supplier.name === selectedSupplier)
              ) {
                select.value = selectedSupplier;
              }
            }

            function loadAdminReferenceOptions(companies, suppliers) {
              const companyOptions = companies.map(company => [
                company.name,
                company.name,
              ]);
              replaceSelectOptions(
                document.getElementById("admin-supplier-default-company"),
                [["", "No default company"], ...companyOptions]
              );
              replaceSelectOptions(
                document.getElementById("admin-import-company"),
                [["", "Select invoice company…"], ...companyOptions]
              );
              replaceSelectOptions(
                document.getElementById("admin-matrix-company"),
                [
                  ["", "Select invoice company…"],
                  ["*", "All invoice companies"],
                  ...companyOptions,
                ]
              );
              refreshAdminMatrixSupplierOptions();
            }

            async function loadSharePointFolderOptions() {
              const status = document.getElementById("admin-sharepoint-folder-status");
              const rootSelect = document.getElementById("admin-company-root-folder");
              const preview = document.getElementById("admin-company-folder-preview");
              const submit = document.getElementById("admin-company-submit");
              try {
                const response = await fetch("/api/admin/sharepoint/folders");
                const body = await response.json();
                if (!response.ok) {
                  throw new Error(body.detail || "SharePoint folders could not be loaded.");
                }
                sharePointCompanyFolders = new Map(
                  body.company_roots.map(structure => [structure.root, structure])
                );
                rootSelect.replaceChildren();
                const emptyOption = document.createElement("option");
                emptyOption.value = "";
                emptyOption.textContent = "Select SharePoint company folder…";
                rootSelect.appendChild(emptyOption);
                for (const structure of body.company_roots) {
                  const option = document.createElement("option");
                  option.value = structure.root;
                  option.textContent = structure.root.split("/").pop();
                  rootSelect.appendChild(option);
                }
                rootSelect.disabled = false;
                submit.disabled = false;
                status.textContent =
                  `${body.company_roots.length} complete company folder structure(s) found. ` +
                  `Incoming and Rejected are shared across all companies.`;
                preview.textContent =
                  "Select a company folder to preview its workflow destinations.";
              } catch (error) {
                sharePointCompanyFolders = new Map();
                rootSelect.disabled = true;
                submit.disabled = true;
                status.textContent = `SharePoint folders unavailable: ${error.message}`;
              }
            }

            // Filtering the approval matrix only re-renders this table from
            // the cached adminMatrix; it does not refetch the admin panel.
            function renderAdminMatrixTable() {
              const matrixQuery = document.getElementById("admin-matrix-filter")
                .value.trim().toLowerCase();
              const matrixEmailFilter = document.getElementById("admin-matrix-email-filter").value;
              const visibleMatrix = adminMatrix.filter(entry => {
                const searchText = [entry.company, entry.supplier, entry.approver1_name,
                  entry.approver1_email, entry.approver2_name, entry.approver2_email]
                  .filter(Boolean).join(" ").toLowerCase();
                const missingEmail = !entry.approver1_email ||
                  (entry.approver2_name && !entry.approver2_email);
                return (!matrixQuery || searchText.includes(matrixQuery)) &&
                  (matrixEmailFilter !== "missing" || missingEmail);
              });
              renderCompanyGroupedTables(
                document.getElementById("admin-matrix-table"),
                rowsGroupedByCompany(visibleMatrix),
                [
                  { label: "Supplier company", value: m => m.supplier },
                  {
                    label: "Approver 1",
                    value: m => m.approver1_email
                      ? `${m.approver1_name} <${m.approver1_email}>`
                      : `${m.approver1_name} — missing email`,
                  },
                  {
                    label: "Approver 2",
                    value: m => m.approver2_name
                      ? (m.approver2_email
                        ? `${m.approver2_name} <${m.approver2_email}>`
                        : `${m.approver2_name} — missing email`)
                      : "—",
                  },
                ],
                async id => {
                  await fetch(`/api/admin/approval-matrix/${id}`, { method: "DELETE" });
                  await loadAdminPanel();
                },
                "id",
                entry => {
                  openAdminEditor("Edit approval route", [
                    {
                      key: "company",
                      label: "Invoice company",
                      type: "select",
                      options: includeCurrentOption(
                        companyOptions(true),
                        entry.company,
                        entry.company === "*" ? "All invoice companies" : entry.company
                      ),
                      value: entry.company,
                      required: true,
                    },
                    {
                      key: "supplier",
                      label: "Supplier company",
                      type: "select",
                      options: includeCurrentOption(
                        supplierOptions(),
                        entry.supplier
                      ),
                      value: entry.supplier,
                      required: true,
                    },
                    {
                      key: "approver1_name",
                      label: "Approver 1 name",
                      value: entry.approver1_name,
                      required: true,
                    },
                    {
                      key: "approver1_email",
                      label: "Approver 1 email",
                      type: "email",
                      value: entry.approver1_email,
                      required: true,
                    },
                    {
                      key: "approver2_name",
                      label: "Approver 2 name",
                      value: entry.approver2_name,
                      nullWhenBlank: true,
                    },
                    {
                      key: "approver2_email",
                      label: "Approver 2 email",
                      type: "email",
                      value: entry.approver2_email,
                      nullWhenBlank: true,
                    },
                  ], async values => {
                    await putJson(
                      `/api/admin/approval-matrix/${entry.id}`,
                      values
                    );
                    await loadAdminPanel();
                  });
                }
              );
            }

            async function loadAdminPanel() {
              try {
                const [companies, suppliers, matrix, supplierTerms, threshold, irjSettings] = await Promise.all([
                  fetchAdminJson("/api/admin/companies"),
                  fetchAdminJson("/api/admin/suppliers"),
                  fetchAdminJson("/api/admin/approval-matrix"),
                  fetchAdminJson("/api/admin/supplier-terms"),
                  fetchAdminJson("/api/admin/ai-threshold"),
                  fetchAdminJson("/api/irj-configurations"),
                ]);
                adminCompanies = companies;
                adminSuppliers = suppliers;
                adminMatrix = matrix;
                adminSupplierTerms = supplierTerms;
                loadAdminReferenceOptions(companies, suppliers);
                renderSimpleTable(
                  document.getElementById("admin-companies-table"),
                  [
                    { label: "Name", value: c => c.name },
                    { label: "SharePoint company folder", value: c => c.sharepoint_root_folder },
                    {
                      label: "Workflow destinations",
                      value: c => `<div class="path-list">${[
                          ["Nominal", c.folder_structure.nominal_invoices],
                          ["PO Match", c.folder_structure.po_match],
                          ["Approved", c.folder_structure.approved_for_payment],
                          ["Paid", c.folder_structure.paid],
                          ["Reconciled", c.folder_structure.reconciled],
                        ].map(([label, path]) =>
                          `<div><strong>${escapeHtml(label)}</strong>` +
                          `<span class="path-value">${escapeHtml(path)}</span></div>`
                        ).join("")}</div>`
                    },
                    { label: "Aliases", value: c => (c.aliases || []).join(", ") },
                  ],
                  companies,
                  async name => {
                    await fetch(`/api/admin/companies/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                    await loadCompanies();
                    const irjResponse = await fetch("/api/irj-configurations");
                    if (irjResponse.ok) {
                      const settings = await irjResponse.json();
                      irjConfigurations = new Map(
                        settings.map(setting => [setting.company, setting])
                      );
                    }
                  },
                  "name",
                  company => {
                    const rootOptions = includeCurrentOption(
                      [
                        ["", "Select SharePoint company folder…"],
                        ...[...sharePointCompanyFolders.values()].map(structure => [
                          structure.root,
                          structure.root.split("/").pop(),
                        ]),
                      ],
                      company.sharepoint_root_folder
                    );
                    openAdminEditor(`Edit ${company.name}`, [
                      {
                        key: "sharepoint_root_folder",
                        label: "SharePoint company folder",
                        type: "select",
                        options: rootOptions,
                        value: company.sharepoint_root_folder,
                        required: true,
                        fullWidth: true,
                      },
                      {
                        key: "aliases",
                        label: "Aliases (comma separated)",
                        value: (company.aliases || []).join(", "),
                        fullWidth: true,
                      },
                      {
                        key: "vat_number",
                        label: "VAT number",
                        value: company.vat_number,
                        nullWhenBlank: true,
                      },
                      {
                        key: "address",
                        label: "Address",
                        type: "textarea",
                        value: company.address,
                        nullWhenBlank: true,
                        fullWidth: true,
                      },
                    ], async values => {
                      values.aliases = splitCommaList(values.aliases);
                      await putJson(
                        `/api/admin/companies/${encodeURIComponent(company.name)}`,
                        values
                      );
                      await loadAdminPanel();
                      await loadCompanies();
                    });
                  }
                );
                renderCompanyGroupedTables(
                  document.getElementById("admin-suppliers-table"),
                  suppliersGroupedByCompany(suppliers, matrix, supplierTerms),
                  [
                    { label: "Name", value: s => s.name },
                    { label: "Aliases", value: s => (s.aliases || []).join(", ") },
                    { label: "Default company", value: s => s.default_company },
                    { label: "Contact", value: s => s.contact_email },
                    { label: "Invoice no. pattern", value: s => s.invoice_number_pattern },
                  ],
                  async name => {
                    await fetch(`/api/admin/suppliers/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                    await loadSuppliers();
                  },
                  "name",
                  supplier => {
                    openAdminEditor(`Edit ${supplier.name}`, [
                      {
                        key: "aliases",
                        label: "Aliases (comma separated)",
                        value: (supplier.aliases || []).join(", "),
                        fullWidth: true,
                      },
                      {
                        key: "default_company",
                        label: "Default company",
                        type: "select",
                        options: includeCurrentOption(
                          [["", "No default company"], ...companyOptions().slice(1)],
                          supplier.default_company
                        ),
                        value: supplier.default_company,
                        nullWhenBlank: true,
                      },
                      {
                        key: "contact_email",
                        label: "Contact email",
                        type: "email",
                        value: supplier.contact_email,
                        nullWhenBlank: true,
                      },
                      {
                        key: "invoice_number_pattern",
                        label: "Invoice number pattern (# digit, @ letter, * alphanumeric)",
                        value: supplier.invoice_number_pattern,
                        nullWhenBlank: true,
                        fullWidth: true,
                      },
                    ], async values => {
                      values.aliases = splitCommaList(values.aliases);
                      await putJson(
                        `/api/admin/suppliers/${encodeURIComponent(supplier.name)}`,
                        values
                      );
                      await loadAdminPanel();
                      await loadSuppliers();
                    });
                  }
                );
                renderCompanyGroupedTables(
                  document.getElementById("admin-supplier-terms-table"),
                  rowsGroupedByCompany(supplierTerms),
                  [
                    { label: "Supplier company", value: t => t.supplier },
                    { label: "Account no.", value: t => t.supplier_account_number },
                    { label: "Payment method", value: t => t.default_payment_method },
                    { label: "Terms", value: t => t.payment_terms_notice },
                    { label: "Bank account", value: t => t.bank_account },
                  ],
                  null,
                  "id",
                  terms => {
                    openAdminEditor("Edit supplier payment settings", [
                      {
                        key: "company",
                        label: "Invoice company",
                        type: "select",
                        options: includeCurrentOption(
                          companyOptions(true),
                          terms.company,
                          terms.company === "*" ? "All invoice companies" : terms.company
                        ),
                        value: terms.company,
                        required: true,
                      },
                      {
                        key: "supplier",
                        label: "Supplier company",
                        type: "select",
                        options: includeCurrentOption(
                          supplierOptions(),
                          terms.supplier
                        ),
                        value: terms.supplier,
                        required: true,
                      },
                      {
                        key: "supplier_account_number",
                        label: "Supplier account number",
                        value: terms.supplier_account_number,
                        nullWhenBlank: true,
                      },
                      {
                        key: "default_payment_method",
                        label: "Default payment method",
                        value: terms.default_payment_method,
                        nullWhenBlank: true,
                      },
                      {
                        key: "payment_terms_notice",
                        label: "Payment terms",
                        value: terms.payment_terms_notice,
                        nullWhenBlank: true,
                      },
                      {
                        key: "bank_account",
                        label: "Bank account",
                        value: terms.bank_account,
                        nullWhenBlank: true,
                      },
                    ], async values => {
                      await putJson(`/api/admin/supplier-terms/${terms.id}`, values);
                      await loadAdminPanel();
                    });
                  }
                );
                renderAdminMatrixTable();
                document.getElementById("admin-threshold-value").value = threshold.threshold ?? 0.8;
                irjConfigurations = new Map(
                  irjSettings.map(setting => [setting.company, setting])
                );
                document.getElementById("admin-irj-settings").innerHTML =
                  '<table class="section-table"><thead><tr>' +
                  '<th>Company</th><th>Mode</th><th>Latest paper IRJ</th><th></th>' +
                  '</tr></thead><tbody>' +
                  irjSettings.map(setting => `
                    <tr>
                      <td>${escapeHtml(setting.company)}</td>
                      <td>
                        <select data-irj-mode="${escapeHtml(setting.company)}">
                          <option value="automatic" ${setting.mode === "automatic" ? "selected" : ""}>Automatic</option>
                          <option value="manual" ${setting.mode === "manual" ? "selected" : ""}>Manual at Sage</option>
                        </select>
                      </td>
                      <td>
                        <input
                          data-irj-current="${escapeHtml(setting.company)}"
                          inputmode="numeric"
                          pattern="[0-9]{6}"
                          maxlength="6"
                          value="${escapeHtml(setting.current_irj || "")}"
                          placeholder="e.g. 004321"
                          ${setting.mode === "manual" ? "disabled" : ""}
                        >
                      </td>
                      <td><button data-save-irj="${escapeHtml(setting.company)}">Save</button></td>
                    </tr>
                  `).join("") + '</tbody></table>';
                document.querySelectorAll("[data-irj-mode]").forEach(select => {
                  select.addEventListener("change", () => {
                    document.querySelector(
                      `[data-irj-current="${CSS.escape(select.dataset.irjMode)}"]`
                    ).disabled = select.value === "manual";
                  });
                });
                document.querySelectorAll("[data-save-irj]").forEach(button => {
                  button.addEventListener("click", async () => {
                    const company = button.dataset.saveIrj;
                    const mode = document.querySelector(
                      `[data-irj-mode="${CSS.escape(company)}"]`
                    ).value;
                    const currentInput = document.querySelector(
                      `[data-irj-current="${CSS.escape(company)}"]`
                    );
                    const currentIrj = currentInput.value.trim();
                    if (mode === "automatic" && currentIrj && !/^\d{6}$/.test(currentIrj)) {
                      showToast("Latest paper IRJ must contain exactly six digits.", true);
                      return;
                    }
                    try {
                      await putJson(
                        `/api/admin/irj-configurations/${encodeURIComponent(company)}`,
                        { mode, current_irj: currentIrj || null }
                      );
                      showToast(`${company} IRJ settings saved.`);
                      await loadAdminPanel();
                    } catch (error) {
                      showToast(error.message, true);
                    }
                  });
                });
              } catch (error) {
                showToast(`Failed to load admin panel: ${error.message}`, true);
              }
            }

            document.getElementById("admin-import-form").addEventListener("submit", async event => {
              event.preventDefault();
              const fileInput = document.getElementById("admin-import-file");
              const companySelect = document.getElementById("admin-import-company");
              const resultBox = document.getElementById("admin-import-result");
              if (!companySelect.value || !fileInput.files.length) {
                return;
              }
              const formData = new FormData();
              formData.append("file", fileInput.files[0]);
              formData.append("company", companySelect.value);
              resultBox.innerHTML = "<p>Importing…</p>";
              try {
                let response = await fetch("/api/admin/import/supplier-master-data", {
                  method: "POST",
                  body: formData,
                });
                let body = await readResponseBody(response);
                if (response.status === 409 && body.detail?.duplicates) {
                  const duplicates = body.detail.duplicates
                    .map(item =>
                      `${item.existing_name} (spreadsheet row${item.row_numbers.length === 1 ? "" : "s"} ` +
                      `${item.row_numbers.join(", ")})`
                    )
                    .join("\n");
                  const replace = window.confirm(
                    `The following supplier companies already exist:

${duplicates}

` +
                    `Replace their configured values with the spreadsheet rows?`
                  );
                  if (!replace) {
                    resultBox.innerHTML = "<p>Import cancelled; no records were changed.</p>";
                    return;
                  }
                  formData.set("replace_existing", "true");
                  response = await fetch("/api/admin/import/supplier-master-data", {
                    method: "POST",
                    body: formData,
                  });
                  body = await readResponseBody(response);
                }
                if (!response.ok) {
                  const detail = typeof body.detail === "string"
                    ? body.detail
                    : body.detail?.message;
                  throw new Error(detail || "Import failed.");
                }
                const issueRows = body.rows.filter(r => r.status !== "imported");
                const issueList = issueRows.length
                  ? "<ul>" +
                    issueRows
                      .map(r =>
                        `<li>Row ${escapeHtml(r.row_number)} (` +
                        `${escapeHtml(r.company === "*" ? "All invoice companies" : (r.company || "—"))} / ` +
                        `${escapeHtml(r.supplier || "—")}): ${escapeHtml(r.reason)}</li>`
                      )
                      .join("") +
                    "</ul>"
                  : "";
                resultBox.innerHTML =
                  `<p><strong>${escapeHtml(body.imported)}</strong> row(s) imported, ` +
                  `<strong>${escapeHtml(body.warnings)}</strong> warning(s), ` +
                  `<strong>${escapeHtml(body.skipped)}</strong> row(s) skipped.</p>${issueList}`;
                event.target.reset();
                await loadAdminPanel();
                await loadSuppliers();
              } catch (error) {
                resultBox.innerHTML = "";
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-company-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/companies", {
                  name: document.getElementById("admin-company-name").value,
                  sharepoint_root_folder: document.getElementById("admin-company-root-folder").value,
                  aliases: splitCommaList(document.getElementById("admin-company-aliases").value),
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-company-root-folder").addEventListener("change", event => {
              const structure = sharePointCompanyFolders.get(event.target.value);
              const preview = document.getElementById("admin-company-folder-preview");
              if (!structure) {
                preview.textContent =
                  "Select a company folder to preview its workflow destinations.";
                return;
              }
              const rows = [
                ["Nominal invoices", structure.nominal_invoices],
                ["Approver 1", structure.nominal_approver_1],
                ["Approver 2", structure.nominal_approver_2],
                ["Nominal on hold", structure.nominal_on_hold],
                ["PO match", structure.po_match],
                ["PO on hold", structure.po_on_hold],
                ["Approved — BACS", structure.approved_bacs],
                ["Approved — BANKLINE", structure.approved_bankline],
                ["Approved — FOREIGN POA", structure.approved_foreign_poa],
                ["Paid", structure.paid],
                ["Reconciled", structure.reconciled],
              ];
              preview.innerHTML =
                "<table><tbody>" +
                rows.map(([label, path]) =>
                  `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(path)}</td></tr>`
                ).join("") +
                "</tbody></table>";
              const nameInput = document.getElementById("admin-company-name");
              if (!nameInput.value) {
                nameInput.value = structure.root.split("/").pop();
              }
            });

            document.getElementById("admin-supplier-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/suppliers", {
                  name: document.getElementById("admin-supplier-name").value,
                  aliases: splitCommaList(document.getElementById("admin-supplier-aliases").value),
                  default_company: document.getElementById("admin-supplier-default-company").value || null,
                  contact_email: document.getElementById("admin-supplier-contact").value || null,
                  invoice_number_pattern:
                    document.getElementById("admin-supplier-invoice-pattern").value || null,
                });
                event.target.reset();
                await loadAdminPanel();
                await loadSuppliers();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-matrix-company").addEventListener(
              "change",
              () => refreshAdminMatrixSupplierOptions()
            );

            document.getElementById("admin-matrix-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/approval-matrix", {
                  company: document.getElementById("admin-matrix-company").value,
                  supplier: document.getElementById("admin-matrix-supplier").value,
                  approver1_name: document.getElementById("admin-matrix-approver1-name").value,
                  approver1_email: document.getElementById("admin-matrix-approver1-email").value,
                  approver2_name: document.getElementById("admin-matrix-approver2-name").value || null,
                  approver2_email: document.getElementById("admin-matrix-approver2-email").value || null,
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-threshold-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                const threshold = Number(document.getElementById("admin-threshold-value").value);
                const response = await fetch("/api/admin/ai-threshold", {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ threshold }),
                });
                if (!response.ok) throw new Error("Failed to save threshold.");
                showToast("AI confidence threshold saved.");
              } catch (error) {
                showToast(error.message, true);
              }
            });

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

            document.getElementById("admin-bi-metrics").addEventListener(
              "toggle",
              event => {
                if (event.target.open) loadMetrics();
              }
            );

            document.getElementById("admin-operations").addEventListener(
              "toggle",
              event => {
                if (event.target.open) loadOperations();
              }
            );
            document.getElementById("refresh-operations").addEventListener(
              "click",
              loadOperations
            );
            document.getElementById("admin-matrix-filter").addEventListener(
              "input",
              () => {
                if (adminMatrixFilterTimer !== null) {
                  window.clearTimeout(adminMatrixFilterTimer);
                }
                adminMatrixFilterTimer = window.setTimeout(renderAdminMatrixTable, 250);
              }
            );
            document.getElementById("admin-matrix-email-filter").addEventListener(
              "change",
              renderAdminMatrixTable
            );

            document.getElementById("admin-companies-config").addEventListener(
              "toggle",
              event => {
                if (event.target.open && sharePointCompanyFolders.size === 0) {
                  loadSharePointFolderOptions();
                }
              }
            );
            document.getElementById("metrics-granularity").addEventListener(
              "change",
              loadMetrics
            );

            window.addEventListener("beforeunload", () => {
              if (activityEventSource !== null) activityEventSource.close();
            });

            loadAuthConfig().then(enterApp);
