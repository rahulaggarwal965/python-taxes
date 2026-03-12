from decimal import Decimal
from typing import NamedTuple

MAX = Decimal("999999999999.99")


class CARateRow(NamedTuple):
    """California tax withholding rate bracket.

    min: "Over" threshold (income must be > min)
    max: "But not over" threshold (income must be <= max), MAX for last bracket
    withhold_amount: Cumulative "plus" tax from lower brackets
    percent: Tax rate as percentage (e.g. Decimal("1.1"), Decimal("10.23"))
    """

    min: Decimal
    max: Decimal
    withhold_amount: Decimal
    percent: Decimal


# Per-year lookup tables (annual values only).
# Tuples are (single/married_0_1, married_2_plus/hoh).

low_income_exemption = {
    2023: (Decimal("17252"), Decimal("34503")),
    2024: (Decimal("17769"), Decimal("35538")),
    2025: (Decimal("18368"), Decimal("36736")),
    2026: (Decimal("18896"), Decimal("37791")),
}

standard_deduction = {
    2023: (Decimal("5202"), Decimal("10404")),
    2024: (Decimal("5363"), Decimal("10726")),
    2025: (Decimal("5540"), Decimal("11080")),
    2026: (Decimal("5706"), Decimal("11412")),
}

exemption_allowance_credit = {
    2023: Decimal("154.00"),
    2024: Decimal("158.40"),
    2025: Decimal("163.90"),
    2026: Decimal("168.30"),
}

estimated_deduction_allowance = {
    2023: Decimal("1000"),
    2024: Decimal("1000"),
    2025: Decimal("1000"),
    2026: Decimal("1000"),
}
