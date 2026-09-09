from __future__ import annotations

import re


_ALLOWED_INVOICE_NUMBER = re.compile(r"^[A-Za-z0-9./-]+$")
_PATTERN_TOKENS = {
    "#": r"[0-9]",
    "@": r"[A-Za-z]",
    "*": r"[A-Za-z0-9]",
}


def validate_invoice_number_pattern(pattern: str | None) -> str | None:
    if pattern is None:
        return None
    normalized = pattern.strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError("Invoice number patterns must not exceed 128 characters.")
    if any(character.isspace() for character in normalized):
        raise ValueError("Invoice number patterns must not contain whitespace.")
    if not any(character in _PATTERN_TOKENS for character in normalized):
        raise ValueError(
            "An invoice number pattern must contain #, @, or * placeholders."
        )
    return normalized


def invoice_number_warnings(
    value: str | None,
    *,
    supplier: str | None = None,
    pattern: str | None = None,
) -> list[str]:
    if value is None or value == "":
        return []
    warnings: list[str] = []
    if len(value) > 128:
        warnings.append("Supplier invoice number exceeds 128 characters.")
        return warnings
    if value != value.strip() or any(character.isspace() for character in value):
        warnings.append(
            "Supplier invoice number contains whitespace or disconnected components."
        )
    if not _ALLOWED_INVOICE_NUMBER.fullmatch(value):
        warnings.append(
            "Supplier invoice number contains characters other than letters, "
            "digits, hyphens, slashes, or full stops."
        )
    normalized_pattern = validate_invoice_number_pattern(pattern)
    if normalized_pattern and not re.fullmatch(
        _pattern_regex(normalized_pattern), value
    ):
        supplier_label = supplier or "This supplier"
        warnings.append(
            f"Supplier invoice number does not match the configured pattern "
            f"for '{supplier_label}' ({normalized_pattern})."
        )
    return warnings


def _pattern_regex(pattern: str) -> str:
    return "".join(
        _PATTERN_TOKENS.get(character, re.escape(character))
        for character in pattern
    )
