def is_valid_ca_tax_year(value: int) -> int:
    if value in [2023, 2024, 2025, 2026, 2027]:
        return value
    raise ValueError(
        "Invalid CA tax year. Valid CA tax years are 2023, 2024, 2025, 2026, and 2027."
    )
