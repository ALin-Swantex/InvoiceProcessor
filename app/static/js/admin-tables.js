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
