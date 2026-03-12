"""Tests for California state income tax withholding.

Verified against examples from EDD Publication DE 44 (Method B) for 2026.
The annualization approach may differ by ±$0.02 from per-period table
lookups due to rounding in the per-period tables. Both approaches are
officially sanctioned by the EDD.
"""

from decimal import Decimal

import pytest

from python_taxes.state.ca.income import employer_withholding


class TestPDFExamples2026:
    """Verify against the worked examples in the 2026 EDD publication."""

    def test_example_a_low_income(self):
        """Weekly $210, single, 1 allowance → $0 (below low income threshold)."""
        assert employer_withholding(
            taxable_wages=Decimal("210"),
            pay_frequency="weekly",
            filing_status="single",
            allowances=1,
            tax_year=2026,
        ) == Decimal("0.00")

    def test_example_b_biweekly_married(self):
        """Biweekly $1,600, married, 3 allowances (1 estimated) → $2.38."""
        assert employer_withholding(
            taxable_wages=Decimal("1600"),
            pay_frequency="biweekly",
            filing_status="married",
            allowances=2,
            additional_allowances=1,
            tax_year=2026,
        ) == Decimal("2.38")

    def test_example_d_weekly_hoh(self):
        """Weekly $950, HoH, 3 allowances → $1.67 (annualized; PDF per-period gives $1.69)."""
        assert employer_withholding(
            taxable_wages=Decimal("950"),
            pay_frequency="weekly",
            filing_status="hoh",
            allowances=3,
            tax_year=2026,
        ) == Decimal("1.67")

    def test_example_e_semimonthly_married(self):
        """Semi-monthly $2,400, married, 4 allowances → $4.13."""
        assert employer_withholding(
            taxable_wages=Decimal("2400"),
            pay_frequency="semimonthly",
            filing_status="married",
            allowances=4,
            tax_year=2026,
        ) == Decimal("4.13")

    def test_example_f_monthly_married(self):
        """Monthly $4,750 (annual $57,000), married, 4 allowances → $7.17."""
        assert employer_withholding(
            taxable_wages=Decimal("4750"),
            pay_frequency="monthly",
            filing_status="married",
            allowances=4,
            tax_year=2026,
        ) == Decimal("7.17")


class TestEdgeCases:
    def test_zero_wages(self):
        assert employer_withholding(Decimal("0")) == Decimal("0.00")

    def test_negative_wages_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            employer_withholding(Decimal("-100"))

    def test_low_income_single(self):
        """Annual wages just at threshold → $0."""
        # 2026 single annual low income threshold = $18,896
        # Weekly: $18,896 / 52 = $363.38
        assert employer_withholding(
            taxable_wages=Decimal("363"),
            pay_frequency="weekly",
            filing_status="single",
            tax_year=2026,
        ) == Decimal("0.00")

    def test_low_income_married_2_plus(self):
        """Married with 2+ allowances uses higher threshold."""
        # 2026 married 2+ annual threshold = $37,791
        # Biweekly: $37,791 / 26 = $1,453.5
        assert employer_withholding(
            taxable_wages=Decimal("1453"),
            pay_frequency="biweekly",
            filing_status="married",
            allowances=2,
            tax_year=2026,
        ) == Decimal("0.00")

    def test_low_income_married_0_1_lower_threshold(self):
        """Married with 0-1 allowances uses lower (single) threshold."""
        # 2026 single/married_0_1 annual threshold = $18,896
        # Biweekly: $18,896 / 26 = $727.
        assert employer_withholding(
            taxable_wages=Decimal("726"),
            pay_frequency="biweekly",
            filing_status="married",
            allowances=1,
            tax_year=2026,
        ) == Decimal("0.00")

    def test_extra_withholding(self):
        """Extra withholding is added per-period."""
        base = employer_withholding(
            taxable_wages=Decimal("5000"),
            pay_frequency="biweekly",
            filing_status="single",
            tax_year=2026,
        )
        with_extra = employer_withholding(
            taxable_wages=Decimal("5000"),
            pay_frequency="biweekly",
            filing_status="single",
            extra_withholding=Decimal("100"),
            tax_year=2026,
        )
        assert with_extra == base + Decimal("100")

    def test_rounded(self):
        result = employer_withholding(
            taxable_wages=Decimal("2400"),
            pay_frequency="semimonthly",
            filing_status="married",
            allowances=4,
            tax_year=2026,
            rounded=True,
        )
        assert result == Decimal("4")

    def test_all_pay_frequencies(self):
        """Verify all pay frequencies produce non-negative results."""
        for freq in ["weekly", "biweekly", "semimonthly", "monthly",
                      "quarterly", "semiannual", "daily"]:
            result = employer_withholding(
                taxable_wages=Decimal("5000"),
                pay_frequency=freq,
                filing_status="single",
                tax_year=2026,
            )
            assert result >= Decimal("0.00"), f"Failed for {freq}"

    def test_high_income_top_bracket(self):
        """Income in the 14.63% bracket (Mental Health Services Tax)."""
        result = employer_withholding(
            taxable_wages=Decimal("100000"),
            pay_frequency="monthly",
            filing_status="single",
            allowances=0,
            tax_year=2026,
        )
        assert result > Decimal("0.00")
