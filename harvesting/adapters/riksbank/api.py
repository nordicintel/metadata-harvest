"""Riksbank URLs for harvesting; shared constants live in the retrieval module."""

from retrieval.adapters.riksbank import (
    ACCEPT_JSON,
    API_ROOT,
    MPD_FORECASTS,
    MPD_SERIES_IDS,
    PROVIDER_CODE,
    SWEA_OBSERVATIONS,
    SWEA_ROOT,
    SWESTR_ID,
    SWESTR_ROOT,
)

__all__ = [
    "ACCEPT_JSON",
    "API_ROOT",
    "GROUPS_URL",
    "MPD_FORECASTS",
    "MPD_POLICY_ROUNDS",
    "MPD_SERIES_IDS",
    "PAGES",
    "PROVIDER_CODE",
    "SERIES_URL",
    "SWEA_OBSERVATIONS",
    "SWESTR_DATA_URL",
    "SWESTR_LATEST_URL",
    "latest_by_group_url",
    "series_by_group_url",
]

GROUPS_URL = f"{SWEA_ROOT}/Groups"
SERIES_URL = f"{SWEA_ROOT}/Series"
MPD_POLICY_ROUNDS = f"{MPD_FORECASTS}/policy_rounds"
SWESTR_LATEST_URL = f"{SWESTR_ROOT}/latest/{SWESTR_ID}"
SWESTR_DATA_URL = f"{SWESTR_ROOT}/{SWESTR_ID}"

_PORTAL = "https://developer.api.riksbank.se/api-details#api="
_SITE = "https://www.riksbank.se/en-gb/statistics"
PAGES = {
    "swea": {
        "source_url": f"{_SITE}/interest-rates-and-exchange-rates/"
        "search-interest-rates-and-exchange-rates/",
        "doc_url": f"{_PORTAL}swea-api",
        "guide": f"{_SITE}/interest-rates-and-exchange-rates/"
        "retrieving-interest-rates-and-exchange-rates-via-api/",
        "guide_label": "Retrieving interest rates and exchange rates via API",
    },
    "mpd": {
        "source_url": f"{_SITE}/macro-indicators/forecasts-and-outcomes/",
        "doc_url": f"{_PORTAL}monetary-policy-data",
        "guide": f"{_SITE}/macro-indicators/forecasts-and-outcomes/"
        "retrieving-forecasts-and-outcomes-via-api/",
        "guide_label": "Retrieving forecasts and outcomes via API",
    },
    "swestr": {
        "source_url": f"{_SITE}/swestr/",
        "doc_url": f"{_PORTAL}tora-api",
        "guide": f"{_SITE}/swestr/collecting-swestr-via-api/",
        "guide_label": "Collecting SWESTR via API",
    },
}
"""Human-readable pages per API; every URL answered HTTP 200 on 2026-10-09 (the
portal is a single-page app, so its ``#api=`` fragment is resolved client-side)."""


def series_by_group_url(group_id: int) -> str:
    return f"{SWEA_ROOT}/Series/ByGroup/{group_id}"


def latest_by_group_url(group_id: int) -> str:
    return f"{SWEA_OBSERVATIONS}/Latest/ByGroup/{group_id}"
