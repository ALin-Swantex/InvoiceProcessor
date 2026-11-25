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
        await loadIrjConfigurations();
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
