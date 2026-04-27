from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    COMPANY = "company"
    FUND = "fund"
    INSTRUMENT = "instrument"
    INDEX = "index"
    SOVEREIGN = "sovereign"
    SECTOR = "sector"
    FOUNDER = "founder"
    OTHER = "other"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELISTED = "delisted"
    MERGED = "merged"
    DISSOLVED = "dissolved"


class Namespace(StrEnum):
    BIST_TICKER = "bist_ticker"
    KAP_ENTITY_CODE = "kap_entity_code"
    MKK_ID = "mkk_id"
    TAX_NUMBER = "tax_number"
    MERSIS = "mersis"
    LEI = "lei"
    ISIN = "isin"
    TEFAS_CODE = "tefas_code"
    TEFAS_FOUNDER_CODE = "tefas_founder_code"
    EVDS_SERIES = "evds_series"
    BLOOMBERG_TICKER = "bloomberg_ticker"


class Frequency(StrEnum):
    TICK = "tick"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


class RestatementBasis(StrEnum):
    NOMINAL = "nominal"
    TAS29_REAL = "tas29_real"
    CPI_DEFLATED_2003 = "cpi_deflated_2003"
