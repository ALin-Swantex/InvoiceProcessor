CREATE TABLE company_irj_sequences (
    company text PRIMARY KEY,
    next_number bigint NOT NULL CHECK (
        next_number > 0 AND next_number <= 1000000
    )
);

ALTER TABLE invoices
    DROP CONSTRAINT IF EXISTS invoices_irj_number_key;

CREATE UNIQUE INDEX invoices_company_irj_number_key
    ON invoices (lower(company), irj_number)
    WHERE company IS NOT NULL AND irj_number IS NOT NULL;
