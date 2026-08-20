from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# PLACEHOLDER DATA
# ---------------------------------------------------------------------------
# This module stands in for the real list of companies invoiced by the
# organisation and their SharePoint filing locations. In production this
# should come from a SharePoint List (e.g. "Companies") maintained by an
# administrator, not from a hard-coded Python dictionary.
#
# Replace `COMPANIES` below with a call to a SharePoint List through
# Microsoft Graph, for example:
#   GET /sites/{site-id}/lists/Companies/items?expand=fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    company_folder: str
    po_matching_folder: str


COMPANIES: dict[str, CompanyProfile] = {
    "Acme Trading Ltd": CompanyProfile(
        name="Acme Trading Ltd",
        company_folder="Invoices/Acme Trading Ltd",
        po_matching_folder="Invoices/Acme Trading Ltd/PO Matching",
    ),
    "Northfield Manufacturing": CompanyProfile(
        name="Northfield Manufacturing",
        company_folder="Invoices/Northfield Manufacturing",
        po_matching_folder="Invoices/Northfield Manufacturing/PO Matching",
    ),
    "Riverside Logistics": CompanyProfile(
        name="Riverside Logistics",
        company_folder="Invoices/Riverside Logistics",
        po_matching_folder="Invoices/Riverside Logistics/PO Matching",
    ),
}


def list_companies() -> list[CompanyProfile]:
    return list(COMPANIES.values())


def get_company(name: str) -> CompanyProfile | None:
    return COMPANIES.get(name)
