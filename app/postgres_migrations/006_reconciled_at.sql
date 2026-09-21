-- Separates "when the invoice was marked as reconciled in the app"
-- (reconciled_at, system-recorded) from "the date the payment appears on
-- the bank statement" (reconciliation_date, entered by Purchase Ledger).
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS reconciled_at text;
