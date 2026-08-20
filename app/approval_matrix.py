from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# PLACEHOLDER DATA
# ---------------------------------------------------------------------------
# This module stands in for each company's real approval matrix. In
# production this must be a controlled, easily maintained store (the
# specification requires it to be editable by an authorised member of staff
# without changing workflow code), for example a SharePoint List such as
# "Approval Matrix" with columns: Company, Supplier, Approver1Name,
# Approver1Email, Approver2Name, Approver2Email.
#
# Replace `APPROVAL_MATRIX` and the functions below with Microsoft Graph
# calls against that SharePoint List, e.g.:
#   GET /sites/{site-id}/lists/ApprovalMatrix/items?expand=fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Approver:
    name: str
    email: str


@dataclass(frozen=True)
class ApprovalMatrixEntry:
    company: str
    supplier: str
    approver1: Approver
    approver2: Approver | None = None


APPROVAL_MATRIX: list[ApprovalMatrixEntry] = [
    ApprovalMatrixEntry(
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        approver1=Approver(name="Jordan Blake", email="jordan.blake@example.test"),
        approver2=Approver(name="Sam Ellis", email="sam.ellis@example.test"),
    ),
    ApprovalMatrixEntry(
        company="Acme Trading Ltd",
        supplier="Northgate Supplies",
        approver1=Approver(name="Jordan Blake", email="jordan.blake@example.test"),
    ),
    ApprovalMatrixEntry(
        company="Northfield Manufacturing",
        supplier="Supplier Ltd",
        approver1=Approver(name="Priya Nair", email="priya.nair@example.test"),
        approver2=Approver(name="Chris Adeyemi", email="chris.adeyemi@example.test"),
    ),
    ApprovalMatrixEntry(
        company="Riverside Logistics",
        supplier="Northgate Supplies",
        approver1=Approver(name="Morgan Reyes", email="morgan.reyes@example.test"),
    ),
]


def find_approvers(company: str, supplier: str) -> ApprovalMatrixEntry | None:
    company_key = company.strip().casefold()
    supplier_key = supplier.strip().casefold()
    for entry in APPROVAL_MATRIX:
        if (
            entry.company.strip().casefold() == company_key
            and entry.supplier.strip().casefold() == supplier_key
        ):
            return entry
    return None


def list_matrix() -> list[ApprovalMatrixEntry]:
    return list(APPROVAL_MATRIX)
