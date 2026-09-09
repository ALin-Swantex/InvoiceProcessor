ALTER TABLE invoices
    ADD COLUMN document_type text NOT NULL DEFAULT 'invoice',
    ADD COLUMN document_classification_confidence double precision,
    ADD COLUMN document_classification_reason text;

ALTER TABLE invoices
    ADD CONSTRAINT invoices_document_type_check
    CHECK (document_type IN ('invoice', 'statement'));
