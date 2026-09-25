ALTER TABLE invoices
    ADD COLUMN approval_requested_at timestamptz,
    ADD COLUMN approval_reminder_sent_at timestamptz,
    ADD COLUMN approval_hold_started_at timestamptz,
    ADD COLUMN approval_hold_reminder_sent_at timestamptz;

CREATE INDEX idx_invoices_approval_reminders
ON invoices (status, approval_requested_at, approval_reminder_sent_at)
WHERE status IN ('Awaiting Approval 1', 'Awaiting Approval 2');

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
    i.correction_reason, i.corrected_fields_json,
    i.approval_requested_at, i.approval_reminder_sent_at,
    i.approval_hold_started_at, i.approval_hold_reminder_sent_at
FROM invoices AS i;
