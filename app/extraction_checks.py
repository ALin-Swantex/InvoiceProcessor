"""Deterministic checks on an LLM's extraction.

An LLM does not give a calibrated confidence score, so its self-reported
certainty is not trusted on its own. These checks look at facts that can be
verified without asking a model: is the value actually in the document, do the
amounts add up, is the date believable. A failed check adds a warning (which
routes the invoice to Needs Review) and caps that field's confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping

# A PDF with less text than this is treated as scanned (image only): the model
# reads the page image and the "is it in the text?" check is skipped.
MIN_TEXT_LAYER_CHARS = 40

UNGROUNDED_CAP = 0.30
ARITHMETIC_CAP = 0.30
NON_POSITIVE_CAP = 0.30
BAD_CURRENCY_CAP = 0.30
IMPLAUSIBLE_DATE_CAP = 0.60
UNKNOWN_COMPANY_CAP = 0.60

_FUTURE_TOLERANCE = timedelta(days=30)
_MAX_AGE = timedelta(days=5 * 365)
_COMPANY_SUFFIXES = {"ltd", "limited", "plc", "inc", "llc", "the", "and", "co", "company"}


@dataclass
class CheckOutcome:
    warnings: list[str] = field(default_factory=list)
    caps: dict[str, float] = field(default_factory=dict)

    def flag(self, field_name: str, cap: float, warning: str) -> None:
        self.warnings.append(warning)
        self.caps[field_name] = min(cap, self.caps.get(field_name, 1.0))


def run_checks(
    *,
    values: Mapping[str, object],
    text_layer: str,
    known_companies: Iterable[str] = (),
    today: date | None = None,
) -> CheckOutcome:
    """Check extracted ``values`` against the document text and each other."""
    outcome = CheckOutcome()
    today = today or date.today()

    _check_grounding(values, text_layer, outcome)
    _check_arithmetic(values, outcome)
    _check_amount(values, outcome)
    _check_date(values, today, outcome)
    _check_currency(values, outcome)
    _check_company(values, known_companies, outcome)
    return outcome


def _alphanumeric(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _check_grounding(
    values: Mapping[str, object], text_layer: str, outcome: CheckOutcome
) -> None:
    if len(text_layer.strip()) < MIN_TEXT_LAYER_CHARS:
        return  # scanned or empty text layer: nothing to compare against
    squashed = _alphanumeric(text_layer)
    digits = re.sub(r"\D", "", text_layer)

    for name, label in (
        ("supplier_invoice_number", "supplier invoice number"),
        ("purchase_order_number", "purchase order number"),
    ):
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            needle = _alphanumeric(value)
            if needle and needle not in squashed:
                outcome.flag(
                    name,
                    UNGROUNDED_CAP,
                    f"The {label} '{value}' was not found in the document text.",
                )

    supplier = values.get("supplier")
    if isinstance(supplier, str) and supplier.strip():
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", supplier.casefold())
            if len(token) >= 3 and token not in _COMPANY_SUFFIXES
        ]
        if tokens and tokens[0] not in squashed:
            outcome.flag(
                "supplier",
                UNGROUNDED_CAP,
                f"The supplier name '{supplier}' was not found in the document text.",
            )

    total = values.get("invoice_value")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        needle = f"{abs(float(total)):.2f}".replace(".", "")
        if needle not in digits:
            outcome.flag(
                "invoice_value",
                UNGROUNDED_CAP,
                f"The invoice total {float(total):.2f} was not found in the document text.",
            )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _check_arithmetic(values: Mapping[str, object], outcome: CheckOutcome) -> None:
    net = _number(values.get("net_amount"))
    tax = _number(values.get("tax_amount"))
    gross = _number(values.get("invoice_value"))
    if net is None or tax is None or gross is None:
        return
    tolerance = max(0.02, abs(gross) * 0.005)
    if abs((net + tax) - gross) > tolerance:
        outcome.flag(
            "invoice_value",
            ARITHMETIC_CAP,
            f"Net {net:.2f} plus tax {tax:.2f} does not equal the total {gross:.2f}.",
        )


def _check_amount(values: Mapping[str, object], outcome: CheckOutcome) -> None:
    total = _number(values.get("invoice_value"))
    if total is not None and total <= 0:
        outcome.flag(
            "invoice_value",
            NON_POSITIVE_CAP,
            f"The invoice total {total:.2f} is zero or negative (a credit note?).",
        )


def _check_date(
    values: Mapping[str, object], today: date, outcome: CheckOutcome
) -> None:
    raw = values.get("invoice_date")
    if not isinstance(raw, str) or not raw:
        return
    try:
        invoice_date = date.fromisoformat(raw)
    except ValueError:
        return  # the caller already warns about an unreadable date
    if invoice_date > today + _FUTURE_TOLERANCE:
        outcome.flag(
            "invoice_date",
            IMPLAUSIBLE_DATE_CAP,
            f"The invoice date {raw} is in the future.",
        )
    elif invoice_date < today - _MAX_AGE:
        outcome.flag(
            "invoice_date",
            IMPLAUSIBLE_DATE_CAP,
            f"The invoice date {raw} is more than five years old.",
        )


def _check_currency(values: Mapping[str, object], outcome: CheckOutcome) -> None:
    currency = values.get("currency")
    if isinstance(currency, str) and currency and not re.fullmatch(r"[A-Z]{3}", currency):
        outcome.flag(
            "currency",
            BAD_CURRENCY_CAP,
            f"'{currency}' is not a three-letter currency code.",
        )


def _check_company(
    values: Mapping[str, object],
    known_companies: Iterable[str],
    outcome: CheckOutcome,
) -> None:
    known = {name.strip().casefold() for name in known_companies if name.strip()}
    company = values.get("company")
    if not known or not isinstance(company, str) or not company.strip():
        return
    if company.strip().casefold() not in known:
        outcome.flag(
            "company",
            UNKNOWN_COMPANY_CAP,
            f"The company '{company}' is not one of the configured companies.",
        )
