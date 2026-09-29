from datetime import date

from scripts.verify_retail_demo_data import normalise_date_value


def test_normalise_date_value_handles_database_date_values():
    assert normalise_date_value(date(2024, 1, 1)) == "2024-01-01"
    assert normalise_date_value("2026-12-31") == "2026-12-31"
