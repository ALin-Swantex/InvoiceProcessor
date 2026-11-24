-- IRJ sequences are per company, but the key was case-sensitive text. A
-- sequence seeded as 'ACME TRADING LTD' (migration 008) and later used as
-- 'Acme Trading Ltd' started a second sequence at 000001, whose numbers then
-- clashed with invoices_company_irj_number_key. Keep the highest next number
-- for each company, ignoring case, and make the key case-insensitive.
DELETE FROM company_irj_sequences AS duplicate
USING company_irj_sequences AS keeper
WHERE lower(duplicate.company) = lower(keeper.company)
  AND (
      duplicate.next_number < keeper.next_number
      OR (
          duplicate.next_number = keeper.next_number
          AND duplicate.company > keeper.company
      )
  );

CREATE UNIQUE INDEX company_irj_sequences_company_lower_key
    ON company_irj_sequences (lower(company));
