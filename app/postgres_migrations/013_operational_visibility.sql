ALTER TABLE invoices
    ADD COLUMN extraction_model text,
    ADD COLUMN extraction_prompt_version text,
    ADD COLUMN extracted_fields_json text,
    ADD COLUMN routing_explanation text,
    ADD COLUMN reviewed_at text,
    ADD COLUMN reviewed_by text,
    ADD COLUMN correction_reason text,
    ADD COLUMN corrected_fields_json text;

CREATE TABLE worker_heartbeats (
    worker_name text PRIMARY KEY,
    status text NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    last_error text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE VIEW bi_invoice_metadata AS
SELECT
    i.id, i.company, i.irj_number, i.status, i.document_type, i.invoice_type,
    i.supplier, i.supplier_invoice_number, i.po_number, i.invoice_date,
    i.invoice_value, i.currency, i.created_at, i.received_at, i.message_id,
    i.attachment_id, i.internet_message_id, i.sender_name, i.sender_address,
    i.subject, i.original_filename, i.size_bytes, i.sharepoint_item_id,
    i.sharepoint_web_url, i.ai_confidence, i.ai_field_confidences,
    i.ai_review_warnings, i.document_classification_confidence,
    i.document_classification_reason, i.review_reason, i.review_return_status,
    i.duplicate_of_invoice_id, i.po_query_notes, i.po_query_category,
    i.po_query_contact, i.sage_registered_at, i.sage_reference,
    i.sage_registered_by, i.approver1_name, i.approver1_email,
    i.approver1_decision, i.approver1_date, i.approver1_comments,
    i.approver2_name, i.approver2_email, i.approver2_decision,
    i.approver2_date, i.approver2_comments, i.hold_reason, i.hold_level,
    i.payment_route_decided_at, i.payment_route_decided_by, i.payment_method,
    i.supplier_account_number, i.is_foreign_payment, i.payment_date,
    i.payment_reference, i.paid_by, i.foreign_allocation_date,
    i.foreign_allocation_reference, i.foreign_allocated_by,
    i.reconciliation_date, i.reconciliation_notes, i.reconciled_by,
    i.reconciled_at, i.rejection_reason, i.cancelled_at, i.cancelled_by,
    i.cancellation_reason,
    i.extraction_model, i.extraction_prompt_version, i.extracted_fields_json,
    i.routing_explanation, i.reviewed_at, i.reviewed_by,
    i.correction_reason, i.corrected_fields_json
FROM invoices AS i;

CREATE VIEW bi_invoice_corrections AS
SELECT
    id AS invoice_id,
    company,
    supplier,
    extraction_model,
    extraction_prompt_version,
    ai_confidence,
    extracted_fields_json,
    corrected_fields_json,
    correction_reason,
    reviewed_by,
    reviewed_at,
    routing_explanation
FROM invoices
WHERE corrected_fields_json IS NOT NULL;
