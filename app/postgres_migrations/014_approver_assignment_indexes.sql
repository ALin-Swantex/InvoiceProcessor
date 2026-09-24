CREATE INDEX IF NOT EXISTS invoices_approver1_email_idx
    ON invoices (lower(btrim(approver1_email)))
    WHERE approver1_email IS NOT NULL;

CREATE INDEX IF NOT EXISTS invoices_approver2_email_idx
    ON invoices (lower(btrim(approver2_email)))
    WHERE approver2_email IS NOT NULL;
