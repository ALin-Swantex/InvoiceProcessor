-- Distinguish an email stage that was actually sent from one that was only
-- claimed. A claim whose process died before sending can then be taken over
-- after a timeout, so the email is not lost forever.
ALTER TABLE invoice_email_stages
    ADD COLUMN IF NOT EXISTS sent_at timestamptz;

-- Every existing row is either a completed send or was already released, so
-- treat all current claims as sent and never re-send them.
UPDATE invoice_email_stages
SET sent_at = claimed_at
WHERE sent_at IS NULL;
