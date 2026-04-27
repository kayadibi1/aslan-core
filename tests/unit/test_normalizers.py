import pytest

from aslan_core.registry.normalizers import (
    normalize_isin,
    normalize_tax_number,
    normalize_ticker,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234567890", "1234567890"),
        (" 1234567890 ", "1234567890"),
        ("12.345.678-90", "1234567890"),  # punctuation stripped
    ],
)
def test_tax_number_normalization(raw: str, expected: str) -> None:
    assert normalize_tax_number(raw) == expected


def test_tax_number_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        normalize_tax_number("123")


def test_tax_number_rejects_letters() -> None:
    """All non-digits are stripped, so 'abcd1234' → '1234' which is too short."""
    with pytest.raises(ValueError):
        normalize_tax_number("abcd1234")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("asels", "ASELS"),
        ("  ASELS  ", "ASELS"),
        ("ASELS.E", "ASELS.E"),  # market segment suffix preserved
    ],
)
def test_ticker_normalization(raw: str, expected: str) -> None:
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("traasels91h1", "TRAASELS91H1"),
        (" TRAASELS91H1 ", "TRAASELS91H1"),
    ],
)
def test_isin_normalization(raw: str, expected: str) -> None:
    assert normalize_isin(raw) == expected


def test_isin_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        normalize_isin("TRAA")
