CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_nocase
    ON companies (lower(name));

CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_name_nocase
    ON suppliers (lower(name));

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_matrix_company_supplier_nocase
    ON approval_matrix (lower(company), lower(supplier));
