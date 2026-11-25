"""Applied migrations are checksummed by the runner; editing one breaks every
deployed database. Add a new numbered file instead (019 onwards)."""

import hashlib
import re

from app.postgres_migrate import MIGRATIONS_DIRECTORY

FROZEN_MIGRATIONS = {
    "001_authoritative_schema.sql": "5123c3f23ddd528445250b31bfa8c2551cf7c868f857508770f10e347c2acf91",
    "002_document_classification.sql": "d7de1470515247f185e57891e3af8e394bd6d4367b0c3cf892b31e3ef650d27e",
    "003_six_digit_irj_numbers.sql": "0170492f483ae8efa7109bfcac9d39ac4d6506b3483327b2b234924f7b137cb9",
    "004_supplier_invoice_number_patterns.sql": "5731feec66ec9c3bcc1dede8f251f6ec1391b41a7d457786461f6520f1425802",
    "005_configuration_indexes.sql": "6a59a7414ae0d4f2e278d3b2cf1a2c7051ae08f5c8d21db0dabd5a60444887e4",
    "006_reconciled_at.sql": "94b4f345358b241a444567fc4892fde06c9283d43dca2396a72d8e84467e5dfe",
    "007_company_irj_sequences.sql": "af6e34967e7e6a03fd8fa4403fad08fd8d8a9f3a9cb70d7079997730b18c0147",
    "008_seed_company_irj_sequences.sql": "0d872985fc62bde3284fe2af9324983876c6ab28c28d49500a75f20e8506d35e",
    "009_link_suppliers_to_companies.sql": "a806e5c070a80a8eb93b109809d3e0ade22c38c026c7c30c2fe1535104bd60a7",
    "010_remove_users_add_email_stages.sql": "a4f9e420b50b9813469a8fd9ff88c537a100a5773978b500405418cc293adb09",
    "011_power_bi_reporting_views.sql": "4210f4d0e0fd170b3149e613a872351f305b030ff59c9db111b857ee96ff6d48",
    "012_postgres_runtime_state.sql": "a4a063c3466433dfbb6ee1ae6334e8dc1fb1aa89711e1703bab54b14301f2eba",
    "013_operational_visibility.sql": "9976d3d2fafe175c505458c924fa752503cf3005126228060c71b69b44ea9306",
    "014_approver_assignment_indexes.sql": "e8ce2ec0c165879f6b7914d32be7258f02ec6597898e52b43d442126edb56531",
    "015_approval_reminders.sql": "3d1ccc000ed6c61c648d64803573b8e3de25b3222f0f2d1bd74e3ff15969a3c0",
    "016_email_stage_sent_marker.sql": "6ce277f529afd6ce8820348cdfe9ae243660d12a12abb1a7101571d23f107a56",
    "017_invoice_lookup_indexes.sql": "f4e8a8344a21f8ca27b3238776659ba0ba502e2ec8f416acf7885dc54fb78092",
    "018_case_insensitive_irj_sequences.sql": "70e95e58c7ffc37f57acf97787e80b30449daf15ce6c749082fa15cecdc0a6e2",
}


def runner_checksum(text: str) -> str:
    """The same normalisation PostgresInvoiceStore applies before hashing."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def test_applied_migrations_are_never_edited() -> None:
    present = {path.name: path for path in MIGRATIONS_DIRECTORY.glob("*.sql")}

    for name, checksum in FROZEN_MIGRATIONS.items():
        assert name in present, f"{name} was renamed or deleted"
        actual = runner_checksum(present[name].read_text(encoding="utf-8"))
        assert actual == checksum, f"{name} was edited; add a new migration instead"


def test_new_migrations_are_numbered_after_the_frozen_ones() -> None:
    highest = max(int(name[:3]) for name in FROZEN_MIGRATIONS)
    for path in MIGRATIONS_DIRECTORY.glob("*.sql"):
        if path.name in FROZEN_MIGRATIONS:
            continue
        match = re.match(r"(\d+)_", path.name)
        assert match and int(match.group(1)) > highest, (
            f"{path.name} must be numbered {highest + 1:03d} or later"
        )
