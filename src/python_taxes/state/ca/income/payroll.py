from decimal import Decimal
from typing import Annotated, Literal

from pydantic import StrictBool, validate_call

from python_taxes import CURRENT_TAX_YEAR, currency_field
from python_taxes.federal import rounding

from .tables import (
    estimated_deduction_allowance,
    exemption_allowance_credit,
    low_income_exemption,
    standard_deduction,
)
from .tables import hoh as hoh_tables
from .tables import married as married_tables
from .tables import single as single_tables

PAY_FREQUENCY = {
    "semiannual": 2,
    "quarterly": 4,
    "monthly": 12,
    "semimonthly": 24,
    "biweekly": 26,
    "weekly": 52,
    "daily": 260,
}

SCHEDULES = {
    "single": single_tables.schedule,
    "married": married_tables.schedule,
    "hoh": hoh_tables.schedule,
}


@validate_call
def employer_withholding(
    taxable_wages: Annotated[Decimal, currency_field],
    pay_frequency: Annotated[
        str,
        Literal[
            "semiannual",
            "quarterly",
            "monthly",
            "semimonthly",
            "biweekly",
            "weekly",
            "daily",
        ],
    ] = "biweekly",
    filing_status: Annotated[
        str, Literal["single", "married", "hoh"]
    ] = "single",
    allowances: int = 0,
    additional_allowances: int = 0,
    extra_withholding: Annotated[Decimal, currency_field] = Decimal("0.00"),
    tax_year: int = CURRENT_TAX_YEAR,
    rounded: StrictBool = False,
) -> Decimal:
    """Calculate California state income tax withholding (Method B).

    Parameters:
    taxable_wages -- Gross wages this pay period
    pay_frequency -- Payroll period (default 'biweekly')
    filing_status -- 'single', 'married', or 'hoh' (default 'single')
    allowances -- Regular withholding allowances on DE 4 / W-4 (default 1)
    additional_allowances -- Estimated deduction allowances on DE 4 (default 0)
    extra_withholding -- Extra amount to withhold each pay period (default 0)
    tax_year -- Tax year (default CURRENT_TAX_YEAR)
    rounded -- Round to nearest whole dollar (default False)
    """
    pay_periods = PAY_FREQUENCY[pay_frequency]

    # Determine which lookup column: "high" for married 2+ or hoh, "low" otherwise
    use_high = (filing_status == "married" and allowances >= 2) or filing_status == "hoh"
    lookup_idx = 1 if use_high else 0

    # Step 1: Low income exemption
    annual_wages = taxable_wages * pay_periods
    threshold = low_income_exemption[tax_year][lookup_idx]
    if annual_wages <= threshold:
        return Decimal("0.00")

    # Step 2: Subtract estimated deductions
    if additional_allowances > 0:
        annual_wages -= additional_allowances * estimated_deduction_allowance[tax_year]

    # Step 3: Subtract standard deduction
    taxable_income = annual_wages - standard_deduction[tax_year][lookup_idx]
    if taxable_income <= 0:
        return Decimal("0.00")

    # Step 4: Bracket lookup and tax computation
    # CA convention: "over X but not over Y" → income > min and income <= max
    brackets = SCHEDULES[filing_status][tax_year]
    computed_tax = Decimal("0.00")
    for row in brackets:
        if taxable_income > row.min and taxable_income <= row.max:
            computed_tax = (
                (taxable_income - row.min) * (row.percent / Decimal("100"))
                + row.withhold_amount
            )
            break
    else:
        # Income exceeds all brackets — use last bracket
        last = brackets[-1]
        computed_tax = (
            (taxable_income - last.min) * (last.percent / Decimal("100"))
            + last.withhold_amount
        )

    # Step 5: Subtract exemption allowance credit
    computed_tax -= allowances * exemption_allowance_credit[tax_year]

    # De-annualize
    withheld_this_period = computed_tax / pay_periods

    # Add extra withholding
    if extra_withholding:
        withheld_this_period += extra_withholding

    return (
        withheld_this_period.quantize(rounding[rounded])
        if withheld_this_period > 0
        else Decimal("0.00")
    )
