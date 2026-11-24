"""Demo mode: run the whole app on temporary local data, with no cloud services.

    python -m app.demo                 # http://127.0.0.1:8000
    python -m app.demo --port 8001

Everything is local: SQLite stores in a temporary folder, local password login,
and a set of sample invoices across every workflow stage. Nothing talks to
Azure, Microsoft 365, SharePoint or PostgreSQL, and every Azure/Graph/database
setting in the environment is ignored, so real credentials can never be used.
The data is discarded when the process stops unless --data-dir is given.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
from app.statuses import Status
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # keep sample_pdf importable without the web stack
    from fastapi import FastAPI

# Settings that must never reach a demo run, however the environment is set up.
_CLOUD_ENVIRONMENT_PREFIXES = (
    "GRAPH_",
    "OUTLOOK_",
    "SHAREPOINT_",
    "EXCEL_CONVERSION_",
    "AZURE_",
    "SAGE_",
    "OUTLOOK_MCP_",
    "ANTHROPIC_",
    "EXTRACTION_",
    "OPENAI_",
)
_CLOUD_ENVIRONMENT_NAMES = frozenset(
    {"DATABASE_URL", "EMAIL_NOTIFICATION_SENDER", "PURCHASE_LEDGER_NOTIFICATION_EMAIL"}
)


def prepare_demo_environment(data_dir: Path) -> None:
    """Point the process at local data only, before the app is created."""
    for name in list(os.environ):
        if name.startswith(_CLOUD_ENVIRONMENT_PREFIXES) or name in _CLOUD_ENVIRONMENT_NAMES:
            del os.environ[name]
    os.environ.update(
        {
            "INVOICE_PROCESSOR_SKIP_DOTENV": "1",  # never read the real .env
            "AUTH_LOCAL_LOGIN_ENABLED": "true",
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_ENTRA_TENANT_ID": "",
            "AUTH_ENTRA_CLIENT_ID": "",
            "AUTH_ENTRA_CLIENT_SECRET": "",
            "AUTH_ENTRA_REDIRECT_URI": "",
            "EMAIL_NOTIFICATIONS_ENABLED": "false",
            "CONFIG_STORE_BACKEND": "sqlite",
            "CONFIG_DB_PATH": str(data_dir / "config.db"),
            "MANUAL_UPLOAD_DIR": str(data_dir / "manual_uploads"),
            "SHAREPOINT_INVOICE_CACHE_DIR": str(data_dir / "sharepoint_cache"),
        }
    )


def sample_pdf(heading: str, lines: Sequence[str]) -> bytes:
    """A small, valid one-page PDF so the PDF viewer has something to show."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    parts = [f"BT /F1 20 Tf 60 730 Td ({escape(heading)}) Tj ET"]
    y = 690
    for line in lines:
        parts.append(f"BT /F1 12 Tf 60 {y} Td ({escape(line)}) Tj ET")
        y -= 22
    stream = "\n".join(parts).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(output)


def _sample_invoices(approver1_email: str, approver2_email: str) -> list[tuple[str, dict]]:
    """(status, extra fields) for one sample invoice per workflow stage."""
    low_confidence = {
        "company": 0.97,
        "supplier": 0.58,
        "supplier_invoice_number": 0.93,
        "invoice_value": 0.66,
        "invoice_date": 0.9,
        "currency": 0.99,
    }
    acme, north = "Acme Trading Ltd", "Northfield Manufacturing"
    return [
        (Status.AWAITING_EXTRACTION, dict(
            company=acme, supplier="Bright Paper", supplier_invoice_number="BP-10442",
            invoice_value=1240.5, currency="GBP", invoice_date="2026-11-02",
            ai_confidence=0.71, ai_field_confidences=json.dumps(low_confidence),
            ai_review_warnings=(
                "Supplier name confidence 58% is below the 80% threshold.\n"
                "Invoice value confidence 66% is below the 80% threshold."
            ),
        )),
        (Status.AWAITING_EXTRACTION, dict(
            company=north, supplier="Supplier Ltd", supplier_invoice_number="SL-2210",
            invoice_value=88.0, currency="GBP", invoice_date="2026-11-09",
        )),
        (Status.NEEDS_REVIEW, dict(
            company=acme, supplier="Kestrel Logistics", supplier_invoice_number="KL-7781",
            invoice_value=432.0, currency="GBP", invoice_date="2026-11-05",
            review_reason="Possible duplicate of an existing invoice.",
            document_type="invoice", ai_confidence=0.55,
        )),
        (Status.NEEDS_REVIEW, dict(
            document_type="statement",
            review_reason="Looks like a supplier statement.",
            document_classification_confidence=0.62,
        )),
        (Status.AWAITING_PO_MATCHING, dict(
            company=acme, supplier="Bright Paper Co", po_number="PO-55120",
            irj_number="004512", invoice_value=3410.0, currency="GBP",
            invoice_date="2026-11-03", supplier_invoice_number="BP-10431",
        )),
        (Status.PO_QUERY, dict(
            company=acme, supplier="Kestrel Logistics", po_number="PO-55211",
            po_query_notes="Quantity on invoice exceeds PO.",
            po_query_category="quantity mismatch", invoice_value=950.0, currency="GBP",
        )),
        (Status.AWAITING_SAGE_REGISTRATION, dict(
            company=north, supplier="Supplier Ltd", irj_number="004513",
            invoice_value=512.2, currency="GBP", supplier_invoice_number="SL-2198",
        )),
        (Status.AWAITING_APPROVAL_1, dict(
            company=acme, supplier="Bright Paper Co", irj_number="004514",
            approver1_name="Jordan Blake", approver1_email=approver1_email,
            invoice_value=760.0, currency="GBP", supplier_invoice_number="BP-10440",
        )),
        (Status.AWAITING_APPROVAL_2, dict(
            company=acme, supplier="Bright Paper Co", irj_number="004515",
            approver1_name="Jordan Blake", approver1_email=approver1_email,
            approver1_decision="approved",
            approver2_name="Sam Ellis", approver2_email=approver2_email,
            invoice_value=5200.0, currency="GBP", supplier_invoice_number="BP-10441",
        )),
        ("Approval Query / On Hold", dict(
            company=acme, supplier="Kestrel Logistics", irj_number="004516",
            hold_reason="Awaiting confirmation of delivery from site.", hold_level=1,
            approver1_name="Jordan Blake", approver1_email=approver1_email,
            invoice_value=1180.0, currency="GBP",
        )),
        (Status.APPROVED_BACS, dict(
            company=north, supplier="Supplier Ltd", irj_number="004517",
            payment_method="BACS", invoice_value=2200.0, currency="GBP",
        )),
        (Status.APPROVED_BANKLINE, dict(
            company=acme, supplier="Bright Paper Co", irj_number="004518",
            payment_method="Bankline", invoice_value=640.0, currency="GBP",
        )),
        (Status.PAID, dict(
            company=acme, supplier="Bright Paper Co", irj_number="004519",
            payment_date="2026-11-10", paid_by="Purchase Ledger",
            invoice_value=315.0, currency="GBP",
        )),
        (Status.RECONCILED, dict(
            company=acme, supplier="Bright Paper Co", irj_number="004500",
            reconciliation_date="2026-11-12", reconciled_by="Purchase Ledger",
            invoice_value=275.0, currency="GBP",
        )),
        (Status.REJECTED, dict(
            company=acme, supplier="Kestrel Logistics", irj_number="004501",
            rejection_reason="Not our order.", invoice_value=99.0, currency="GBP",
        )),
    ]


def seed_demo_data(
    data_dir: Path,
    *,
    invoice_store: object,
    companies_store: object,
    suppliers_store: object,
    approval_matrix_store: object,
    approver1_email: str,
    approver2_email: str,
) -> int:
    """Add sample reference data and invoices; returns the number of invoices."""
    for name in ("Acme Trading Ltd", "Northfield Manufacturing"):
        if companies_store.get(name) is None:  # type: ignore[attr-defined]
            companies_store.create(  # type: ignore[attr-defined]
                name=name, sharepoint_root_folder=f"Invoices/{name}"
            )
    for name, company in (
        ("Supplier Ltd", "Northfield Manufacturing"),
        ("Bright Paper Co", "Acme Trading Ltd"),
        ("Kestrel Logistics", "Acme Trading Ltd"),
    ):
        if suppliers_store.get(name) is None:  # type: ignore[attr-defined]
            suppliers_store.create(  # type: ignore[attr-defined]
                name=name, default_company=company, contact_email="ap@example.test"
            )
    approval_matrix_store.create(  # type: ignore[attr-defined]
        company="Acme Trading Ltd",
        supplier="Bright Paper Co",
        approver1_name="Jordan Blake",
        approver1_email=approver1_email,
        approver2_name="Sam Ellis",
        approver2_email=approver2_email,
    )

    pdf_dir = data_dir / "sample_pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    samples = _sample_invoices(approver1_email, approver2_email)
    for index, (status, fields) in enumerate(samples):
        filename = f"invoice-{1000 + index}.pdf"
        lines = [f"{label}: {fields[key]}" for label, key in (
            ("Supplier", "supplier"),
            ("Invoice no.", "supplier_invoice_number"),
            ("Date", "invoice_date"),
            ("PO", "po_number"),
            ("Total", "invoice_value"),
        ) if fields.get(key)]
        pdf_path = pdf_dir / filename
        pdf_path.write_bytes(sample_pdf("SAMPLE INVOICE (demo data)", lines or ["Sample document"]))
        record = invoice_store.add_from_outlook(  # type: ignore[attr-defined]
            message={
                "id": f"demo-message-{index}",
                "subject": f"Invoice {index + 1} from supplier",
                "from": {"emailAddress": {"name": "Accounts", "address": "accounts@supplier.example"}},
                "receivedDateTime": "2026-11-14T09:30:00Z",
            },
            attachment={"id": f"demo-attachment-{index}", "name": filename},
            stored_path=pdf_path,
        )
        invoice_store.update_fields(record.id, status=status, **fields)  # type: ignore[attr-defined]
    return len(samples)


def build_demo_app(data_dir: Path) -> FastAPI:
    """Create the app on local SQLite stores in ``data_dir`` and seed it."""
    from app.activity_feed import ActivityFeedStore
    from app.approval_matrix import ApprovalMatrixStore
    from app.auth import SEED_USERS, AuthStore
    from app.companies import CompanyStore
    from app.invoices import InvoiceStore
    from app.main import create_app
    from app.outlook_notifications import OutlookNotificationStore
    from app.suppliers import SupplierStore

    data_dir.mkdir(parents=True, exist_ok=True)
    prepare_demo_environment(data_dir)
    emails = {user[0]: user[2] for user in SEED_USERS}
    invoices = InvoiceStore(data_dir / "invoices.db")
    companies = CompanyStore(data_dir / "config.db")
    suppliers = SupplierStore(data_dir / "config.db")
    matrix = ApprovalMatrixStore(data_dir / "config.db")
    if not invoices.list_all():  # a reused --data-dir is not seeded twice
        seed_demo_data(
            data_dir,
            invoice_store=invoices,
            companies_store=companies,
            suppliers_store=suppliers,
            approval_matrix_store=matrix,
            approver1_email=emails["jordan.blake"],
            approver2_email=emails["sam.ellis"],
        )
    return create_app(
        invoice_store=invoices,
        auth_store=AuthStore(data_dir / "auth.db"),
        companies_store=companies,
        suppliers_store=suppliers,
        approval_matrix_store=matrix,
        activity_feed=ActivityFeedStore(data_dir / "activity_feed.db"),
        notification_store=OutlookNotificationStore(data_dir / "outlook_notifications.db"),
        auto_configure_sharepoint=False,
    )


def port_in_use(port: int) -> bool:
    """True if anything already accepts connections on this port.

    Checks by connecting, not binding: another server (for example Docker) can
    hold the same port on a different address, and the operating system would
    still let a second server bind quietly. Then ``localhost`` in the browser
    reaches the wrong one.
    """
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _print_banner(url: str, data_dir: Path, keep: bool) -> None:
    from app.auth import SEED_USERS

    print("\n  Invoice Processor - DEMO MODE (sample data, no cloud services)")
    print(f"  Open {url}\n")
    print("  Sign in with a demo account:")
    for username, display_name, _email, role, password in SEED_USERS:
        print(f"    {username:<17} {password:<18} {role}")
    where = f"kept in {data_dir}" if keep else "discarded when this stops"
    print(f"\n  Data is {where}. Press Ctrl+C to stop.\n", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Invoice Processor in demo mode on local sample data."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--public-url",
        help="Address to show in the start-up message (for example when Docker maps a different port).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Keep demo data in this folder (default: a temporary folder).",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")

    if port_in_use(args.port):
        print(
            f"\n  Port {args.port} is already in use (for example by the Docker stack),\n"
            f"  so http://localhost:{args.port} would open that program, not the demo.\n"
            f"  Stop it, or pick another port:  python -m app.demo --port {args.port + 10}\n"
        )
        return 2

    import uvicorn

    keep = args.data_dir is not None
    temporary = None if keep else tempfile.TemporaryDirectory(prefix="invoice-demo-")
    data_dir = args.data_dir if keep else Path(temporary.name)  # type: ignore[union-attr]
    try:
        app = build_demo_app(data_dir)
        shown_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
        _print_banner(args.public_url or f"http://{shown_host}:{args.port}", data_dir, keep)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if temporary is not None:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
