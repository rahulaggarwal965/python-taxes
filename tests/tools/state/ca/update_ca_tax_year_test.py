"""Tests for tools/state/ca/update_ca_tax_year.py.

Verifies the PDF extraction pipeline produces values that exactly match
the manually-verified data already committed in the codebase, for every
year where we have both a PDF and codebase data.

Usage:
    uv run --group tools --group test python -m pytest tests/tools/state/ca/ -v
"""

import sys
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

# Make the tools/state/ca directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent / "tools" / "state" / "ca"))

import update_ca_tax_year as tool  # noqa: E402

from python_taxes.state.ca.income.tables import (  # noqa: E402
    MAX,
    CARateRow,
    estimated_deduction_allowance,
    exemption_allowance_credit,
    low_income_exemption,
    standard_deduction,
)
from python_taxes.state.ca.income.tables import hoh as hoh_tables  # noqa: E402
from python_taxes.state.ca.income.tables import married as married_tables  # noqa: E402
from python_taxes.state.ca.income.tables import single as single_tables  # noqa: E402

CODEBASE_SCHEDULES = {
    "single": single_tables.schedule,
    "married": married_tables.schedule,
    "hoh": hoh_tables.schedule,
}


# ---------------------------------------------------------------------------
# PDF download helpers
# ---------------------------------------------------------------------------

PDF_DIR = Path("/tmp/claude")

EDD_PDF_URLS = {
    2024: "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/24methb.pdf",
    2025: "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/25methb.pdf",
    2026: "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/26methb.pdf",
}

PDF_PATHS = {
    year: PDF_DIR / f"ca_methb_{year}.pdf"
    for year in EDD_PDF_URLS
}


def _ensure_pdf(year: int) -> bool:
    """Download the CA PDF for a given year if not already cached."""
    path = PDF_PATHS.get(year)
    if path is None:
        return False
    if path.exists():
        return True
    url = EDD_PDF_URLS.get(year)
    if url is None:
        return False
    try:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, path)
        return path.exists()
    except Exception:
        return False


def _pdf_available(year: int) -> bool:
    return _ensure_pdf(year)


def _extract_all(year: int):
    """Extract brackets and lookup tables from a year's PDF."""
    import pdfplumber

    pdf_path = str(PDF_PATHS[year])
    with pdfplumber.open(pdf_path) as pdf:
        detected_year = tool.detect_year(pdf)
        assert detected_year == year
        lookup = tool.extract_lookup_tables(pdf)
        brackets = tool.extract_brackets(pdf)
    return brackets, lookup


# ---------------------------------------------------------------------------
# Parsing unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestParseDollar:
    def test_simple(self):
        assert tool.parse_dollar("$0") == Decimal("0")

    def test_with_cents(self):
        assert tool.parse_dollar("$2,480.00") == Decimal("2480.00")

    def test_large(self):
        assert tool.parse_dollar("$115,488.06") == Decimal("115488.06")

    def test_comma_thousands(self):
        assert tool.parse_dollar("$1,000,000") == Decimal("1000000")


class TestParsePercent:
    def test_integer_like(self):
        assert tool.parse_percent("1.100%") == Decimal("1.100")

    def test_decimal(self):
        assert tool.parse_percent("10.230%") == Decimal("10.230")

    def test_two_decimal(self):
        assert tool.parse_percent("14.630%") == Decimal("14.630")


# ---------------------------------------------------------------------------
# Validation unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestBracketValidation:
    def _good_rows(self):
        return [
            CARateRow(Decimal("0"), Decimal("10000"), Decimal("0"), Decimal("1.1")),
            CARateRow(Decimal("10000"), Decimal("50000"), Decimal("110"), Decimal("2.2")),
            CARateRow(Decimal("50000"), MAX, Decimal("990"), Decimal("4.4")),
        ]

    def _make_tables(self, rows=None):
        if rows is None:
            rows = self._good_rows()
        return [tool.TableData(s, rows) for s in ["single", "married", "hoh"]]

    def test_valid_tables_pass(self):
        tool.validate_brackets(self._make_tables(), 9999)

    def test_wrong_table_count_raises(self):
        with pytest.raises(ValueError, match="Expected 3 tables"):
            tool.validate_brackets(self._make_tables()[:2], 9999)

    def test_first_row_nonzero_min_raises(self):
        bad = self._good_rows()
        bad[0] = CARateRow(Decimal("100"), bad[0].max, bad[0].withhold_amount, bad[0].percent)
        with pytest.raises(ValueError, match="first row min should be 0"):
            tool.validate_brackets(self._make_tables(bad), 9999)

    def test_non_increasing_pct_raises(self):
        bad = self._good_rows()
        bad[2] = CARateRow(bad[2].min, bad[2].max, bad[2].withhold_amount, Decimal("2.2"))
        with pytest.raises(ValueError, match="not increasing"):
            tool.validate_brackets(self._make_tables(bad), 9999)

    def test_bracket_gap_raises(self):
        bad = self._good_rows()
        bad[1] = CARateRow(Decimal("10001"), bad[1].max, bad[1].withhold_amount, bad[1].percent)
        with pytest.raises(ValueError, match="min=10001"):
            tool.validate_brackets(self._make_tables(bad), 9999)

    def test_last_row_not_max_raises(self):
        bad = self._good_rows()
        bad[-1] = CARateRow(bad[-1].min, Decimal("99999"), bad[-1].withhold_amount, bad[-1].percent)
        with pytest.raises(ValueError, match="last row max should be MAX"):
            tool.validate_brackets(self._make_tables(bad), 9999)


class TestLookupValidation:
    def test_valid_passes(self):
        lookup = tool.LookupTables(
            low_income_single=Decimal("18896"),
            low_income_high=Decimal("37791"),
            standard_deduction_single=Decimal("5706"),
            standard_deduction_high=Decimal("11412"),
            exemption_credit=Decimal("168.30"),
            estimated_deduction=Decimal("1000"),
        )
        tool.validate_lookup(lookup, 9999)

    def test_high_not_above_single_raises(self):
        lookup = tool.LookupTables(
            low_income_single=Decimal("18896"),
            low_income_high=Decimal("18896"),
            standard_deduction_single=Decimal("5706"),
            standard_deduction_high=Decimal("11412"),
            exemption_credit=Decimal("168.30"),
            estimated_deduction=Decimal("1000"),
        )
        with pytest.raises(ValueError, match="high threshold must exceed"):
            tool.validate_lookup(lookup, 9999)


# ---------------------------------------------------------------------------
# Code generation unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestCodeGeneration:
    def test_generate_bracket_block(self):
        rows = [
            CARateRow(Decimal("0"), Decimal("10000"), Decimal("0"), Decimal("1.1")),
            CARateRow(Decimal("10000"), MAX, Decimal("110"), Decimal("2.2")),
        ]
        code = tool.generate_bracket_block(rows, 2099)
        assert "    2099: [" in code
        assert 'min=Decimal("0")' in code
        assert 'max=Decimal("10000")' in code
        assert "max=MAX" in code
        assert 'withhold_amount=Decimal("0.00")' in code
        assert 'percent=Decimal("1.1")' in code
        assert 'percent=Decimal("2.2")' in code

    def test_format_decimal(self):
        assert tool.format_decimal(Decimal("0")) == "0.00"
        assert tool.format_decimal(Decimal("1234")) == "1234.00"
        assert tool.format_decimal(Decimal("99.5")) == "99.50"
        assert tool.format_decimal(Decimal("115488.06")) == "115488.06"


# ---------------------------------------------------------------------------
# PDF extraction integration tests (require network for PDF download)
# ---------------------------------------------------------------------------


def _make_extraction_tests(year):
    """Generate a test class for a specific year's PDF extraction."""

    @pytest.mark.skipif(
        not _pdf_available(year),
        reason=f"{year} CA PDF not available",
    )
    class ExtractionTests:
        @pytest.fixture(scope="class")
        def extracted(self):
            return _extract_all(year)

        @pytest.fixture(scope="class")
        def brackets(self, extracted):
            return extracted[0]

        @pytest.fixture(scope="class")
        def lookup(self, extracted):
            return extracted[1]

        def test_table_count(self, brackets):
            assert len(brackets) == 3

        def test_validates(self, brackets):
            tool.validate_brackets(brackets, year)

        @pytest.mark.parametrize("filing_status", ["single", "married", "hoh"])
        def test_brackets_match_codebase(self, brackets, filing_status):
            extracted = {t.filing_status: t.rows for t in brackets}
            tool_rows = extracted[filing_status]
            codebase_rows = CODEBASE_SCHEDULES[filing_status][year]

            assert len(tool_rows) == len(codebase_rows)
            for i, (ext, cb) in enumerate(zip(tool_rows, codebase_rows)):
                assert ext.min == cb.min, f"row {i} min: {ext.min} != {cb.min}"
                assert ext.max == cb.max, f"row {i} max: {ext.max} != {cb.max}"
                assert ext.withhold_amount == cb.withhold_amount, (
                    f"row {i} withhold: {ext.withhold_amount} != {cb.withhold_amount}"
                )
                assert ext.percent.normalize() == cb.percent.normalize(), (
                    f"row {i} percent: {ext.percent} != {cb.percent}"
                )

        def test_low_income_matches(self, lookup):
            codebase = low_income_exemption[year]
            assert lookup.low_income_single == codebase[0]
            assert lookup.low_income_high == codebase[1]

        def test_standard_deduction_matches(self, lookup):
            codebase = standard_deduction[year]
            assert lookup.standard_deduction_single == codebase[0]
            assert lookup.standard_deduction_high == codebase[1]

        def test_exemption_credit_matches(self, lookup):
            assert lookup.exemption_credit == exemption_allowance_credit[year]

        def test_estimated_deduction_matches(self, lookup):
            assert lookup.estimated_deduction == estimated_deduction_allowance[year]

    ExtractionTests.__name__ = f"TestExtraction{year}"
    ExtractionTests.__qualname__ = f"TestExtraction{year}"
    return ExtractionTests


TestExtraction2024 = _make_extraction_tests(2024)
TestExtraction2025 = _make_extraction_tests(2025)
TestExtraction2026 = _make_extraction_tests(2026)
