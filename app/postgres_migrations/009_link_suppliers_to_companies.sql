UPDATE suppliers AS supplier
SET default_company_id = company.id
FROM companies AS company
WHERE supplier.default_company_id IS NULL
  AND supplier.default_company IS NOT NULL
  AND lower(company.name) = lower(supplier.default_company);

CREATE INDEX IF NOT EXISTS idx_suppliers_default_company_id
    ON suppliers (default_company_id);
