-- Lookups used by duplicate detection, search and intake. They let those
-- queries stay fast once there are tens of thousands of invoices.
CREATE INDEX IF NOT EXISTS idx_invoices_duplicate_number
    ON invoices (
        lower(btrim(coalesce(company, ''))),
        lower(btrim(coalesce(supplier, ''))),
        lower(btrim(coalesce(supplier_invoice_number, '')))
    );

CREATE INDEX IF NOT EXISTS idx_invoices_duplicate_filename
    ON invoices (
        lower(btrim(coalesce(company, ''))),
        lower(btrim(coalesce(original_filename, '')))
    );

CREATE INDEX IF NOT EXISTS idx_invoices_internet_message
    ON invoices (internet_message_id, attachment_id)
    WHERE internet_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_invoices_irj_number
    ON invoices (irj_number)
    WHERE irj_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_invoices_size_bytes
    ON invoices (size_bytes);

CREATE INDEX IF NOT EXISTS idx_invoices_sharepoint_item
    ON invoices (sharepoint_item_id)
    WHERE sharepoint_item_id IS NOT NULL;
