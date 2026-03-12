"""Tests for tools/federal/update_federal_tax_year.py.

Verifies the PDF extraction pipeline produces values that exactly match
the manually-verified data already committed in the codebase, for every
year where we have both a PDF and codebase data.

Usage:
    uv run --group tools --group test python -m pytest tests/tools/federal/ -v
"""

import sys
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

# Make the tools/federal directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "tools" / "federal"))

import update_federal_tax_year as tool  # noqa: E402

from python_taxes.federal.income.tables.percentage import MAX  # noqa: E402
from python_taxes.federal.income.tables.percentage.automated import (  # noqa: E402
    hoh,
    married,
    single,
)
from python_taxes.federal.social_security import wage_limit  # noqa: E402

# Map (filing_status, schedule_type) to codebase dicts
CODEBASE_TABLES = {
    ("single", "standard"): single.standard_schedule,
    ("single", "multiple_jobs"): single.multiple_jobs,
    ("married", "standard"): married.standard_schedule,
    ("married", "multiple_jobs"): married.multiple_jobs,
    ("hoh", "standard"): hoh.standard_schedule,
    ("hoh", "multiple_jobs"): hoh.multiple_jobs,
}


# ---------------------------------------------------------------------------
# PDF download helpers
# ---------------------------------------------------------------------------

PDF_DIR = Path("/tmp/claude")

PDF_URLS = {
    2024: "https://www.irs.gov/pub/irs-prior/p15t--2024.pdf",
    2025: "https://www.irs.gov/pub/irs-prior/p15t--2025.pdf",
    2026: "https://www.irs.gov/pub/irs-prior/p15t--2026.pdf",
}

PDF_PATHS = {
    2024: PDF_DIR / "p15t_2024.pdf",
    2025: PDF_DIR / "p15t_2025.pdf",
    2026: PDF_DIR / "p15t_2026.pdf",
}


def _ensure_pdf(year: int) -> bool:
    """Download the PDF for a given year if not already cached. Returns True on success."""
    path = PDF_PATHS.get(year)
    if path is None:
        return False
    if path.exists():
        return True
    url = PDF_URLS.get(year)
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


def _extract_tables(year: int) -> list[tool.TableData]:
    """Extract tables from a year's PDF."""
    import pdfplumber

    pdf_path = str(PDF_PATHS[year])
    with pdfplumber.open(pdf_path) as pdf:
        page, detected_year = tool.find_target_page(pdf)
        assert detected_year == year
        return tool.extract_tables(page)


# ---------------------------------------------------------------------------
# Parsing unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestParseDollar:
    def test_simple(self):
        assert tool.parse_dollar("$0") == Decimal("0")

    def test_with_cents(self):
        assert tool.parse_dollar("$2,480.00") == Decimal("2480.00")

    def test_large(self):
        assert tool.parse_dollar("$206,583.50") == Decimal("206583.50")

    def test_no_dollar_sign(self):
        # Shouldn't happen in practice, but the function strips $
        assert tool.parse_dollar("19300") == Decimal("19300")


class TestParsePercent:
    def test_zero(self):
        assert tool.parse_percent("0%") == 0

    def test_nonzero(self):
        assert tool.parse_percent("37%") == 37


class TestParseDataLine:
    """Test the core line-parsing logic."""

    def test_normal_row_10_tokens(self):
        # Married row 2 from 2026 PDF
        tokens = [
            "$19,300", "$44,100", "$0.00", "10%", "$19,300",
            "$16,100", "$28,500", "$0.00", "10%", "$16,100",
        ]
        left, right = tool.parse_data_line(tokens)

        assert left.min == Decimal("19300")
        assert left.max == Decimal("44099.99")
        assert left.withhold_amount == Decimal("0.00")
        assert left.percent == 10

        assert right.min == Decimal("16100")
        assert right.max == Decimal("28499.99")
        assert right.withhold_amount == Decimal("0.00")
        assert right.percent == 10

    def test_last_row_8_tokens(self):
        # Married last row from 2026 PDF
        tokens = [
            "$788,000", "$206,583.50", "37%", "$788,000",
            "$400,450", "$103,291.75", "37%", "$400,450",
        ]
        left, right = tool.parse_data_line(tokens)

        assert left.min == Decimal("788000")
        assert left.max == MAX
        assert left.withhold_amount == Decimal("206583.50")
        assert left.percent == 37

        assert right.min == Decimal("400450")
        assert right.max == MAX
        assert right.withhold_amount == Decimal("103291.75")
        assert right.percent == 37

    def test_bad_token_count_raises(self):
        with pytest.raises(ValueError, match="Expected 8 or 10 tokens"):
            tool.parse_data_line(["$0", "$100", "10%"])


class TestParseHalf:
    def test_5_tokens(self):
        row = tool._parse_half(["$0", "$7,500", "$0.00", "0%", "$0"])
        assert row.min == Decimal("0")
        assert row.max == Decimal("7499.99")
        assert row.withhold_amount == Decimal("0.00")
        assert row.percent == 0

    def test_4_tokens_last_row(self):
        row = tool._parse_half(["$648,100", "$192,979.25", "37%", "$648,100"])
        assert row.min == Decimal("648100")
        assert row.max == MAX
        assert row.withhold_amount == Decimal("192979.25")
        assert row.percent == 37

    def test_bad_count_raises(self):
        with pytest.raises(ValueError, match="Expected 4 or 5 tokens"):
            tool._parse_half(["$0", "$100"])


# ---------------------------------------------------------------------------
# Validation unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestValidation:
    def _make_table(self, filing_status, schedule_type, rows):
        return tool.TableData(filing_status, schedule_type, rows)

    def _good_rows(self):
        """Minimal valid 3-bracket table."""
        return [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), Decimal("49999.99"), Decimal("0"), 10),
            tool.BracketRow(Decimal("50000"), MAX, Decimal("4000"), 22),
        ]

    def _make_6_tables(self, rows=None):
        if rows is None:
            rows = self._good_rows()
        tables = []
        for status in ["married", "single", "hoh"]:
            for sched in ["standard", "multiple_jobs"]:
                tables.append(self._make_table(status, sched, rows))
        return tables

    def test_valid_tables_pass(self):
        tables = self._make_6_tables()
        tool.validate_tables(tables, 9999)  # Should not raise

    def test_wrong_table_count_raises(self):
        tables = self._make_6_tables()[:5]
        with pytest.raises(ValueError, match="Expected 6 tables"):
            tool.validate_tables(tables, 9999)

    def test_missing_filing_status_raises(self):
        rows = self._good_rows()
        tables = []
        for status in ["married", "single", "single"]:
            for sched in ["standard", "multiple_jobs"]:
                tables.append(self._make_table(status, sched, rows))
        with pytest.raises(ValueError, match="Missing table combinations"):
            tool.validate_tables(tables, 9999)

    def test_different_row_counts_raises(self):
        tables = self._make_6_tables()
        # Give one table an extra row
        bad_rows = self._good_rows() + [
            tool.BracketRow(Decimal("0"), MAX, Decimal("0"), 50),
        ]
        tables[0] = self._make_table("married", "standard", bad_rows)
        with pytest.raises(ValueError, match="different row counts"):
            tool.validate_tables(tables, 9999)

    def test_different_percentages_raises(self):
        tables = self._make_6_tables()
        # Change one table's percentages
        bad_rows = [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), Decimal("49999.99"), Decimal("0"), 15),
            tool.BracketRow(Decimal("50000"), MAX, Decimal("4000"), 22),
        ]
        tables[0] = self._make_table("married", "standard", bad_rows)
        with pytest.raises(ValueError, match="different percentage sequences"):
            tool.validate_tables(tables, 9999)

    def test_first_row_nonzero_min_raises(self):
        bad_rows = [
            tool.BracketRow(Decimal("100"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), Decimal("49999.99"), Decimal("0"), 10),
            tool.BracketRow(Decimal("50000"), MAX, Decimal("4000"), 22),
        ]
        tables = self._make_6_tables(bad_rows)
        with pytest.raises(ValueError, match="first row min should be 0"):
            tool.validate_tables(tables, 9999)

    def test_non_increasing_percentages_raises(self):
        bad_rows = [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), Decimal("49999.99"), Decimal("0"), 10),
            tool.BracketRow(Decimal("50000"), MAX, Decimal("4000"), 10),  # same as prev
        ]
        tables = self._make_6_tables(bad_rows)
        with pytest.raises(ValueError, match="not increasing"):
            tool.validate_tables(tables, 9999)

    def test_bracket_gap_raises(self):
        bad_rows = [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10001"), Decimal("49999.99"), Decimal("0"), 10),  # gap
            tool.BracketRow(Decimal("50000"), MAX, Decimal("4000"), 22),
        ]
        tables = self._make_6_tables(bad_rows)
        with pytest.raises(ValueError, match="min=10001"):
            tool.validate_tables(tables, 9999)

    def test_last_row_not_max_raises(self):
        bad_rows = [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), Decimal("49999.99"), Decimal("0"), 10),
            tool.BracketRow(Decimal("50000"), Decimal("99999.99"), Decimal("4000"), 22),
        ]
        tables = self._make_6_tables(bad_rows)
        with pytest.raises(ValueError, match="last row max should be MAX"):
            tool.validate_tables(tables, 9999)


# ---------------------------------------------------------------------------
# Code generation unit tests (no PDF needed)
# ---------------------------------------------------------------------------


class TestCodeGeneration:
    def test_generate_year_block(self):
        rows = [
            tool.BracketRow(Decimal("0"), Decimal("9999.99"), Decimal("0"), 0),
            tool.BracketRow(Decimal("10000"), MAX, Decimal("500.50"), 10),
        ]
        code = tool.generate_year_block(rows, 2099)
        assert '    2099: [' in code
        assert 'min=Decimal("0.00")' in code
        assert 'max=Decimal("9999.99")' in code
        assert 'max=MAX' in code
        assert 'withhold_amount=Decimal("500.50")' in code
        assert 'percent=0,' in code
        assert 'percent=10,' in code

    def test_format_decimal_always_2dp(self):
        assert tool.format_decimal(Decimal("0")) == "0.00"
        assert tool.format_decimal(Decimal("1234")) == "1234.00"
        assert tool.format_decimal(Decimal("99.5")) == "99.50"
        assert tool.format_decimal(Decimal("206583.50")) == "206583.50"


# ---------------------------------------------------------------------------
# Social Security wage base integration tests (require network)
# ---------------------------------------------------------------------------


class TestSSWageBase:
    """Verify SS wage base fetched from Federal Register matches codebase data."""

    @pytest.mark.parametrize("year", [2024, 2025, 2026])
    def test_matches_codebase(self, year):
        fetched = tool.fetch_ss_wage_base(year)
        assert fetched == wage_limit[year], (
            f"SS wage base mismatch for {year}: fetched {fetched} != codebase {wage_limit[year]}"
        )


# ---------------------------------------------------------------------------
# PDF extraction integration tests (require downloaded PDFs)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _pdf_available(2024),
    reason="2024 PDF not available at /tmp/claude/p15t_2024.pdf",
)
class TestExtraction2024:
    """Verify tool extraction matches every value in the 2024 codebase data."""

    @pytest.fixture(scope="class")
    def tables(self):
        return _extract_tables(2024)

    def test_table_count(self, tables):
        assert len(tables) == 6

    def test_validates(self, tables):
        tool.validate_tables(tables, 2024)

    @pytest.mark.parametrize(
        "filing_status,schedule_type",
        [
            ("single", "standard"),
            ("single", "multiple_jobs"),
            ("married", "standard"),
            ("married", "multiple_jobs"),
            ("hoh", "standard"),
            ("hoh", "multiple_jobs"),
        ],
    )
    def test_matches_codebase(self, tables, filing_status, schedule_type):
        extracted = {
            (t.filing_status, t.schedule_type): t.rows for t in tables
        }
        tool_rows = extracted[(filing_status, schedule_type)]
        codebase_rows = CODEBASE_TABLES[(filing_status, schedule_type)][2024]

        assert len(tool_rows) == len(codebase_rows)
        for i, (ext, cb) in enumerate(zip(tool_rows, codebase_rows)):
            assert ext.min == cb.min, f"row {i} min: {ext.min} != {cb.min}"
            assert ext.max == cb.max, f"row {i} max: {ext.max} != {cb.max}"
            assert ext.withhold_amount == cb.withhold_amount, (
                f"row {i} withhold: {ext.withhold_amount} != {cb.withhold_amount}"
            )
            assert ext.percent == cb.percent, (
                f"row {i} percent: {ext.percent} != {cb.percent}"
            )


@pytest.mark.skipif(
    not _pdf_available(2025),
    reason="2025 PDF not available at /tmp/claude/p15t_2025.pdf",
)
class TestExtraction2025:
    """Verify tool extraction matches every value in the 2025 codebase data."""

    @pytest.fixture(scope="class")
    def tables(self):
        return _extract_tables(2025)

    def test_table_count(self, tables):
        assert len(tables) == 6

    def test_validates(self, tables):
        tool.validate_tables(tables, 2025)

    @pytest.mark.parametrize(
        "filing_status,schedule_type",
        [
            ("single", "standard"),
            ("single", "multiple_jobs"),
            ("married", "standard"),
            ("married", "multiple_jobs"),
            ("hoh", "standard"),
            ("hoh", "multiple_jobs"),
        ],
    )
    def test_matches_codebase(self, tables, filing_status, schedule_type):
        extracted = {
            (t.filing_status, t.schedule_type): t.rows for t in tables
        }
        tool_rows = extracted[(filing_status, schedule_type)]
        codebase_rows = CODEBASE_TABLES[(filing_status, schedule_type)][2025]

        assert len(tool_rows) == len(codebase_rows)
        for i, (ext, cb) in enumerate(zip(tool_rows, codebase_rows)):
            assert ext.min == cb.min, f"row {i} min: {ext.min} != {cb.min}"
            assert ext.max == cb.max, f"row {i} max: {ext.max} != {cb.max}"
            assert ext.withhold_amount == cb.withhold_amount, (
                f"row {i} withhold: {ext.withhold_amount} != {cb.withhold_amount}"
            )
            assert ext.percent == cb.percent, (
                f"row {i} percent: {ext.percent} != {cb.percent}"
            )


@pytest.mark.skipif(
    not _pdf_available(2026),
    reason="2026 PDF not available at /tmp/claude/p15t.pdf",
)
class TestExtraction2026:
    """Verify tool extraction matches every value in the 2026 codebase data."""

    @pytest.fixture(scope="class")
    def tables(self):
        return _extract_tables(2026)

    def test_table_count(self, tables):
        assert len(tables) == 6

    def test_validates(self, tables):
        tool.validate_tables(tables, 2026)

    @pytest.mark.parametrize(
        "filing_status,schedule_type",
        [
            ("single", "standard"),
            ("single", "multiple_jobs"),
            ("married", "standard"),
            ("married", "multiple_jobs"),
            ("hoh", "standard"),
            ("hoh", "multiple_jobs"),
        ],
    )
    def test_matches_codebase(self, tables, filing_status, schedule_type):
        extracted = {
            (t.filing_status, t.schedule_type): t.rows for t in tables
        }
        tool_rows = extracted[(filing_status, schedule_type)]
        codebase_rows = CODEBASE_TABLES[(filing_status, schedule_type)][2026]

        assert len(tool_rows) == len(codebase_rows)
        for i, (ext, cb) in enumerate(zip(tool_rows, codebase_rows)):
            assert ext.min == cb.min, f"row {i} min: {ext.min} != {cb.min}"
            assert ext.max == cb.max, f"row {i} max: {ext.max} != {cb.max}"
            assert ext.withhold_amount == cb.withhold_amount, (
                f"row {i} withhold: {ext.withhold_amount} != {cb.withhold_amount}"
            )
            assert ext.percent == cb.percent, (
                f"row {i} percent: {ext.percent} != {cb.percent}"
            )
