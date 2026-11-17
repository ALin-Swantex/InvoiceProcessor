"""Save every PDF in a mailbox into Company/Supplier folders.

A one-off backfill. It only READS the mailbox (nothing is marked read, moved or
deleted) and only READS the company and supplier lists from the database. Each
PDF is sent to Claude to read the company and supplier, then copied into

    <output>/<Company>/<Supplier>/<date>_<invoice number>.pdf

Names that are not in your registers go to clearly named holding folders
instead of a guessed one: ``_Unknown company``, ``<Company>/_Unregistered
suppliers``, ``_Statements`` and ``_Unreadable``. ``index.csv`` lists every file
with what was read from it, and the run can be stopped and repeated safely:
already-sorted attachments are skipped.

    python -m scripts.sort_mailbox_pdfs --output ~/Desktop/sorted --dry-run
    python -m scripts.sort_mailbox_pdfs --output ~/Desktop/sorted --limit 25

Each PDF read is a paid API call and the invoice leaves your network, so the
script shows the count and asks before sending anything (``--yes`` skips that).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

from app.ai_extraction import (
    ExtractionError,
    ExtractionResult,
    ReferenceData,
    ai_extraction_configured,
    run_ai_extraction,
)
from app.environment import load_project_environment

UNKNOWN_COMPANY = "_Unknown company"
UNREGISTERED_SUPPLIERS = "_Unregistered suppliers"
STATEMENTS = "_Statements"
UNREADABLE = "_Unreadable"
UNKNOWN = "Unknown"
ESTIMATED_COST_PER_PDF = "about $0.01-0.05"
INDEX_COLUMNS = (
    "saved_as", "received", "sender", "subject", "original_name", "company",
    "supplier", "invoice_number", "invoice_date", "invoice_value", "currency",
    "confidence", "note",
)


@dataclass(frozen=True)
class Attachment:
    message_id: str
    attachment_id: str
    name: str
    size: int
    received: str
    sender: str
    subject: str

    @property
    def key(self) -> str:
        return f"{self.message_id}\0{self.attachment_id}"


# ---------------------------------------------------------------------------
# Choosing the folder (pure, so it can be tested without a mailbox)
# ---------------------------------------------------------------------------


def safe_part(text: str | None, fallback: str = UNKNOWN) -> str:
    """A string that is safe to use as one file or folder name."""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:80] or fallback


def destination(
    result: ExtractionResult | None, reference: ReferenceData | None
) -> Path:
    """The folder (relative to the output folder) for one extraction."""
    if result is None:
        return Path(UNREADABLE)
    if result.document_type == "statement":
        return Path(STATEMENTS)
    company, supplier = result.company, result.supplier
    if reference is None:  # no register available: trust the printed names
        return Path(safe_part(company, UNKNOWN_COMPANY), safe_part(supplier))
    known_companies = {name for name, _ in reference.companies}
    known_suppliers = {name for name, _ in reference.suppliers}
    if company not in known_companies:
        return Path(UNKNOWN_COMPANY, safe_part(supplier))
    if supplier not in known_suppliers:
        return Path(safe_part(company), UNREGISTERED_SUPPLIERS, safe_part(supplier))
    return Path(safe_part(company), safe_part(supplier))


def file_name(result: ExtractionResult | None, attachment: Attachment) -> str:
    stem = Path(attachment.name).stem
    if result is not None and result.supplier_invoice_number:
        stem = result.supplier_invoice_number
    date = (result.invoice_date if result is not None else None) or attachment.received[:10]
    return f"{safe_part(date, 'undated')}_{safe_part(stem, 'invoice')}.pdf"


def free_path(folder: Path, name: str) -> Path:
    candidate = folder / name
    counter = 2
    while candidate.exists():
        candidate = folder / f"{Path(name).stem}_{counter}.pdf"
        counter += 1
    return candidate


# ---------------------------------------------------------------------------
# Progress and index files
# ---------------------------------------------------------------------------


class Ledger:
    """Remembers what is already sorted so a rerun skips it."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.progress_path = output / ".progress.jsonl"
        self.index_path = output / "index.csv"
        self.done_keys: set[str] = set()
        self.hashes: dict[str, str] = {}
        if self.progress_path.exists():
            for line in self.progress_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                self.done_keys.add(entry["key"])
                if entry.get("sha256"):
                    self.hashes.setdefault(entry["sha256"], entry["path"])

    def record(self, attachment: Attachment, digest: str, path: str, row: dict[str, Any]) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        with self.progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": attachment.key, "sha256": digest, "path": path}) + "\n")
        new_file = not self.index_path.exists()
        with self.index_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow({column: row.get(column, "") for column in INDEX_COLUMNS})
        self.done_keys.add(attachment.key)
        self.hashes.setdefault(digest, path)


# ---------------------------------------------------------------------------
# Mailbox access (read-only)
# ---------------------------------------------------------------------------


def scan_attachments(
    client: Any,
    *,
    folder: str,
    since: datetime | None,
    limit: int,
    skip: set[str],
) -> Iterator[Attachment]:
    """Yield PDF attachments, newest mail first, until ``limit`` are found."""
    mailbox = quote(client.settings.mailbox, safe="")
    path = (
        f"/users/{mailbox}/messages"
        if folder == "all"
        else f"/users/{mailbox}/mailFolders/{quote(folder, safe='')}/messages"
    )
    # Graph only accepts a date sort when the date is also the first filter.
    stamp = (
        since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if since is not None
        else "1900-01-01T00:00:00Z"
    )
    filters = [f"receivedDateTime ge {stamp}", "hasAttachments eq true"]
    params = {
        "$top": "50",
        "$select": "id,subject,from,receivedDateTime",
        "$filter": " and ".join(filters),
        "$orderby": "receivedDateTime desc",
    }
    found = 0
    for page in client.session.paginate(path, params=params, operation="List mailbox messages"):
        for message in page.get("value", []):
            sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
            for attachment in client.list_pdf_attachments(message["id"]):
                item = Attachment(
                    message_id=message["id"],
                    attachment_id=attachment["id"],
                    name=str(attachment.get("name") or "invoice.pdf"),
                    size=int(attachment.get("size") or 0),
                    received=str(message.get("receivedDateTime") or ""),
                    sender=sender,
                    subject=str(message.get("subject") or ""),
                )
                if item.key in skip:
                    continue
                yield item
                found += 1
                if found >= limit:
                    return


def download(client: Any, attachment: Attachment) -> bytes:
    mailbox = quote(client.settings.mailbox, safe="")
    content = client.session.download(
        f"/users/{mailbox}/messages/{quote(attachment.message_id, safe='')}"
        f"/attachments/{quote(attachment.attachment_id, safe='')}/$value",
        max_bytes=client.settings.max_pdf_bytes,
        too_large_error=lambda: ExtractionError("The PDF is larger than the size limit."),
        operation="Download attachment",
    )
    if not content.startswith(b"%PDF-"):
        raise ExtractionError("The attachment is not a valid PDF.")
    return content


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def read_pdf(
    content: bytes,
    reference: ReferenceData | None,
    extractor: Callable[..., ExtractionResult],
) -> ExtractionResult:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "invoice.pdf"
        path.write_bytes(content)
        return extractor(path, reference=reference)


def process(
    attachment: Attachment,
    *,
    client: Any,
    reference: ReferenceData | None,
    extractor: Callable[..., ExtractionResult],
) -> tuple[bytes, ExtractionResult | None, str]:
    """Download and read one PDF (runs in a worker thread; touches no files)."""
    content = download(client, attachment)
    try:
        return content, read_pdf(content, reference, extractor), ""
    except ExtractionError as error:  # unreadable: keep the PDF, in _Unreadable
        return content, None, str(error)


def save(
    attachment: Attachment,
    content: bytes,
    result: ExtractionResult | None,
    note: str,
    *,
    output: Path,
    reference: ReferenceData | None,
    ledger: Ledger,
) -> str:
    digest = hashlib.sha256(content).hexdigest()
    row: dict[str, Any] = {
        "received": attachment.received[:10],
        "sender": attachment.sender,
        "subject": attachment.subject,
        "original_name": attachment.name,
    }
    if digest in ledger.hashes:
        row["saved_as"] = ledger.hashes[digest]
        row["note"] = "identical PDF already saved; not copied again"
        ledger.record(attachment, digest, ledger.hashes[digest], row)
        return "duplicate"
    folder = output / destination(result, reference)
    folder.mkdir(parents=True, exist_ok=True)
    target = free_path(folder, file_name(result, attachment))
    target.write_bytes(content)
    relative = str(target.relative_to(output))
    if result is not None:
        row.update(
            company=result.company, supplier=result.supplier,
            invoice_number=result.supplier_invoice_number,
            invoice_date=result.invoice_date, invoice_value=result.invoice_value,
            currency=result.currency, confidence=f"{result.confidence:.2f}",
            note="; ".join(result.warnings),
        )
    else:
        row["note"] = note
    row["saved_as"] = relative
    ledger.record(attachment, digest, relative, row)
    return "saved" if result is not None else "unreadable"


def run(
    client: Any,
    *,
    output: Path,
    reference: ReferenceData | None,
    extractor: Callable[..., ExtractionResult] = run_ai_extraction,
    folder: str = "inbox",
    since: datetime | None = None,
    limit: int = 25,
    workers: int = 3,
    dry_run: bool = False,
    confirm: Callable[[int], bool] = lambda count: True,
    say: Callable[[str], None] = print,
) -> dict[str, int]:
    ledger = Ledger(output)
    items = list(
        scan_attachments(client, folder=folder, since=since, limit=limit, skip=ledger.done_keys)
    )
    megabytes = sum(item.size for item in items) / 1_000_000
    say(f"Found {len(items)} PDF(s) not yet sorted ({megabytes:.1f} MB).")
    counts = {"found": len(items), "saved": 0, "duplicate": 0, "unreadable": 0, "failed": 0}
    if dry_run or not items:
        return counts
    if not confirm(len(items)):
        say("Cancelled. Nothing was downloaded or sent.")
        return counts

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(process, item, client=client, reference=reference, extractor=extractor): item
            for item in items
        }
        for number, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                content, result, note = future.result()
            except Exception as error:  # service down, rate limit, network: retry next run
                counts["failed"] += 1
                say(f"[{number}/{len(items)}] {item.name}: not done ({error}); it will be retried on the next run.")
                continue
            outcome = save(item, content, result, note, output=output, reference=reference, ledger=ledger)
            counts[outcome] += 1
            say(f"[{number}/{len(items)}] {item.name}: {outcome}")
    return counts


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def load_reference() -> ReferenceData | None:
    """Known companies and suppliers from the database (read-only)."""
    try:
        from app.postgres_config import postgres_config_stores

        companies, suppliers, *_ = postgres_config_stores()
        return ReferenceData(
            companies=tuple((c.name, tuple(c.aliases)) for c in companies.list()),
            suppliers=tuple((s.name, tuple(s.aliases)) for s in suppliers.list()),
        )
    except Exception as error:
        print(f"Could not read the company/supplier lists ({error}).")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", required=True, type=Path, help="folder to save into (outside the repository)")
    parser.add_argument("--folder", default="inbox", help="mail folder name, or 'all' for the whole mailbox (default: inbox)")
    parser.add_argument("--since", help="only mail received on or after this date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=25, help="most PDFs to sort this run (default 25: a small trial)")
    parser.add_argument("--workers", type=int, default=3, help="PDFs read at the same time (default 3)")
    parser.add_argument("--dry-run", action="store_true", help="only count the PDFs; download and send nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask before sending PDFs to Claude")
    parser.add_argument("--no-register", action="store_true", help="do not use the company/supplier lists from the database")
    args = parser.parse_args(argv)

    load_project_environment()
    if not args.dry_run and not ai_extraction_configured():
        print("AI extraction is not configured: set ANTHROPIC_API_KEY in .env first.")
        return 2
    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    )
    from app.outlook_graph import graph_client_from_environment

    client = graph_client_from_environment()
    reference = None if args.no_register else load_reference()
    if reference is None and not args.no_register:
        print("Continuing without the lists: names will be used as printed.")

    def confirm(count: int) -> bool:
        if args.yes:
            return True
        answer = input(
            f"Send {count} PDF(s) to Claude to read ({ESTIMATED_COST_PER_PDF} each)? [y/N] "
        )
        return answer.strip().casefold() in {"y", "yes"}

    counts = run(
        client, output=args.output, reference=reference, folder=args.folder, since=since,
        limit=args.limit, workers=args.workers, dry_run=args.dry_run, confirm=confirm,
    )
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
