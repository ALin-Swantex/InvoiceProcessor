"""The mailbox sorting script, run against a fake mailbox and a fake reader."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai_extraction import ExtractionError, ExtractionResult, ExtractionServiceError, ReferenceData
from scripts.sort_mailbox_pdfs import (
    Attachment,
    destination,
    run,
    safe_part,
)

REFERENCE = ReferenceData(
    companies=(("Acme Trading Ltd", ()),),
    suppliers=(("Bolt Supplies Ltd", ()),),
)


def result(**overrides) -> ExtractionResult:
    values = dict(
        company="Acme Trading Ltd", supplier="Bolt Supplies Ltd",
        supplier_invoice_number="INV-1", purchase_order_number=None,
        invoice_date="2026-03-01", invoice_value=10.0, currency="GBP",
        confidence=0.95, needs_review=False, field_confidences={}, warnings=(),
    )
    values.update(overrides)
    return ExtractionResult(**values)


class FakeSession:
    def __init__(self, messages, content) -> None:
        self.messages, self.content = messages, content
        self.downloads = 0

    def paginate(self, path, params=None, operation=""):
        yield {"value": self.messages}

    def download(self, path, **kwargs):
        self.downloads += 1
        return self.content[path.split("/attachments/")[1].split("/")[0]]


class FakeClient:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.settings = SimpleNamespace(mailbox="a@b.test", max_pdf_bytes=10_000_000)
        self.files = files
        messages = [
            {"id": f"m-{name}", "subject": "s", "receivedDateTime": "2026-03-02T10:00:00Z",
             "from": {"emailAddress": {"address": "x@y.test"}}}
            for name in files
        ]
        self.session = FakeSession(messages, files)

    def list_pdf_attachments(self, message_id):
        name = message_id.removeprefix("m-")
        return [{"id": name, "name": f"{name}.pdf", "size": len(self.files[name])}]


def pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n" + tag.encode()


def test_destination_uses_exact_known_names_and_holding_folders() -> None:
    assert destination(result(), REFERENCE) == Path("Acme Trading Ltd/Bolt Supplies Ltd")
    assert destination(result(supplier="New Co"), REFERENCE) == Path(
        "Acme Trading Ltd/_Unregistered suppliers/New Co"
    )
    assert destination(result(company="Other Ltd"), REFERENCE) == Path(
        "_Unknown company/Bolt Supplies Ltd"
    )
    assert destination(result(company=None, supplier=None), REFERENCE) == Path(
        "_Unknown company/Unknown"
    )
    assert destination(result(document_type="statement"), REFERENCE) == Path("_Statements")
    assert destination(None, REFERENCE) == Path("_Unreadable")


def test_folder_names_cannot_escape_the_output_folder() -> None:
    assert "/" not in safe_part("../../etc/passwd")
    assert safe_part("  ..  ") == "Unknown"
    assert destination(result(supplier="../x"), None) == Path("Acme Trading Ltd/x")


def test_dry_run_counts_but_downloads_and_reads_nothing(tmp_path: Path) -> None:
    client = FakeClient({"one": pdf("1"), "two": pdf("2")})
    calls = []

    counts = run(client, output=tmp_path, reference=REFERENCE, dry_run=True,
                 extractor=lambda *a, **k: calls.append(1))

    assert counts["found"] == 2
    assert client.session.downloads == 0 and calls == []
    assert not (tmp_path / "index.csv").exists()


def test_pdfs_are_sorted_indexed_and_skipped_on_a_rerun(tmp_path: Path) -> None:
    client = FakeClient({"one": pdf("1"), "two": pdf("2")})
    extractor = lambda path, reference=None: result()

    first = run(client, output=tmp_path, reference=REFERENCE, extractor=extractor, workers=1)

    assert first["saved"] == 2
    saved = sorted(p.name for p in (tmp_path / "Acme Trading Ltd" / "Bolt Supplies Ltd").iterdir())
    assert saved == ["2026-03-01_INV-1.pdf", "2026-03-01_INV-1_2.pdf"]  # same name, never overwritten
    assert (tmp_path / "index.csv").read_text().count("\n") == 3  # header + 2 rows

    second = run(client, output=tmp_path, reference=REFERENCE, extractor=extractor, workers=1)
    assert second["found"] == 0


def test_identical_pdfs_are_saved_once(tmp_path: Path) -> None:
    client = FakeClient({"one": pdf("same"), "two": pdf("same")})

    counts = run(client, output=tmp_path, reference=REFERENCE,
                 extractor=lambda path, reference=None: result(), workers=1)

    assert counts["saved"] == 1 and counts["duplicate"] == 1


def test_unreadable_pdfs_are_kept_and_service_errors_are_retried(tmp_path: Path) -> None:
    client = FakeClient({"bad": pdf("bad"), "down": pdf("down")})

    def extractor(path, reference=None):
        if b"bad" in path.read_bytes():
            raise ExtractionError("cut off")
        raise ExtractionServiceError("rate limited")

    counts = run(client, output=tmp_path, reference=REFERENCE, extractor=extractor, workers=1)

    assert counts["unreadable"] == 1 and counts["failed"] == 1
    assert list((tmp_path / "_Unreadable").iterdir())
    # the failed one is not recorded, so the next run tries it again
    again = run(client, output=tmp_path, reference=REFERENCE,
                extractor=lambda path, reference=None: result(), workers=1)
    assert again["found"] == 1 and again["saved"] == 1


def test_declining_the_prompt_sends_nothing(tmp_path: Path) -> None:
    client = FakeClient({"one": pdf("1")})

    counts = run(client, output=tmp_path, reference=REFERENCE, confirm=lambda n: False,
                 extractor=lambda *a, **k: pytest.fail("must not read"))

    assert counts["saved"] == 0 and client.session.downloads == 0


def test_limit_caps_how_many_pdfs_are_handled(tmp_path: Path) -> None:
    client = FakeClient({f"f{n}": pdf(str(n)) for n in range(5)})

    counts = run(client, output=tmp_path, reference=REFERENCE, limit=2, dry_run=True)

    assert counts["found"] == 2
