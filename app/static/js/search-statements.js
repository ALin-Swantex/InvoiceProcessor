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
