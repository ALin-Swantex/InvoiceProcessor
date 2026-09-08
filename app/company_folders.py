from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath


INVOICES_ROOT = "Invoices"
INCOMING_INVOICES_FOLDER = "Invoices/Incoming Invoices"
REJECTED_INVOICES_FOLDER = "Invoices/Rejected Invoices"


@dataclass(frozen=True)
class CompanyFolderStructure:
    root: str
    nominal_invoices: str
    nominal_approver_1: str
    nominal_approver_2: str
    nominal_on_hold: str
    po_invoices: str
    po_match: str
    po_on_hold: str
    approved_for_payment: str
    approved_bacs: str
    approved_bankline: str
    approved_foreign_poa: str
    paid: str
    reconciled: str

    @classmethod
    def from_root(cls, root: str) -> CompanyFolderStructure:
        normalized = str(PurePosixPath(root))
        if (
            not normalized.startswith(f"{INVOICES_ROOT}/")
            or len(PurePosixPath(normalized).parts) != 2
        ):
            raise ValueError(
                "A company SharePoint root must be a direct child of 'Invoices'."
            )

        def child(relative_path: str) -> str:
            return str(PurePosixPath(normalized) / relative_path)

        return cls(
            root=normalized,
            nominal_invoices=child("Nominal Invoices"),
            nominal_approver_1=child("Nominal Invoices/Approver 1"),
            nominal_approver_2=child("Nominal Invoices/Approver 2"),
            nominal_on_hold=child("Nominal Invoices/On hold"),
            po_invoices=child("PO Invoices"),
            po_match=child("PO Invoices/PO Match"),
            po_on_hold=child("PO Invoices/On hold"),
            approved_for_payment=child("Approved for payment"),
            approved_bacs=child("Approved for payment/BACS"),
            approved_bankline=child("Approved for payment/BANKLINE"),
            approved_foreign_poa=child("Approved for payment/FOREIGN POA"),
            paid=child("Paid"),
            reconciled=child("Reconciled"),
        )

    def missing_paths(self, existing_paths: set[str] | frozenset[str]) -> list[str]:
        return [
            path
            for path in asdict(self).values()
            if path not in existing_paths
        ]

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def discover_company_folder_structures(
    folder_paths: list[str],
) -> list[CompanyFolderStructure]:
    existing = frozenset(folder_paths)
    reserved = {
        INCOMING_INVOICES_FOLDER,
        REJECTED_INVOICES_FOLDER,
    }
    structures: list[CompanyFolderStructure] = []
    for path in folder_paths:
        if path in reserved:
            continue
        parts = PurePosixPath(path).parts
        if len(parts) != 2 or parts[0] != INVOICES_ROOT:
            continue
        structure = CompanyFolderStructure.from_root(path)
        if not structure.missing_paths(existing):
            structures.append(structure)
    return sorted(structures, key=lambda structure: structure.root.casefold())
