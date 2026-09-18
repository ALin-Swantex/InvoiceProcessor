from pathlib import Path

from app.irj import IrjNumberGenerator


def test_company_irj_sequences_are_independent(tmp_path: Path) -> None:
    generator = IrjNumberGenerator(tmp_path / "invoices.db")
    generator.set_current("GIFTED", "004321")
    generator.set_current("GBCC", "000099")

    assert generator.current("GIFTED") == "004321"
    assert generator.current("GBCC") == "000099"
    assert generator.generate("GIFTED") == "004322"
    assert generator.generate("GBCC") == "000100"
    assert generator.current("GIFTED") == "004322"
    assert generator.current("GBCC") == "000100"


def test_reserving_irj_only_advances_matching_company(tmp_path: Path) -> None:
    generator = IrjNumberGenerator(tmp_path / "invoices.db")
    generator.reserve("000250", "GIFTED")

    assert generator.generate("GIFTED") == "000251"
    assert generator.generate("GBCC") == "000001"
