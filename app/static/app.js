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

            document.getElementById("invoice-search-form").addEventListener(
              "submit",
              async event => {
                event.preventDefault();
                const query = document.getElementById("invoice-search-irj").value.trim();
                const company = document.getElementById("invoice-search-company").value;
                const supplier = document.getElementById("invoice-search-supplier").value;
                const result = document.getElementById("invoice-search-result");
                const submitButton = event.target.querySelector('button[type="submit"]');
                result.className = "empty-state";
                result.innerHTML = loadingStateHtml("Searching invoices…");
                submitButton.disabled = true;
                submitButton.setAttribute("aria-busy", "true");
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
                    result.innerHTML = emptyStateHtml(
                      "No matching invoices",
                      "Try a different company or supplier, or check the IRJ number."
                    );
                    return;
                  }
                  result.className = "";
                  result.innerHTML =
                    `<p class="search-summary" role="status">${matches.length} ` +
                    `invoice${matches.length === 1 ? "" : "s"} found</p>` +
                    matches.map(invoice =>
                      `<div class="metric-panel search-result">` +
                      `<div class="actions search-result-header">` +
                      `<div class="search-result-title"><strong>${escapeHtml(invoice.irj_number ? `IRJ ${invoice.irj_number}` : invoice.original_filename)}</strong>` +
                      `<span>${escapeHtml([invoice.supplier, invoice.company].filter(Boolean).join(" · ") || "Supplier not recorded")}</span></div>` +
                      `${statusPillHtml(invoice.status)}${pdfLinkButton(invoice)}</div>` +
                      `${invoiceAnalysisHtml(invoice, false)}</div>`
                    ).join("");
                } catch (error) {
                  result.className = "empty-state";
                  result.innerHTML = emptyStateHtml(error.message, "", "error");
                } finally {
                  submitButton.disabled = false;
                  submitButton.removeAttribute("aria-busy");
                }
              }
            );

            let statementLibrary = {};

            function renderStatementList(company) {
              const container = document.getElementById("statement-list");
              if (!company) {
                container.className = "";
                container.innerHTML = emptyStateHtml(
                  "Choose a company",
                  "Select a company above to list the PDF statements in its SharePoint Statements folder."
                );
                return;
              }
              const statements = statementLibrary[company] || [];
              if (!statements.length) {
                container.className = "";
                container.innerHTML = emptyStateHtml(
                  `No statements for ${company}`,
                  "Statements filed from the Flagged queue, or saved to its SharePoint Statements folder, will appear here."
                );
                return;
              }
              container.className = "";
              container.innerHTML =
                `<div class="admin-table-wrap"><table class="section-table statement-table" aria-label="Statements for ${escapeAttr(company)}"><thead><tr>` +
                '<th scope="col">Statement</th><th scope="col">Modified</th><th scope="col" class="num">Size</th><th scope="col"><span class="visually-hidden">Actions</span></th>' +
                "</tr></thead><tbody>" +
                statements.map(statement => `
                  <tr>
                    <td class="cell-file"><span class="cell-clamp" title="${escapeAttr(statement.name)}">${escapeHtml(statement.name)}</span></td>
                    <td class="cell-date">${escapeHtml(formatUkTimestamp(statement.lastModifiedDateTime || statement.createdDateTime))}</td>
                    <td class="num">${statement.size == null ? "—" : `${Math.ceil(Number(statement.size) / 1024).toLocaleString("en-GB")} KB`}</td>
                    <td class="col-actions"><a class="action-link" href="/api/statements/${encodeURIComponent(statement.id)}/pdf" target="_blank" rel="noopener" aria-label="View PDF — ${escapeAttr(statement.name)} (opens in a new tab)">View PDF</a></td>
                  </tr>
                `).join("") +
                "</tbody></table></div>";
            }

            async function loadStatements() {
              const companySelect = document.getElementById("statement-company");
              const container = document.getElementById("statement-list");
              container.className = "";
              container.innerHTML = loadingStateHtml("Loading statements from SharePoint…");
              let response;
              try {
                response = await fetch("/api/statements");
              } catch (error) {
                response = null;
              }
              if (!response || !response.ok) {
                const body = response ? await response.json().catch(() => ({})) : {};
                container.innerHTML = emptyStateHtml(
                  "Statements could not be loaded",
                  typeof body.detail === "string"
                    ? body.detail
                    : "SharePoint did not respond. Open this page again to retry.",
                  "error"
                );
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
              showUpdatedInvoice(detail);
              return detail;
            }

            // An action's reply is the updated invoice. Show it straight away
            // instead of waiting for the full list to reload.
            function showUpdatedInvoice(record) {
              if (!record || typeof record !== "object") return;
              if (!Number.isInteger(record.id) || typeof record.status !== "string") return;
              if (!("message_id" in record)) return;
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
                    `<option value="${escapeAttr(profile.supplier_account_number || "")}">` +
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
              document.getElementById("payment-date").value = localTodayIsoDate();
              document.getElementById("payment-reference").value = "";
              document.getElementById("payment-supplier-context").textContent = [
                invoiceAccessibleName(invoice) || invoice.supplier || "Supplier",
                invoice.invoice_value == null || invoice.invoice_value === ""
                  ? ""
                  : formatMoney(invoice.invoice_value, invoice.currency),
              ].filter(Boolean).join(" · ");

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

            // ---------------------------------------------------------
            // In-page dialogs replacing window.prompt / window.confirm.
            // formDialog() resolves to an object of field values, or null
            // when cancelled (Cancel button, Escape, or closing the dialog).
            // ---------------------------------------------------------
            const uiDialog = document.getElementById("ui-dialog");
            let uiDialogPending = null;

            function formDialog({
              title,
              message = "",
              fields = [],
              confirmLabel = "OK",
              cancelLabel = "Cancel",
              tone = "default",
              scrollMessage = false,
              context = "",
            }) {
              if (uiDialogPending) uiDialogPending(null);
              const form = document.getElementById("ui-dialog-form");
              const fieldContainer = document.getElementById("ui-dialog-fields");
              const messageEl = document.getElementById("ui-dialog-message");
              const errorEl = document.getElementById("ui-dialog-error");
              const confirmButton = document.getElementById("ui-dialog-confirm");
              const cancelButton = document.getElementById("ui-dialog-cancel");
              const opener = document.activeElement;
              document.getElementById("ui-dialog-title").textContent = title;
              const contextEl = document.getElementById("ui-dialog-context");
              contextEl.textContent = context;
              contextEl.hidden = !context;
              messageEl.textContent = message;
              messageEl.hidden = !message;
              messageEl.classList.toggle("scroll", scrollMessage);
              errorEl.textContent = "";
              confirmButton.textContent = confirmLabel;
              cancelButton.textContent = cancelLabel;
              confirmButton.className = tone === "danger" ? "danger-solid" : "primary";
              form.classList.toggle("dialog-tone-danger", tone === "danger");
              fieldContainer.replaceChildren();
              fieldContainer.hidden = !fields.length;
              const controls = [];
              fields.forEach((field, index) => {
                const label = document.createElement("label");
                const caption = document.createElement("span");
                caption.textContent = field.label;
                if (field.required) {
                  const mark = document.createElement("span");
                  mark.className = "required-mark";
                  mark.setAttribute("aria-hidden", "true");
                  mark.textContent = " *";
                  caption.appendChild(mark);
                }
                label.appendChild(caption);
                const control = document.createElement(
                  field.multiline ? "textarea" : "input"
                );
                if (!field.multiline) control.type = field.type || "text";
                control.id = `ui-dialog-field-${index}`;
                control.value = field.value ?? "";
                control.required = Boolean(field.required);
                control.placeholder = field.placeholder || "";
                control.autocomplete = "off";
                if (field.required) control.setAttribute("aria-required", "true");
                label.appendChild(control);
                fieldContainer.appendChild(label);
                controls.push([field.name, control]);
              });

              return new Promise(resolve => {
                const finish = value => {
                  if (uiDialogPending !== finish) return;
                  uiDialogPending = null;
                  form.onsubmit = null;
                  cancelButton.onclick = null;
                  uiDialog.onclose = null;
                  if (uiDialog.open) uiDialog.close();
                  // Return focus to whatever opened the dialog when it is
                  // still on the page; otherwise leave focus where it lands.
                  if (opener && opener.isConnected && typeof opener.focus === "function") {
                    window.requestAnimationFrame(() => {
                      if (!opener.disabled) opener.focus();
                    });
                  }
                  resolve(value);
                };
                uiDialogPending = finish;
                form.onsubmit = event => {
                  event.preventDefault();
                  for (const [, control] of controls) {
                    if (control.required && !control.value) {
                      errorEl.textContent = "Please complete the required field.";
                      control.focus();
                      return;
                    }
                  }
                  const values = {};
                  for (const [name, control] of controls) values[name] = control.value;
                  finish(values);
                };
                cancelButton.onclick = () => finish(null);
                uiDialog.onclose = () => finish(null);
                uiDialog.showModal();
                const firstControl = controls.length ? controls[0][1] : null;
                if (firstControl) {
                  firstControl.focus();
                } else if (tone === "danger") {
                  cancelButton.focus();
                } else {
                  confirmButton.focus();
                }
              });
            }

            // Resolves true when confirmed, false when cancelled.
            async function confirmDialog(options) {
              return (await formDialog({ ...options, fields: [] })) !== null;
            }

            // Resolves to the entered text, or null when cancelled. A required
            // prompt cannot be submitted empty.
            async function promptDialog({
              title,
              message = "",
              label,
              value = "",
              required = false,
              multiline = true,
              type = "text",
              placeholder = "",
              confirmLabel = "OK",
              tone = "default",
              context = "",
            }) {
              const result = await formDialog({
                title,
                message,
                confirmLabel,
                tone,
                context,
                fields: [{ name: "value", label, value, required, multiline, type, placeholder }],
              });
              return result === null ? null : result.value;
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
                container.innerHTML = `<p class="empty">${escapeHtml(adminEmptyCopy(container))}</p>`;
                return;
              }
              const hasActions = Boolean(onDelete || onEdit);
              const head = columns.map(c => `<th scope="col">${escapeHtml(c.label)}</th>`).join("") +
                (hasActions ? '<th scope="col"><span class="visually-hidden">Actions</span></th>' : "");
              const body = rows
                .map((row, index) => {
                  const cells = columns
                    .map(c => c.html
                      ? `<td>${c.value(row) ?? "—"}</td>`
                      : `<td>${escapeHtml(c.value(row) ?? "—")}</td>`)
                    .join("");
                  const rowName = escapeAttr(String(columns[0].value(row) ?? "").trim() || "entry");
                  const actionCell = hasActions
                    ? `<td><div class="row-actions">` +
                      (onEdit
                        ? `<button type="button" class="secondary" data-edit-index="${index}" aria-label="Edit ${rowName}">Edit</button>`
                        : "") +
                      (onDelete
                        ? `<button type="button" class="danger" data-delete-index="${index}" aria-label="Delete ${rowName}">Delete</button>`
                        : "") +
                      `</div></td>`
                    : "";
                  return `<tr>${cells}${actionCell}</tr>`;
                })
                .join("");
              const tableName = adminTableName(container);
              container.innerHTML =
                `<div class="admin-table-wrap"><table class="admin-table"${tableName ? ` aria-label="${escapeAttr(tableName)}"` : ""}>` +
                `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>` +
                `</table></div>`;
              if (onDelete) {
                container.querySelectorAll("[data-delete-index]").forEach(button => {
                  button.addEventListener("click", async () => {
                    const row = rows[Number(button.dataset.deleteIndex)];
                    const label = String(columns[0].value(row) ?? "").trim();
                    if (!await confirmDialog({
                      title: "Delete this entry?",
                      message:
                        `Delete ${label ? `"${label}"` : "this entry"}? ` +
                        "This cannot be undone.",
                      confirmLabel: "Delete",
                      tone: "danger",
                    })) return;
                    try {
                      await onDelete(row[idKey]);
                    } catch (error) {
                      showToast(error.message, true);
                    }
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

            // Admin tables are named after their section (and company group)
            // so screen-reader table lists are meaningful.
            function adminTableName(container) {
              const block = container.closest(".admin-block");
              const section = block
                ?.querySelector(":scope > h3, :scope > summary")?.textContent.trim() || "";
              const group = container.closest("details.admin-company-group")
                ?.querySelector("summary")?.textContent.trim() || "";
              return [section, group].filter(Boolean).join(" — ");
            }

            const ADMIN_EMPTY_COPY = {
              "admin-companies-table": "No companies yet. Add one above using its SharePoint company folder.",
              "admin-suppliers-table": "No suppliers yet. Add one above or bulk import a supplier workbook.",
              "admin-supplier-terms-table": "No payment settings yet. They are created by the supplier workbook import.",
              "admin-matrix-table": "No approval routes match. Clear the filter or add a route above.",
            };

            function adminEmptyCopy(container) {
              const owner = container.closest("[id^='admin-'][id$='-table']");
              return ADMIN_EMPTY_COPY[owner?.id] || "Nothing here yet.";
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
                container.innerHTML = `<p class="empty">${escapeHtml(adminEmptyCopy(container))}</p>`;
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
              const errorEl = document.getElementById("admin-edit-error");
              errorEl.textContent = "";
              document.getElementById("admin-edit-cancel").onclick = () => dialog.close();
              form.onsubmit = async event => {
                event.preventDefault();
                errorEl.textContent = "";
                const values = {};
                for (const [key, { control, field }] of controls) {
                  if (field.omitWhenBlank && !control.value) continue;
                  values[key] = field.nullWhenBlank && !control.value
                    ? null
                    : control.value;
                }
                await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
                  try {
                    await onSave(values);
                    dialog.close();
                    showToast("Changes saved.");
                  } catch (error) {
                    errorEl.textContent = error.message;
                  }
                });
              };
              dialog.showModal();
              container.querySelector("input, select, textarea")?.focus();
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
                      : "Choose company first…",
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
                  await sendJson(`/api/admin/approval-matrix/${id}`, "DELETE");
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
              loadAutoConfirmSetting();
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
                      // Trusted markup: every label and path is escaped below.
                      html: true,
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
                    await sendJson(`/api/admin/companies/${encodeURIComponent(name)}`, "DELETE");
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
                    await sendJson(`/api/admin/suppliers/${encodeURIComponent(name)}`, "DELETE");
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
                        <select data-irj-mode="${escapeAttr(setting.company)}" aria-label="IRJ mode for ${escapeAttr(setting.company)}">
                          <option value="automatic" ${setting.mode === "automatic" ? "selected" : ""}>Automatic</option>
                          <option value="manual" ${setting.mode === "manual" ? "selected" : ""}>Manual at Sage</option>
                        </select>
                      </td>
                      <td>
                        <input
                          data-irj-current="${escapeAttr(setting.company)}"
                          aria-label="Latest paper IRJ for ${escapeAttr(setting.company)}"
                          inputmode="numeric"
                          pattern="[0-9]{6}"
                          maxlength="6"
                          value="${escapeAttr(setting.current_irj || "")}"
                          placeholder="e.g. 004321"
                          ${setting.mode === "manual" ? "disabled" : ""}
                        >
                      </td>
                      <td><button data-save-irj="${escapeAttr(setting.company)}" aria-label="Save IRJ settings for ${escapeAttr(setting.company)}">Save</button></td>
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
              setFormError(event.target, "");
              if (!companySelect.value || !fileInput.files.length) {
                setFormError(
                  event.target,
                  !companySelect.value
                    ? "Choose the invoice company the suppliers belong to."
                    : "Choose an Excel workbook (.xlsx) to import."
                );
                return;
              }
              const importButton = event.target.querySelector('button[type="submit"]');
              importButton.disabled = true;
              importButton.setAttribute("aria-busy", "true");
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
                  const replace = await confirmDialog({
                    title: "Replace existing suppliers?",
                    message: `The following supplier companies already exist:

${duplicates}

` +
                    `Replace their configured values with the spreadsheet rows?`,
                    confirmLabel: "Replace existing values",
                    scrollMessage: true,
                  });
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
                setFormError(event.target, error.message);
              } finally {
                importButton.disabled = false;
                importButton.removeAttribute("aria-busy");
              }
            });

            document.getElementById("admin-company-form").addEventListener("submit", async event => {
              event.preventDefault();
              const form = event.target;
              setFormError(form, "");
              await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
              try {
                await postJson("/api/admin/companies", {
                  name: document.getElementById("admin-company-name").value,
                  sharepoint_root_folder: document.getElementById("admin-company-root-folder").value,
                  aliases: splitCommaList(document.getElementById("admin-company-aliases").value),
                });
                form.reset();
                showToast("Company added.");
                await loadAdminPanel();
              } catch (error) {
                setFormError(form, error.message);
              }
              });
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
              const form = event.target;
              setFormError(form, "");
              await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
              try {
                await postJson("/api/admin/suppliers", {
                  name: document.getElementById("admin-supplier-name").value,
                  aliases: splitCommaList(document.getElementById("admin-supplier-aliases").value),
                  default_company: document.getElementById("admin-supplier-default-company").value || null,
                  contact_email: document.getElementById("admin-supplier-contact").value || null,
                  invoice_number_pattern:
                    document.getElementById("admin-supplier-invoice-pattern").value || null,
                });
                form.reset();
                showToast("Supplier added.");
                await loadAdminPanel();
                await loadSuppliers();
              } catch (error) {
                setFormError(form, error.message);
              }
              });
            });

            document.getElementById("admin-matrix-company").addEventListener(
              "change",
              () => refreshAdminMatrixSupplierOptions()
            );

            document.getElementById("admin-matrix-form").addEventListener("submit", async event => {
              event.preventDefault();
              const form = event.target;
              setFormError(form, "");
              await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
              try {
                await postJson("/api/admin/approval-matrix", {
                  company: document.getElementById("admin-matrix-company").value,
                  supplier: document.getElementById("admin-matrix-supplier").value,
                  approver1_name: document.getElementById("admin-matrix-approver1-name").value,
                  approver1_email: document.getElementById("admin-matrix-approver1-email").value,
                  approver2_name: document.getElementById("admin-matrix-approver2-name").value || null,
                  approver2_email: document.getElementById("admin-matrix-approver2-email").value || null,
                });
                form.reset();
                showToast("Approval route saved.");
                await loadAdminPanel();
              } catch (error) {
                setFormError(form, error.message);
              }
              });
            });

            document.getElementById("admin-threshold-form").addEventListener("submit", async event => {
              event.preventDefault();
              const form = event.target;
              setFormError(form, "");
              const thresholdInput = document.getElementById("admin-threshold-value");
              if (thresholdInput.value === "" || Number(thresholdInput.value) < 0 || Number(thresholdInput.value) > 1) {
                setFormError(form, "Enter a threshold between 0 and 1, for example 0.80.");
                thresholdInput.focus();
                return;
              }
              await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
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
                setFormError(form, error.message);
              }
              });
            });

            async function loadAutoConfirmSetting() {
              try {
                const response = await fetch("/api/admin/ai-auto-confirm");
                if (!response.ok) return;
                const body = await response.json();
                document.getElementById("admin-auto-confirm").checked = Boolean(body.enabled);
              } catch (error) {
                // The switch keeps its previous state; the next panel load retries.
              }
            }

            document.getElementById("admin-auto-confirm-form").addEventListener("submit", async event => {
              event.preventDefault();
              const form = event.target;
              setFormError(form, "");
              await withBusyButton(form.querySelector('button[type="submit"]'), async () => {
                try {
                  const enabled = document.getElementById("admin-auto-confirm").checked;
                  const response = await fetch("/api/admin/ai-auto-confirm", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled }),
                  });
                  if (!response.ok) throw new Error("Failed to save the setting.");
                  showToast(enabled
                    ? "Confident invoices will now be confirmed automatically."
                    : "Every invoice will now wait for Purchase Ledger confirmation.");
                } catch (error) {
                  setFormError(form, error.message);
                }
              });
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

            // ---------------------------------------------------------
            // Admin in-page section menu: jumps to a section (opening it if
            // collapsed) and highlights the section currently in view.
            // ---------------------------------------------------------
            const adminSectionNav = document.getElementById("admin-section-nav");
            const reducedMotion = window.matchMedia
              ? window.matchMedia("(prefers-reduced-motion: reduce)")
              : null;

            function adminSectionBlock(button) {
              return document.getElementById(button.dataset.adminSection)
                ?.closest(".admin-block") || null;
            }

            adminSectionNav.addEventListener("click", event => {
              const button = event.target.closest("button[data-admin-section]");
              if (!button) return;
              const block = adminSectionBlock(button);
              if (!block) return;
              if (block.tagName === "DETAILS" && !block.open) block.open = true;
              block.scrollIntoView({
                behavior: reducedMotion && reducedMotion.matches ? "auto" : "smooth",
                block: "start",
              });
              const heading = block.querySelector(":scope > summary, :scope > h3");
              if (heading) {
                if (heading.tagName === "H3") heading.tabIndex = -1;
                heading.focus({ preventScroll: true });
              }
            });

            // Highlight the last section whose heading has scrolled up to
            // just under the sticky menu.
            const adminSectionButtons = [...adminSectionNav.querySelectorAll("button[data-admin-section]")]
              .map(button => [button, adminSectionBlock(button)])
              .filter(([, block]) => block);
            let adminSpyFrame = null;
            function syncAdminSectionSpy() {
              adminSpyFrame = null;
              if (!adminSectionNav.offsetParent) return;
              const line = adminSectionNav.getBoundingClientRect().bottom + 96;
              let current = adminSectionButtons[0]?.[0];
              for (const [button, block] of adminSectionButtons) {
                if (block.getBoundingClientRect().top <= line) current = button;
              }
              const atBottom = window.innerHeight + window.scrollY >=
                document.documentElement.scrollHeight - 4;
              if (atBottom) current = adminSectionButtons.at(-1)?.[0];
              for (const [button] of adminSectionButtons) {
                if (button === current) button.setAttribute("aria-current", "true");
                else button.removeAttribute("aria-current");
              }
            }
            window.addEventListener("scroll", () => {
              if (adminSpyFrame === null) {
                adminSpyFrame = window.requestAnimationFrame(syncAdminSectionSpy);
              }
            }, { passive: true });
            document.querySelector('#section-nav button[data-tab="admin"]')
              .addEventListener("click", () => window.requestAnimationFrame(syncAdminSectionSpy));

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
