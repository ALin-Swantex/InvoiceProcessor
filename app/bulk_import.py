from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from openpyxl import load_workbook

from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore


# ---------------------------------------------------------------------------
# Bulk import of company/supplier master data from a spreadsheet.
#
# This exists so an admin does not have to manually re-key every row of an
# existing Excel sheet (companies, supplier account numbers, default payment
# method, payment terms, bank account, and approver(s)) into the app one at
# a time. Accepted column headers are deliberately flexible (case/whitespace
# -insensitive, a few common synonyms) since real-world spreadsheets rarely
# match an exact schema.
#
# Row-level failures (e.g. an approver name that doesn't match any existing
# user) do not abort the whole import -- each row succeeds or is reported as
# a skipped row with a reason, and the caller/admin can fix just that row.
# ---------------------------------------------------------------------------

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company", "company name"),
    "supplier": ("supplier", "supplier name"),
    "supplier_account_number": (
        "supplier account number",
        "account number",
        "supplier account",
    ),
    "default_payment_method": (
        "default payment method",
        "payment method",
    ),
    "payment_terms_notice": (
        "payment terms",
        "payment terms notice",
        "terms",
    ),
    "bank_account": (
        "bank account",
        "bank account to pay from",
        "pay from bank account",
    ),
    "approver1": (
        "approver",
        "approver 1",
        "approver1",
        "approvers",
    ),
    "approver2": (
        "approver 2",
        "approver2",
        "second approver",
    ),
}


@dataclass
class ImportRowResult:
    row_number: int
    company: str
    supplier: str
    status: str  # "imported" or "skipped"
    reason: str | None = None


@dataclass
class ImportSummary:
    rows: list[ImportRowResult] = field(default_factory=list)

    @property
    def imported_count(self) -> int:
        return sum(1 for row in self.rows if row.status == "imported")

    @property
    def skipped_count(self) -> int:
        return sum(1 for row in self.rows if row.status == "skipped")


def _normalise_header(value: object) -> str:
    return str(value or "").strip().lower()


def _map_headers(header_row: tuple[object, ...]) -> dict[str, int]:
    """Map our canonical field names to the column index found in the sheet."""
    normalised = [_normalise_header(cell) for cell in header_row]
    mapping: dict[str, int] = {}
    for field_name, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                mapping[field_name] = normalised.index(alias)
                break
    return mapping


def _cell(row: tuple[object, ...], mapping: dict[str, int], field_name: str) -> str | None:
    index = mapping.get(field_name)
    if index is None or index >= len(row):
        return None
    value = row[index]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_names(raw: str) -> list[str]:
    """A single "Approvers" cell may list multiple names separated by a
    comma, semicolon, or the word "and"."""
    text = raw.replace(" and ", ",").replace(";", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def import_supplier_workbook(
    source: Path | BinaryIO,
    *,
    company_store: CompanyStore,
    supplier_store: SupplierStore,
    approval_matrix_store: ApprovalMatrixStore,
    supplier_terms_store: SupplierTermsStore,
    auth_store: AuthStore,
) -> ImportSummary:
    workbook = load_workbook(source, data_only=True, read_only=True)
    sheet = workbook.active

    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return ImportSummary()
    mapping = _map_headers(header_row)

    missing_required = [f for f in ("company", "supplier") if f not in mapping]
    if missing_required:
        raise ValueError(
            "The spreadsheet must have 'Company' and 'Supplier' columns; "
            f"missing: {', '.join(missing_required)}."
        )

    users_by_name = {
        user.display_name.strip().casefold(): user for user in auth_store.list_users()
    }

    summary = ImportSummary()
    for row_number, row in enumerate(rows_iter, start=2):
        if row is None or all(cell is None for cell in row):
            continue

        company = _cell(row, mapping, "company")
        supplier = _cell(row, mapping, "supplier")
        if not company or not supplier:
            summary.rows.append(
                ImportRowResult(
                    row_number=row_number,
                    company=company or "",
                    supplier=supplier or "",
                    status="skipped",
                    reason="Missing company or supplier name.",
                )
            )
            continue

        approver1_raw = _cell(row, mapping, "approver1")
        approver2_raw = _cell(row, mapping, "approver2")
        approver_names = _split_names(approver1_raw) if approver1_raw else []
        if approver2_raw:
            approver_names.append(approver2_raw)

        resolved: list[tuple[str, str]] = []
        unresolved: list[str] = []
        for name in approver_names[:2]:
            user = users_by_name.get(name.strip().casefold())
            if user is None:
                unresolved.append(name)
            else:
                resolved.append((user.display_name, user.email))

        if unresolved:
            summary.rows.append(
                ImportRowResult(
                    row_number=row_number,
                    company=company,
                    supplier=supplier,
                    status="skipped",
                    reason=(
                        "Approver name(s) not found among existing users "
                        f"(create the user first): {', '.join(unresolved)}."
                    ),
                )
            )
            continue

        # Ensure the company and supplier master records exist so the
        # approval matrix / supplier terms rows can reference them; leave
        # folder paths for the admin to adjust afterwards if this created a
        # brand-new company.
        if company_store.get(company) is None:
            company_store.create(
                name=company,
                company_folder=f"Invoices/{company}",
                po_matching_folder=f"Invoices/{company}/PO Matching",
            )
        if supplier_store.get(supplier) is None:
            supplier_store.create(name=supplier)

        if resolved:
            existing_entry = approval_matrix_store.find(company, supplier)
            approver1_name, approver1_email = resolved[0]
            approver2_name, approver2_email = resolved[1] if len(resolved) > 1 else (None, None)
            if existing_entry is None:
                approval_matrix_store.create(
                    company=company,
                    supplier=supplier,
                    approver1_name=approver1_name,
                    approver1_email=approver1_email,
                    approver2_name=approver2_name,
                    approver2_email=approver2_email,
                )
            else:
                approval_matrix_store.update(
                    existing_entry.id,
                    approver1_name=approver1_name,
                    approver1_email=approver1_email,
                    approver2_name=approver2_name,
                    approver2_email=approver2_email,
                )

        supplier_terms_store.upsert(
            company=company,
            supplier=supplier,
            supplier_account_number=_cell(row, mapping, "supplier_account_number"),
            default_payment_method=_cell(row, mapping, "default_payment_method"),
            payment_terms_notice=_cell(row, mapping, "payment_terms_notice"),
            bank_account=_cell(row, mapping, "bank_account"),
        )

        summary.rows.append(
            ImportRowResult(
                row_number=row_number,
                company=company,
                supplier=supplier,
                status="imported",
            )
        )

    return summary
