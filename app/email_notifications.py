from __future__ import annotations


# ---------------------------------------------------------------------------
# PLACEHOLDER — EMAIL / TEAMS NOTIFICATIONS
# ---------------------------------------------------------------------------
# The specification requires real email notifications to approvers and
# Purchase Ledger (e.g. "Approver 1 notified by email"). Sending mail
# requires the `Mail.Send` Microsoft Graph application permission, which
# PROJECT_HANDOFF.md explicitly says must NOT be added at this stage.
#
# Replace `send_email_notification` with a real Microsoft Graph call once
# that permission is approved, for example:
#
#   POST /users/{mailbox}/sendMail
#   { "message": { "subject": ..., "body": ..., "toRecipients": [...] } }
#
# Until then, this placeholder only records that a notification *would*
# have been sent (useful for demos and tests) and never contacts a real
# mail server.
# ---------------------------------------------------------------------------


def send_email_notification(*, recipient: str, subject: str, body: str) -> None:
    """Placeholder for outbound email notifications. Currently a no-op."""
    del recipient, subject, body
    return None
