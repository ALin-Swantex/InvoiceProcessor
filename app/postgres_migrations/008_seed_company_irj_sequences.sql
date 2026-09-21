WITH company_maxima AS (
    SELECT
        lower(btrim(company)) AS company_key,
        max(irj_number::bigint) + 1 AS next_number
    FROM invoices
    WHERE company IS NOT NULL
      AND btrim(company) <> ''
      AND irj_number ~ '^[0-9]{6}$'
    GROUP BY lower(btrim(company))
)
UPDATE company_irj_sequences AS sequence
SET next_number = greatest(sequence.next_number, maxima.next_number)
FROM company_maxima AS maxima
WHERE lower(btrim(sequence.company)) = maxima.company_key;

WITH company_maxima AS (
    SELECT
        min(btrim(company)) AS company,
        lower(btrim(company)) AS company_key,
        max(irj_number::bigint) + 1 AS next_number
    FROM invoices
    WHERE company IS NOT NULL
      AND btrim(company) <> ''
      AND irj_number ~ '^[0-9]{6}$'
    GROUP BY lower(btrim(company))
)
INSERT INTO company_irj_sequences (company, next_number)
SELECT maxima.company, maxima.next_number
FROM company_maxima AS maxima
WHERE NOT EXISTS (
    SELECT 1
    FROM company_irj_sequences AS sequence
    WHERE lower(btrim(sequence.company)) = maxima.company_key
);
