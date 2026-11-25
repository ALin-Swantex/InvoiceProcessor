from __future__ import annotations

import re
from pathlib import Path


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


def _alphanumeric(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def near_miss_number_warnings(
    value: str | None, *sources: str | None
) -> list[str]:
    """Warn when the file name or email subject carries a number one or two
    characters away from the one read, e.g. '335835.pdf' read as 335235.

    On a scanned invoice the text checks cannot run, so this is the only
    cross-check on a misread digit. An unrelated number in the file name, or
    the read number appearing in it, raises nothing."""
    wanted = _alphanumeric(value or "")
    if len(wanted) < 4:
        return []
    for source in sources:
        if not source:
            continue
        name = Path(source).stem if source.lower().endswith(".pdf") else source
        if wanted in _alphanumeric(name):
            return []
    for source in sources:
        if not source:
            continue
        name = Path(source).stem if source.lower().endswith(".pdf") else source
        # Invoice numbers keep their own '-', '.' and '/' ('INV-1007').
        for raw in re.findall(r"[A-Za-z0-9](?:[A-Za-z0-9./-]*[A-Za-z0-9])?", name):
            token = _alphanumeric(raw)
            if len(token) != len(wanted) or not any(c.isdigit() for c in token):
                continue
            differences = sum(a != b for a, b in zip(token, wanted))
            if 1 <= differences <= 2:
                return [
                    f"The invoice number was read as '{value}', but '{source}' "
                    f"suggests '{raw}'. Check the number on the invoice."
                ]
    return []
