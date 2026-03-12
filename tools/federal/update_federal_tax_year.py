"""Update federal income tax withholding tables from IRS Publication 15-T.

Downloads the PDF, extracts the Percentage Method Tables for Automated Payroll
Systems, validates the data, and updates the source files. Also fetches the
Social Security wage base from the Federal Register.

Usage:
    uv run --group tools python tools/federal/update_federal_tax_year.py [OPTIONS]

Options:
    --year YEAR       Tax year to add (auto-detected from PDF if omitted)
    --pdf PATH_OR_URL Path to local PDF or URL (default: IRS website)
    --dry-run         Print extracted data without modifying files
"""

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

MAX = Decimal("999999999999.99")

IRS_PDF_URL = "https://www.irs.gov/pub/irs-pdf/p15t.pdf"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SINGLE_PATH = (
    REPO_ROOT
    / "src/python_taxes/federal/income/tables/percentage/automated/single.py"
)
MARRIED_PATH = (
    REPO_ROOT
    / "src/python_taxes/federal/income/tables/percentage/automated/married.py"
)
HOH_PATH = (
    REPO_ROOT
    / "src/python_taxes/federal/income/tables/percentage/automated/hoh.py"
)
SOCIAL_SECURITY_PATH = REPO_ROOT / "src/python_taxes/federal/social_security.py"
INIT_PATH = REPO_ROOT / "src/python_taxes/__init__.py"
FEDERAL_INIT_PATH = REPO_ROOT / "src/python_taxes/federal/__init__.py"

# Federal Register API for Social Security wage base
FEDERAL_REGISTER_SEARCH_URL = (
    "https://www.federalregister.gov/api/v1/documents.json"
    "?conditions[term]=%22cost-of-living+increase+and+other+determinations%22"
    "&conditions[agencies][]=social-security-administration"
    "&per_page=10&order=newest"
)
WAGE_BASE_RE = re.compile(
    r"OASDI contribution and benefit base is \$([\d,]+)"
)

# Regex to find the target page by title
PAGE_TITLE_RE = re.compile(
    r"(\d{4})\s+Percentage Method Tables for Automated Payroll Systems"
)

# Regex to extract monetary values ($X,XXX.XX) and percentages (XX%)
TOKEN_RE = re.compile(r"\$[\d,]+(?:\.\d+)?|\d+%")

# Filing status patterns and their order on the page
FILING_STATUSES = [
    ("married", re.compile(r"Married Filing Jointly")),
    ("single", re.compile(r"Single or Married Filing Separately")),
    ("hoh", re.compile(r"Head of Household")),
]


class BracketRow(NamedTuple):
    min: Decimal
    max: Decimal
    withhold_amount: Decimal
    percent: int


class TableData(NamedTuple):
    filing_status: str
    schedule_type: str
    rows: list[BracketRow]


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


# ---------------------------------------------------------------------------
# Page discovery
# ---------------------------------------------------------------------------


def find_target_page(pdf) -> tuple:
    """Find the page with the Percentage Method Tables.

    Returns (page_object, detected_year).
    """
    for page in pdf.pages:
        text = page.extract_text() or ""
        match = PAGE_TITLE_RE.search(text)
        if match:
            year = int(match.group(1))
            return page, year
    raise ValueError(
        "Could not find 'Percentage Method Tables for Automated Payroll "
        "Systems' in any page of the PDF."
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_dollar(token: str) -> Decimal:
    """Parse a dollar token like '$19,300' or '$2,480.00' into a Decimal."""
    cleaned = token.replace("$", "").replace(",", "")
    return Decimal(cleaned)


def parse_percent(token: str) -> int:
    """Parse a percentage token like '10%' into an integer."""
    return int(token.replace("%", ""))


def parse_data_line(tokens: list[str]) -> tuple[BracketRow, BracketRow]:
    """Parse a line's tokens into left (standard) and right (multiple_jobs) rows.

    Normal rows have 10 tokens: A B C D% E | A B C D% E
    Last rows have 8 tokens:    A C D% E   | A C D% E
    """
    n = len(tokens)
    if n == 10:
        left_tokens = tokens[:5]
        right_tokens = tokens[5:]
    elif n == 8:
        left_tokens = tokens[:4]
        right_tokens = tokens[4:]
    else:
        raise ValueError(f"Expected 8 or 10 tokens per data line, got {n}: {tokens}")

    return _parse_half(left_tokens), _parse_half(right_tokens)


def _parse_half(tokens: list[str]) -> BracketRow:
    """Parse 4 or 5 tokens for one side of a data line into a BracketRow.

    5 tokens: A, B, C, D%, E  (normal row)
    4 tokens: A, C, D%, E     (last row, no upper bound)
    """
    if len(tokens) == 5:
        a, b, c, d, _e = tokens
        min_val = parse_dollar(a)
        max_val = parse_dollar(b) - Decimal("0.01")
        withhold = parse_dollar(c)
        pct = parse_percent(d)
    elif len(tokens) == 4:
        a, c, d, _e = tokens
        min_val = parse_dollar(a)
        max_val = MAX
        withhold = parse_dollar(c)
        pct = parse_percent(d)
    else:
        raise ValueError(f"Expected 4 or 5 tokens, got {len(tokens)}: {tokens}")

    # Ensure withhold_amount always has 2 decimal places
    withhold = withhold.quantize(Decimal("0.01"))

    return BracketRow(
        min=min_val,
        max=max_val,
        withhold_amount=withhold,
        percent=pct,
    )


def extract_tables(page) -> list[TableData]:
    """Extract all 6 tax tables from the target page using text parsing.

    The page text has filing status headers followed by data lines for each
    filing status. Each data line contains both left (standard) and right
    (multiple_jobs) values as interleaved tokens. The last row of each table
    is detected by having 8 tokens (no upper bound) instead of 10.
    """
    text = page.extract_text()
    lines = text.split("\n")

    tables: dict[str, dict[str, list[BracketRow]]] = {}
    current_status = None

    for line in lines:
        # Check for filing status header
        for status_key, pattern in FILING_STATUSES:
            if pattern.search(line):
                current_status = status_key
                if status_key not in tables:
                    tables[status_key] = {"standard": [], "multiple_jobs": []}
                break

        # Check for data line (starts with $)
        if current_status and line.strip().startswith("$"):
            tokens = TOKEN_RE.findall(line)
            if not tokens:
                continue

            left_row, right_row = parse_data_line(tokens)
            tables[current_status]["standard"].append(left_row)
            tables[current_status]["multiple_jobs"].append(right_row)

            # Last row of a table has no upper bound (8 tokens instead of 10)
            if len(tokens) == 8:
                current_status = None

    # Build flat list of TableData
    result = []
    for status_key in ["married", "single", "hoh"]:
        if status_key not in tables:
            raise ValueError(f"Missing filing status: {status_key}")
        for schedule_type in ["standard", "multiple_jobs"]:
            rows = tables[status_key][schedule_type]
            result.append(TableData(status_key, schedule_type, rows))

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_tables(tables: list[TableData], year: int) -> None:
    """Validate all 6 extracted tables against structural invariants.

    Does NOT hardcode the number of brackets or specific percentages,
    so the tool remains correct if Congress changes the tax structure.
    """
    if len(tables) != 6:
        raise ValueError(f"Expected 6 tables, got {len(tables)}")

    expected = {
        ("married", "standard"),
        ("married", "multiple_jobs"),
        ("single", "standard"),
        ("single", "multiple_jobs"),
        ("hoh", "standard"),
        ("hoh", "multiple_jobs"),
    }
    actual = {(t.filing_status, t.schedule_type) for t in tables}
    if actual != expected:
        raise ValueError(f"Missing table combinations: {expected - actual}")

    # All 6 tables must have the same number of rows
    row_counts = {f"{t.filing_status}/{t.schedule_type}": len(t.rows) for t in tables}
    unique_counts = set(row_counts.values())
    if len(unique_counts) != 1:
        raise ValueError(f"Tables have different row counts: {row_counts}")

    # All 6 tables must have the same percentage sequence
    pct_sequences = {
        f"{t.filing_status}/{t.schedule_type}": tuple(r.percent for r in t.rows)
        for t in tables
    }
    unique_pcts = set(pct_sequences.values())
    if len(unique_pcts) != 1:
        raise ValueError(f"Tables have different percentage sequences: {pct_sequences}")

    num_rows = unique_counts.pop()
    pct_sequence = list(unique_pcts.pop())
    print(f"  {num_rows} brackets, percentages: {pct_sequence}")

    for table in tables:
        label = f"{table.filing_status}/{table.schedule_type}"
        rows = table.rows

        if len(rows) < 2:
            raise ValueError(f"{label}: need at least 2 rows, got {len(rows)}")

        # First row: starts at 0, no base withholding, 0% rate
        if rows[0].min != Decimal("0.00"):
            raise ValueError(f"{label}: first row min should be 0.00")
        if rows[0].withhold_amount != Decimal("0.00"):
            raise ValueError(f"{label}: first row withhold should be 0.00")
        if rows[0].percent != 0:
            raise ValueError(f"{label}: first row percent should be 0")

        # Percentages must be strictly increasing
        for i in range(1, len(rows)):
            if rows[i].percent <= rows[i - 1].percent:
                raise ValueError(
                    f"{label}: percentages not increasing at row {i}: "
                    f"{rows[i-1].percent} -> {rows[i].percent}"
                )

        # Bracket continuity: each row's min == prev row's max + 0.01
        for i in range(1, len(rows)):
            if rows[i - 1].max == MAX:
                raise ValueError(f"{label}: row {i-1} has MAX but is not last")
            expected_min = rows[i - 1].max + Decimal("0.01")
            if rows[i].min != expected_min:
                raise ValueError(
                    f"{label}: row {i} min={rows[i].min} != expected "
                    f"{expected_min} (prev max={rows[i-1].max})"
                )

        # Last row must have no upper bound
        if rows[-1].max != MAX:
            raise ValueError(f"{label}: last row max should be MAX")

    print(f"All 6 tables validated for year {year}.")


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """Format Decimal with exactly 2 decimal places."""
    return str(value.quantize(Decimal("0.01")))


def generate_year_block(rows: list[BracketRow], year: int) -> str:
    """Generate Python source for a year's RateRow list entry."""
    lines = [f"    {year}: ["]
    for row in rows:
        min_str = format_decimal(row.min)
        if row.max == MAX:
            max_expr = "MAX"
        else:
            max_expr = f'Decimal("{format_decimal(row.max)}")'
        withhold_str = format_decimal(row.withhold_amount)

        lines.append("        RateRow(")
        lines.append(f'            min=Decimal("{min_str}"),')
        lines.append(f"            max={max_expr},")
        lines.append(f'            withhold_amount=Decimal("{withhold_str}"),')
        lines.append(f"            percent={row.percent},")
        lines.append("        ),")
    lines.append("    ],")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File modification
# ---------------------------------------------------------------------------


def update_table_file(
    file_path: Path,
    standard_rows: list[BracketRow],
    multiple_jobs_rows: list[BracketRow],
    year: int,
    dry_run: bool,
) -> None:
    """Insert a new year's data into a table file (single.py, married.py, hoh.py)."""
    content = file_path.read_text()

    if f"    {year}: [" in content:
        print(f"  {file_path.name}: year {year} already exists, skipping.")
        return

    standard_code = generate_year_block(standard_rows, year)
    multiple_code = generate_year_block(multiple_jobs_rows, year)

    # Insert into standard_schedule: find the closing "}" before "multiple_jobs"
    pattern_std = re.compile(
        r"(standard_schedule\s*=\s*\{.*?)(^\})",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern_std.search(content)
    if not match:
        raise ValueError(f"Could not find standard_schedule closing brace in {file_path.name}")
    content = content[: match.start(2)] + standard_code + "\n" + content[match.start(2) :]

    # Insert into multiple_jobs: find the closing "}" at end of file
    pattern_mj = re.compile(
        r"(multiple_jobs\s*=\s*\{.*?)(^\})\s*$",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern_mj.search(content)
    if not match:
        raise ValueError(f"Could not find multiple_jobs closing brace in {file_path.name}")
    content = content[: match.start(2)] + multiple_code + "\n" + content[match.start(2) :]

    if dry_run:
        print(f"  {file_path.name}: would insert standard_schedule[{year}] and multiple_jobs[{year}]")
    else:
        file_path.write_text(content)
        print(f"  {file_path.name}: updated.")


def update_current_tax_year(year: int, dry_run: bool) -> None:
    """Update CURRENT_TAX_YEAR in __init__.py."""
    content = INIT_PATH.read_text()
    pattern = re.compile(r"CURRENT_TAX_YEAR\s*=\s*\d+")
    match = pattern.search(content)
    if not match:
        raise ValueError("Could not find CURRENT_TAX_YEAR in __init__.py")

    new_content = pattern.sub(f"CURRENT_TAX_YEAR = {year}", content)

    if dry_run:
        print(f"  __init__.py: would set CURRENT_TAX_YEAR = {year}")
    else:
        INIT_PATH.write_text(new_content)
        print(f"  __init__.py: set CURRENT_TAX_YEAR = {year}")


def fetch_ss_wage_base(year: int) -> Decimal:
    """Fetch the Social Security wage base for a given year from the Federal Register.

    Searches for the SSA's annual "Cost-of-Living Increase and Other Determinations"
    notice and extracts the OASDI contribution and benefit base.
    """
    print(f"  Fetching SS wage base for {year} from Federal Register ...")

    # Search for the COLA notice for this year
    req = urllib.request.Request(
        FEDERAL_REGISTER_SEARCH_URL,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    # Find the document for our target year
    target_title = f"for {year}"
    doc = None
    for result in data.get("results", []):
        if target_title.lower() in result.get("title", "").lower():
            doc = result
            break

    if doc is None:
        raise ValueError(
            f"Could not find Federal Register COLA notice for {year}. "
            f"Searched {len(data.get('results', []))} results."
        )

    doc_number = doc["document_number"]
    print(f"  Found document: {doc['title']} ({doc_number})")

    # Fetch the document details to get the raw text URL
    detail_url = (
        f"https://www.federalregister.gov/api/v1/documents/{doc_number}.json"
        f"?fields[]=raw_text_url"
    )
    detail_req = urllib.request.Request(
        detail_url, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(detail_req) as resp:
        detail = json.loads(resp.read())

    raw_text_url = detail.get("raw_text_url")
    if not raw_text_url:
        raise ValueError(f"No raw_text_url for document {doc_number}")

    # Fetch and parse the raw text
    with urllib.request.urlopen(raw_text_url) as resp:
        text = resp.read().decode("utf-8")

    match = WAGE_BASE_RE.search(text)
    if not match:
        raise ValueError(
            f"Could not find OASDI contribution and benefit base in document {doc_number}"
        )

    wage_base = Decimal(match.group(1).replace(",", ""))
    print(f"  SS wage base for {year}: ${wage_base}")
    return wage_base


def update_ss_wage_base(year: int, wage_base: Decimal, dry_run: bool) -> None:
    """Add the SS wage base for a year to social_security.py."""
    content = SOCIAL_SECURITY_PATH.read_text()

    if f"    {year}: Decimal(" in content:
        print(f"  social_security.py: year {year} already exists, skipping.")
        return

    # Find the closing "}" of the wage_limit dict
    pattern = re.compile(
        r"(wage_limit\s*=\s*\{.*?)(^\})",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if not match:
        raise ValueError("Could not find wage_limit closing brace in social_security.py")

    new_entry = f'    {year}: Decimal("{int(wage_base)}"),\n'
    content = content[: match.start(2)] + new_entry + content[match.start(2) :]

    if dry_run:
        print(f"  social_security.py: would add wage_limit[{year}] = ${wage_base}")
    else:
        SOCIAL_SECURITY_PATH.write_text(content)
        print(f"  social_security.py: added wage_limit[{year}] = ${wage_base}")


def update_valid_tax_years(year: int, dry_run: bool) -> None:
    """Add year to is_valid_tax_year in federal/__init__.py."""
    content = FEDERAL_INIT_PATH.read_text()

    # Update the year list: "if value in [2023, 2024, 2025, 2026]:"
    list_pattern = re.compile(r"(if value in \[)([\d, ]+)(\]:)")
    list_match = list_pattern.search(content)
    if not list_match:
        raise ValueError("Could not find year list in is_valid_tax_year")

    existing_years = list_match.group(2)
    if str(year) in existing_years.split(", "):
        print(f"  federal/__init__.py: year {year} already present, skipping.")
        return

    new_years = existing_years + f", {year}"
    content = content[: list_match.start(2)] + new_years + content[list_match.end(2) :]

    # Update error message: "Valid tax years are 2023, 2024, 2025, and 2026."
    msg_pattern = re.compile(
        r"(Valid tax years are )([\d, ]+),\s+and\s+(\d+)\."
    )
    msg_match = msg_pattern.search(content)
    if msg_match:
        old_last = msg_match.group(3)
        prefix_years = msg_match.group(2) + ", " + old_last
        new_msg = f"{msg_match.group(1)}{prefix_years}, and {year}."
        content = content[: msg_match.start()] + new_msg + content[msg_match.end() :]

    if dry_run:
        print(f"  federal/__init__.py: would add {year} to is_valid_tax_year")
    else:
        FEDERAL_INIT_PATH.write_text(content)
        print(f"  federal/__init__.py: added {year} to is_valid_tax_year")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_tables(tables: list[TableData]) -> None:
    """Pretty-print extracted tables."""
    for table in tables:
        print(f"\n  {table.filing_status} / {table.schedule_type}:")
        for row in table.rows:
            max_str = "MAX" if row.max == MAX else f"${format_decimal(row.max):>15}"
            print(
                f"    min=${format_decimal(row.min):>12}  "
                f"max={max_str:>16}  "
                f"withhold=${format_decimal(row.withhold_amount):>12}  "
                f"pct={row.percent:>2}%"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update tax year tables from IRS Publication 15-T"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Tax year to add (auto-detected from PDF if omitted)",
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default=IRS_PDF_URL,
        help="Path to local PDF or URL to download",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print extracted data without modifying files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        import pdfplumber
    except ImportError:
        print(
            "Error: pdfplumber is required. Install with:\n"
            "  uv sync --group tools",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 1: Acquire PDF
    pdf_path = acquire_pdf(args.pdf)

    # Step 2: Find target page
    with pdfplumber.open(pdf_path) as pdf:
        page, detected_year = find_target_page(pdf)

        year = args.year or detected_year
        print(f"Tax year: {year}")

        if args.year and args.year != detected_year:
            print(
                f"WARNING: specified year ({args.year}) differs from "
                f"PDF year ({detected_year})"
            )

        # Step 3: Extract tables
        tables = extract_tables(page)

    # Step 4: Validate
    validate_tables(tables, year)

    if args.dry_run:
        print("\n--- Extracted data (dry run) ---")
        print_tables(tables)
        print()

    # Organize by filing status
    by_status: dict[str, dict[str, list[BracketRow]]] = {}
    for t in tables:
        by_status.setdefault(t.filing_status, {})[t.schedule_type] = t.rows

    # Step 5: Update files
    print("\nUpdating files:")
    file_map = {
        "single": SINGLE_PATH,
        "married": MARRIED_PATH,
        "hoh": HOH_PATH,
    }
    for status, path in file_map.items():
        update_table_file(
            path,
            by_status[status]["standard"],
            by_status[status]["multiple_jobs"],
            year,
            args.dry_run,
        )

    # Step 6: Fetch and update Social Security wage base
    print("\nSocial Security wage base:")
    wage_base = fetch_ss_wage_base(year)
    update_ss_wage_base(year, wage_base, args.dry_run)

    update_current_tax_year(year, args.dry_run)
    update_valid_tax_years(year, args.dry_run)

    if not args.dry_run:
        print(f"\nDone! Tax year {year} has been added.")
        print("Next steps:")
        print("  1. Review changes: git diff")
        print("  2. Run tests: uv run --group test python -m pytest tests/")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
