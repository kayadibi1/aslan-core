from __future__ import annotations

import re

_TAX_NUM_STRIP = re.compile(r"[^0-9]")


def normalize_tax_number(value: str) -> str:
    """Turkish VKN: 10 digits. Strip whitespace and punctuation."""
    cleaned = _TAX_NUM_STRIP.sub("", value)
    if len(cleaned) != 10:
        raise ValueError(f"tax_number must be 10 digits after normalization, got {len(cleaned)}")
    return cleaned


def normalize_ticker(value: str) -> str:
    """BIST tickers are uppercase; preserve `.<segment>` suffix."""
    return value.strip().upper()


def normalize_isin(value: str) -> str:
    """ISIN: 12 alphanumeric chars, uppercase."""
    cleaned = value.strip().upper()
    if len(cleaned) != 12:
        raise ValueError(f"isin must be 12 chars, got {len(cleaned)}")
    return cleaned
