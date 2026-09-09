UPDATE invoices
SET irj_number = substring(irj_number FROM 5)
WHERE irj_number ~ '^IRJ-[0-9]{6}$';

ALTER TABLE invoices
    ADD CONSTRAINT invoices_irj_number_format_check
    CHECK (irj_number IS NULL OR irj_number ~ '^[0-9]{6}$');
