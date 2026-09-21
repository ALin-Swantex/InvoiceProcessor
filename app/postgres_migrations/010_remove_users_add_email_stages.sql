DROP TABLE IF EXISTS company_user_access;
DROP TABLE IF EXISTS user_sessions;

ALTER TABLE process_configuration
    DROP COLUMN IF EXISTS updated_by;

ALTER TABLE activity_events
    DROP COLUMN IF EXISTS actor_user_id;

DROP TABLE IF EXISTS users;

CREATE TABLE invoice_email_stages (
    invoice_id bigint NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    stage text NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (invoice_id, stage)
);
