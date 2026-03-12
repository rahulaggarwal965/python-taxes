"""Update California state income tax withholding tables from EDD Publication DE 44.

Downloads the Method B PDF, extracts Tables 1-5+ (annual values), validates
the data, and updates the source files.

Usage:
    uv run --group tools python tools/state/ca/update_ca_tax_year.py [OPTIONS]

Options:
    --year YEAR       Tax year to add (auto-detected from PDF if omitted)
    --pdf PATH_OR_URL Path to local PDF or URL (default: EDD website)
    --dry-run         Print extracted data without modifying files
"""

import argparse
import re
import sys
import tempfile
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

MAX = Decimal("999999999999.99")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TABLES_DIR = REPO_ROOT / "src/python_taxes/state/ca/income/tables"
SINGLE_PATH = TABLES_DIR / "single.py"
MARRIED_PATH = TABLES_DIR / "married.py"
HOH_PATH = TABLES_DIR / "hoh.py"
TABLES_INIT_PATH = TABLES_DIR / "__init__.py"
CA_INIT_PATH = REPO_ROOT / "src/python_taxes/state/ca/__init__.py"

EDD_PDF_URL_TEMPLATE = (
    "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/{yy}methb.pdf"
)

YEAR_RE = re.compile(r"California Withholding Schedules for (\d{4})")
TOKEN_RE = re.compile(r"\$[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?%")
DOLLAR_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")

FILING_STATUSES = [
    ("single", re.compile(r"Single.*Persons|SINGLE.*PERSONS|Single.*Dual")),
    ("married", re.compile(r"(?<!Un)Married Persons|(?<!UN)MARRIED PERSONS")),
    ("hoh", re.compile(r"Unmarried.*Head|UNMARRIED.*HEAD")),
]


class CARateRow(NamedTuple):
    min: Decimal
    max: Decimal
    withhold_amount: Decimal
    percent: Decimal


class LookupTables(NamedTuple):
    """Annual lookup values extracted from Tables 1-4."""
    low_income_single: Decimal      # Table 1: single / married 0-1
    low_income_high: Decimal        # Table 1: married 2+ / hoh
    standard_deduction_single: Decimal  # Table 3: single / married 0-1
    standard_deduction_high: Decimal    # Table 3: married 2+ / hoh
    exemption_credit: Decimal       # Table 4: per-allowance annual credit
    estimated_deduction: Decimal    # Table 2: per-allowance annual deduction


class TableData(NamedTuple):
    filing_status: str
    rows: list[CARateRow]


# ---------------------------------------------------------------------------
# PDF acquisition
# ---------------------------------------------------------------------------


def acquire_pdf(source: str) -> str:
    """Download PDF from URL or verify local path. Returns local file path."""
    if source.startswith(("http://", "https://")):
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        print(f"Downloading PDF from {source} ...")
        urllib.request.urlretrieve(source, tmp.name)
        print(f"Saved to {tmp.name}")
        return tmp.name
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {source}")
    return str(path)


def detect_year(pdf) -> int:
    """Detect the tax year from the PDF title."""
    for page in pdf.pages:
        text = page.extract_text() or ""
        match = YEAR_RE.search(text)
        if match:
            return int(match.group(1))
    raise ValueError("Could not detect tax year from PDF title.")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_dollar(token: str) -> Decimal:
    """Parse '$19,300' or '$2,480.00' into Decimal."""
    return Decimal(token.replace("$", "").replace(",", ""))


def parse_percent(token: str) -> Decimal:
    """Parse '10.230%' into Decimal('10.23')."""
    return Decimal(token.replace("%", ""))


# ---------------------------------------------------------------------------
# Table 1-4 extraction (lookup values)
# ---------------------------------------------------------------------------


def extract_lookup_tables(pdf) -> LookupTables:
    """Extract annual values from Tables 1-4."""
    low_income = None
    std_deduction = None
    exemption_credit = None
    estimated_deduction = None

    for page in pdf.pages:
        text = page.extract_text() or ""
        lines = text.split("\n")

        for j, line in enumerate(lines):
            upper = line.upper()

            # Table 1 & 3: look for ANNUAL row with 4 dollar values
            if "ANNUAL" in upper and "$" in line and "DAILY" not in upper and "SEMI" not in upper:
                dollars = DOLLAR_RE.findall(line)
                context = " ".join(lines[max(0, j - 15):j]).upper()

                if "LOW INCOME EXEMPTION" in context and len(dollars) >= 4:
                    low_income = (
                        Decimal(dollars[0].replace(",", "")),
                        Decimal(dollars[2].replace(",", "")),
                    )
                elif "STANDARD DEDUCTION TABLE" in context and len(dollars) >= 4:
                    std_deduction = (
                        Decimal(dollars[0].replace(",", "")),
                        Decimal(dollars[2].replace(",", "")),
                    )

            # Table 4: row starting with "1 " in exemption allowance context
            if "EXEMPTION ALLOWANCE" in " ".join(lines[max(0, j - 20):j]).upper():
                if re.match(r"^1\s", line.strip()):
                    dollars = DOLLAR_RE.findall(line)
                    if len(dollars) >= 7:
                        # Column order: weekly, biweekly, semi-monthly, monthly, quarterly, semi-annual, annual, daily
                        exemption_credit = Decimal(dollars[6].replace(",", ""))

            # Table 2: row starting with "1 " in estimated deduction context
            if "ESTIMATED DEDUCTION" in " ".join(lines[max(0, j - 20):j]).upper():
                if re.match(r"^1\s", line.strip()):
                    dollars = DOLLAR_RE.findall(line)
                    if len(dollars) >= 7:
                        estimated_deduction = Decimal(dollars[6].replace(",", ""))

    if any(v is None for v in [low_income, std_deduction, exemption_credit, estimated_deduction]):
        missing = []
        if low_income is None:
            missing.append("low_income_exemption (Table 1)")
        if std_deduction is None:
            missing.append("standard_deduction (Table 3)")
        if exemption_credit is None:
            missing.append("exemption_credit (Table 4)")
        if estimated_deduction is None:
            missing.append("estimated_deduction (Table 2)")
        raise ValueError(f"Could not extract: {', '.join(missing)}")

    return LookupTables(
        low_income_single=low_income[0],
        low_income_high=low_income[1],
        standard_deduction_single=std_deduction[0],
        standard_deduction_high=std_deduction[1],
        exemption_credit=exemption_credit,
        estimated_deduction=estimated_deduction,
    )


# ---------------------------------------------------------------------------
# Table 5+ extraction (annual tax rate brackets)
# ---------------------------------------------------------------------------


def extract_brackets(pdf) -> list[TableData]:
    """Extract annual tax rate brackets for all 3 filing statuses."""
    # Find the page with annual payroll period tables
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Annual Payroll Period" in text or "ANNUAL PAYROLL PERIOD" in text:
            return _parse_annual_page(text)

    raise ValueError("Could not find annual tax rate table page.")


def _parse_annual_page(text: str) -> list[TableData]:
    """Parse the annual payroll period page into bracket tables."""
    lines = text.split("\n")
    tables: dict[str, list[CARateRow]] = {}
    current_status = None
    pending_over = None

    for line in lines:
        # Check for filing status header
        for key, pat in FILING_STATUSES:
            if pat.search(line):
                current_status = key
                if key not in tables:
                    tables[key] = []
                pending_over = None
                break

        if not current_status:
            continue

        tokens = TOKEN_RE.findall(line)

        if not tokens:
            continue

        # Handle last row: "and over" on same line (2024/2025 format)
        # Tokens per side: over, of_amount_over, plus (3 dollar values)
        if "and over" in line.lower() and "%" in line:
            pct_tokens = [t for t in tokens if "%" in t]
            dollar_tokens = [t for t in tokens if t.startswith("$")]
            if pct_tokens and len(dollar_tokens) >= 3:
                over = parse_dollar(dollar_tokens[0])
                rate = parse_percent(pct_tokens[0])
                plus = parse_dollar(dollar_tokens[2])
                tables[current_status].append(CARateRow(
                    min=over, max=MAX, withhold_amount=plus, percent=rate,
                ))
                current_status = None
                pending_over = None
            continue

        # Handle last row split across lines (2026 format):
        # Line 1: just the "over" values for left and right
        if len(tokens) == 2 and all(t.startswith("$") for t in tokens):
            pending_over = tokens
            continue

        # Line 2: "N/A rate% amount plus" for left and right
        if pending_over and ("N/A" in line or "%" in line):
            pct_tokens = [t for t in tokens if "%" in t]
            dollar_tokens = [t for t in tokens if t.startswith("$")]
            if pct_tokens and len(dollar_tokens) >= 2:
                over = parse_dollar(pending_over[0])
                rate = parse_percent(pct_tokens[0])
                plus = parse_dollar(dollar_tokens[1])
                tables[current_status].append(CARateRow(
                    min=over, max=MAX, withhold_amount=plus, percent=rate,
                ))
                current_status = None
            pending_over = None
            continue

        # Normal row: 10 tokens (5 left annual + 5 right daily)
        if len(tokens) == 10:
            left = tokens[:5]
            over = parse_dollar(left[0])
            not_over = parse_dollar(left[1])
            rate = parse_percent(left[2])
            plus = parse_dollar(left[4])
            tables[current_status].append(CARateRow(
                min=over, max=not_over, withhold_amount=plus, percent=rate,
            ))
            pending_over = None

    result = []
    for status in ["single", "married", "hoh"]:
        if status not in tables:
            raise ValueError(f"Missing filing status: {status}")
        result.append(TableData(status, tables[status]))

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_brackets(tables: list[TableData], year: int) -> None:
    """Validate extracted bracket tables."""
    if len(tables) != 3:
        raise ValueError(f"Expected 3 tables, got {len(tables)}")

    row_counts = {t.filing_status: len(t.rows) for t in tables}
    unique_counts = set(row_counts.values())
    if len(unique_counts) != 1:
        raise ValueError(f"Tables have different row counts: {row_counts}")

    pct_sequences = {
        t.filing_status: tuple(r.percent for r in t.rows) for t in tables
    }
    unique_pcts = set(pct_sequences.values())
    if len(unique_pcts) != 1:
        raise ValueError(f"Tables have different percentage sequences: {pct_sequences}")

    num_rows = unique_counts.pop()
    pct_seq = list(unique_pcts.pop())
    print(f"  {num_rows} brackets, percentages: {pct_seq}")

    for table in tables:
        label = table.filing_status
        rows = table.rows

        if rows[0].min != Decimal("0"):
            raise ValueError(f"{label}: first row min should be 0")

        for i in range(1, len(rows)):
            if rows[i].percent <= rows[i - 1].percent:
                raise ValueError(
                    f"{label}: percentages not increasing at row {i}: "
                    f"{rows[i-1].percent} -> {rows[i].percent}"
                )

        # Bracket continuity: row[i].min == row[i-1].max
        for i in range(1, len(rows)):
            if rows[i].min != rows[i - 1].max:
                raise ValueError(
                    f"{label}: row {i} min={rows[i].min} != prev max={rows[i-1].max}"
                )

        if rows[-1].max != MAX:
            raise ValueError(f"{label}: last row max should be MAX")

    print(f"All 3 tables validated for year {year}.")


def validate_lookup(lookup: LookupTables, year: int) -> None:
    """Validate lookup tables."""
    if lookup.low_income_single <= 0:
        raise ValueError("Low income single threshold must be positive")
    if lookup.low_income_high <= lookup.low_income_single:
        raise ValueError("Low income high threshold must exceed single")
    if lookup.standard_deduction_single <= 0:
        raise ValueError("Standard deduction must be positive")
    if lookup.exemption_credit <= 0:
        raise ValueError("Exemption credit must be positive")
    if lookup.estimated_deduction <= 0:
        raise ValueError("Estimated deduction must be positive")
    print(f"Lookup tables validated for year {year}.")


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """Format Decimal with exactly 2 decimal places."""
    return str(value.quantize(Decimal("0.01")))


def generate_bracket_block(rows: list[CARateRow], year: int) -> str:
    """Generate Python source for a year's CARateRow list entry."""
    lines = [f"    {year}: ["]
    for row in rows:
        min_str = str(int(row.min)) if row.min == int(row.min) else format_decimal(row.min)
        if row.max == MAX:
            max_expr = "MAX"
        else:
            max_expr = f'Decimal("{int(row.max)}")'
        plus_str = format_decimal(row.withhold_amount)
        pct_str = str(row.percent.normalize())

        lines.append("        CARateRow(")
        lines.append(f'            min=Decimal("{min_str}"),')
        lines.append(f"            max={max_expr},")
        lines.append(f'            withhold_amount=Decimal("{plus_str}"),')
        lines.append(f'            percent=Decimal("{pct_str}"),')
        lines.append("        ),")
    lines.append("    ],")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File modification
# ---------------------------------------------------------------------------


def update_bracket_file(
    file_path: Path, rows: list[CARateRow], year: int, dry_run: bool
) -> None:
    """Insert a new year's bracket data into a table file."""
    content = file_path.read_text()

    if f"    {year}: [" in content:
        print(f"  {file_path.name}: year {year} already exists, skipping.")
        return

    code = generate_bracket_block(rows, year)

    # Find closing "}" of the schedule dict
    pattern = re.compile(r"(schedule\s*=\s*\{.*?)(^\})", re.MULTILINE | re.DOTALL)
    match = pattern.search(content)
    if not match:
        raise ValueError(f"Could not find schedule closing brace in {file_path.name}")
    content = content[: match.start(2)] + code + "\n" + content[match.start(2):]

    if dry_run:
        print(f"  {file_path.name}: would insert schedule[{year}]")
    else:
        file_path.write_text(content)
        print(f"  {file_path.name}: updated.")


def update_lookup_tables(lookup: LookupTables, year: int, dry_run: bool) -> None:
    """Add the new year's lookup values to tables/__init__.py."""
    content = TABLES_INIT_PATH.read_text()

    if f"    {year}:" in content:
        print(f"  tables/__init__.py: year {year} already exists, skipping.")
        return

    # Insert into each dict
    dicts_to_update = [
        (
            "low_income_exemption",
            f'    {year}: (Decimal("{int(lookup.low_income_single)}"), Decimal("{int(lookup.low_income_high)}")),\n',
        ),
        (
            "standard_deduction",
            f'    {year}: (Decimal("{int(lookup.standard_deduction_single)}"), Decimal("{int(lookup.standard_deduction_high)}")),\n',
        ),
        (
            "exemption_allowance_credit",
            f'    {year}: Decimal("{format_decimal(lookup.exemption_credit)}"),\n',
        ),
        (
            "estimated_deduction_allowance",
            f'    {year}: Decimal("{int(lookup.estimated_deduction)}"),\n',
        ),
    ]

    for dict_name, new_entry in dicts_to_update:
        pattern = re.compile(
            rf"({dict_name}\s*=\s*\{{.*?)(^\}})", re.MULTILINE | re.DOTALL
        )
        match = pattern.search(content)
        if not match:
            raise ValueError(f"Could not find {dict_name} closing brace")
        content = content[: match.start(2)] + new_entry + content[match.start(2):]

    if dry_run:
        print(f"  tables/__init__.py: would add year {year}")
    else:
        TABLES_INIT_PATH.write_text(content)
        print(f"  tables/__init__.py: updated.")


def update_valid_ca_tax_years(year: int, dry_run: bool) -> None:
    """Add year to is_valid_ca_tax_year."""
    content = CA_INIT_PATH.read_text()

    list_pattern = re.compile(r"(if value in \[)([\d, ]+)(\]:)")
    match = list_pattern.search(content)
    if not match:
        raise ValueError("Could not find year list in is_valid_ca_tax_year")

    if str(year) in match.group(2).split(", "):
        print(f"  ca/__init__.py: year {year} already present, skipping.")
        return

    new_years = match.group(2) + f", {year}"
    content = content[: match.start(2)] + new_years + content[match.end(2):]

    msg_pattern = re.compile(r"(Valid CA tax years are )([\d, ]+),\s+and\s+(\d+)\.")
    msg_match = msg_pattern.search(content)
    if msg_match:
        old_last = msg_match.group(3)
        prefix_years = msg_match.group(2) + ", " + old_last
        new_msg = f"{msg_match.group(1)}{prefix_years}, and {year}."
        content = content[: msg_match.start()] + new_msg + content[msg_match.end():]

    if dry_run:
        print(f"  ca/__init__.py: would add {year}")
    else:
        CA_INIT_PATH.write_text(content)
        print(f"  ca/__init__.py: added {year}")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_tables(tables: list[TableData], lookup: LookupTables) -> None:
    """Pretty-print extracted data."""
    print(f"\n  Low income exemption: single={lookup.low_income_single}, high={lookup.low_income_high}")
    print(f"  Standard deduction: single={lookup.standard_deduction_single}, high={lookup.standard_deduction_high}")
    print(f"  Exemption credit (per allowance): {lookup.exemption_credit}")
    print(f"  Estimated deduction (per allowance): {lookup.estimated_deduction}")

    for table in tables:
        print(f"\n  {table.filing_status}:")
        for row in table.rows:
            max_str = "MAX" if row.max == MAX else f"${row.max:>12}"
            print(
                f"    over=${row.min:>12}  not_over={max_str:>13}  "
                f"rate={row.percent:>5}%  plus=${row.withhold_amount:>12}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update CA tax year tables from EDD Publication DE 44"
    )
    parser.add_argument(
        "--year", type=int, default=None,
        help="Tax year to add (auto-detected from PDF if omitted)",
    )
    parser.add_argument(
        "--pdf", type=str, default=None,
        help="Path to local PDF or URL to download",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print extracted data without modifying files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print(
            "Error: pdfplumber is required. Install with:\n  uv sync --group tools",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine PDF source
    if args.pdf:
        source = args.pdf
    elif args.year:
        yy = str(args.year)[-2:]
        source = EDD_PDF_URL_TEMPLATE.format(yy=yy)
    else:
        source = EDD_PDF_URL_TEMPLATE.format(yy="26")

    pdf_path = acquire_pdf(source)

    with pdfplumber.open(pdf_path) as pdf:
        year = args.year or detect_year(pdf)
        print(f"Tax year: {year}")

        # Extract all data
        lookup = extract_lookup_tables(pdf)
        tables = extract_brackets(pdf)

    # Validate
    validate_lookup(lookup, year)
    validate_brackets(tables, year)

    if args.dry_run:
        print("\n--- Extracted data (dry run) ---")
        print_tables(tables, lookup)
        print()

    # Update files
    print("\nUpdating files:")
    file_map = {"single": SINGLE_PATH, "married": MARRIED_PATH, "hoh": HOH_PATH}
    for table in tables:
        update_bracket_file(file_map[table.filing_status], table.rows, year, args.dry_run)

    update_lookup_tables(lookup, year, args.dry_run)
    update_valid_ca_tax_years(year, args.dry_run)

    if not args.dry_run:
        print(f"\nDone! CA tax year {year} has been added.")
        print("Next steps:")
        print("  1. Review changes: git diff")
        print("  2. Run tests: uv run --group test python -m pytest tests/state/")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
