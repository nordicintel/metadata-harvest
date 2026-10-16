"""Map Riksbank records and probe responses to metadata-only Dataset documents.

All three APIs are English only; labels and notes are the API's own texts, apart
from the adapter labels for the synthetic dimensions and SWESTR measures below.

Time representation (metadata-only, complete dimensions):

- SWEA: every calendar date from the earliest ``observationMinDate`` to the latest
  of ``observationMaxDate`` and the probe's latest observation dates. It is a
  superset of the observation dates (no weekends or holidays are published), so
  retrieval leaves those cells out, and it needs no extra request.
- Forecasts: every period-end date of the series' frequency (monthly, quarterly
  or annual, inferred from the vintage's period spacing) from ``start_date`` to the last period of the
  latest round's vintage, plus any off-grid date that vintage returned.
- SWESTR: every calendar date from the first published fixing (2021-09-01) to the
  latest fixing date.
"""

import calendar
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from apps.harvest.metadata_adapters.riksbank.client import (
    MPD_FORECASTS,
    MPD_SERIES_IDS,
    PAGES,
    PROVIDER_CODE,
    SWEA_OBSERVATIONS,
    SWESTR_DATA_URL,
    SWESTR_LATEST_URL,
    series_by_group_url,
)
from retrieval.data_adapters.riksbank import (
    MEASURE,
    POLICY_ROUND,
    SERIES,
    SWESTR_ID,
    SWESTR_MEASURES,
    TIME,
)

logger = logging.getLogger("riksbank_logger")

MAPPING_VERSION = 1
"""Increment when a mapping change must be distinguishable in accepted state."""

LANGUAGE = "en"
SOURCE = "Sveriges Riksbank"
PER_CENT = "per cent"
KRONA_INDEX = "index, 18 November 1992 = 100"

SWEA_UNITS: dict[int, str] = {
    # Riksbank key interest rates (policy, deposit, lending, liquidity facility and
    # marginal rate): annual interest rates, e.g. policy rate 1.75 on 2026-10-09.
    2: PER_CENT,
    # Reference rate (Interest Act) and historical discount rate: annual rates.
    3: PER_CENT,
    # STIBOR fixings T/N to 12 months: annual interest rates.
    5: PER_CENT,
    # Swedish Treasury bill yields, 1 to 12 months.
    6: PER_CENT,
    # Swedish government bond yields, 2 to 10 years.
    7: PER_CENT,
    # Swedish fixing rates (STFIX), 3 months to 5 years.
    8: PER_CENT,
    # Swedish mortgage bond yields, 2 and 5 years.
    9: PER_CENT,
    # Swedish corporate certificate (CP) rates, 3 and 6 months.
    10: PER_CENT,
    # Euro market rates, 3 months (USD, JPY, GBP, EUR, NOK, DKK).
    97: PER_CENT,
    # Euro market rates, 6 months (USD, JPY, GBP, EUR, NOK, DKK).
    98: PER_CENT,
    # International government bond yields, 5 years.
    99: PER_CENT,
    # International government bond yields, 10 years.
    100: PER_CENT,
    # Swedish TCW index: the group description says the index "equals 100" on
    # 18 November 1992, its starting date.
    12: KRONA_INDEX,
    # Swedish KIX index (SEKKIX92): the forecast API's KIX series SEQKIXNAANA has
    # the unit "Index, 18 Nov 1992 = 100"; 122.05 on 2026-10-09.
    151: KRONA_INDEX,
    # Currencies against Swedish kronor: SEK per ONE unit of the currency, also for
    # small currencies (JPY 0.06297, IDR 0.00056, KRW 0.00743 on 2026-10-09; the
    # Riksbank does not quote these per 100). SEKETT (SEK) is 1.
    130: "SEK per unit of currency",
    # Special drawing rights: the series is described as "SEK/SDR".
    138: "SEK per SDR",
    # Group 155 (SEK/USD forward premiums, e.g. -0.05736) is left out on purpose:
    # neither the API nor the Riksbank pages state its unit.
}
"""Hand-made units by SWEA group: the SWEA API reports no units."""

SWESTR_FIRST_DATE = "2021-09-01"
"""First SWESTR fixing served by ``/swestr/v1/SWESTR`` (checked 2026-10-09; the
preliminary test-period values were removed from the API)."""
SWESTR_MEASURE_TEXT = {
    "rate": ("SWESTR", PER_CENT, "The SWESTR interest rate (fixing)."),
    "pctl12_5": (
        "12.5th percentile",
        PER_CENT,
        "Interest rate at the lower trimming boundary, which corresponds to the "
        "volume-weighted percentile 12.5 of the transaction dataset before trimming.",
    ),
    "pctl87_5": (
        "87.5th percentile",
        PER_CENT,
        "Interest rate at the upper trimming boundary, which corresponds to the "
        "volume-weighted percentile 87.5 of the transaction dataset before trimming.",
    ),
    "volume": (
        "Volume",
        "SEK million",
        "The total transaction volume on which the calculation of SWESTR is based.",
    ),
    "numberOfTransactions": ("Number of transactions", "number", None),
    "numberOfAgents": ("Number of reporting agents", "number", None),
}
"""Adapter labels, units and Riksbank definitions (from
https://www.riksbank.se/en-gb/statistics/swestr/, volume shown in MSEK)."""
SWESTR_STATUS_NOTE = (
    'status "republication" marks a republished fixing and "alternative '
    'calculation: <reason>" a fixing calculated with the alternative method '
    "(reasons per the Riksbank: 1 insufficient number of reporters, 2 insufficient "
    "volume, 3 excessive volume share, 4 technical problems)."
)
SWEA_TIME_NOTE = (
    "Time lists every calendar date from the first to the last observation date "
    "reported for the group's series. The Riksbank publishes observations only on "
    "banking days, so other dates have no data."
)
MPD_NOTE = (
    "Each policy round is one vintage: the outcomes and forecasts published with "
    "that round's monetary policy decision. Rounds without a vintage for this "
    "series (such as 2020:2) have no data."
)
TIME_UNITS = {"Q": "quarterly", "M": "monthly", "A": "annual"}
_LABEL_LIMIT = 80


def calendar_dates(first: str, last: str) -> list[str]:
    """Every ISO date from ``first`` to ``last`` inclusive."""
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    if end < start:
        raise ValueError(f"date range ends before it starts: {first}..{last}")
    return [
        (start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)
    ]


def period_ends(first: str, last: str, frequency: str) -> list[str]:
    """Period-end dates of ``frequency`` (Q, M or A) covering ``first``..``last``."""
    step = {"M": 1, "Q": 3, "A": 12}[frequency]
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    year, month = start.year, start.month
    month += (-month) % step  # the period's last month
    periods = []
    while (year, month) <= (end.year, end.month + (-end.month) % step):
        periods.append(
            date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        )
        month += step
        if month > 12:
            year, month = year + 1, month - 12
    return periods


def infer_frequency(periods: Sequence[str], series_id: str) -> str:
    """``M``, ``Q`` or ``A`` from a vintage's period-end dates.

    The id's third letter is not reliable (``SEDMENAYNA`` has ``D`` but quarterly
    periods), so the observed spacing decides; the letter is used only when a
    vintage has a single period.

    Raises:
        ValueError: The periods are not month ends or have no regular spacing.
    """
    days = [date.fromisoformat(period) for period in periods]
    if any(d.day != calendar.monthrange(d.year, d.month)[1] for d in days):
        raise ValueError(f"{series_id}: periods are not month ends")
    months = [d.year * 12 + d.month for d in days]
    gaps = {b - a for a, b in zip(months, months[1:], strict=False)}
    step = math.gcd(*gaps) if gaps else {"M": 1, "Q": 3, "A": 12}.get(series_id[2:3])
    if not step:
        raise ValueError(f"{series_id}: irregular forecast periods")
    frequency, step = (
        ("A", 12) if step % 12 == 0 else ("Q", 3) if step % 3 == 0 else ("M", 1)
    )
    if days[0].month % step:
        raise ValueError(f"{series_id}: periods do not end quarters or years")
    return frequency


def _link(api: str) -> dict[str, Any]:
    pages = PAGES[api]
    return {
        "describedby": [
            {"href": pages["guide"], "label": pages["guide_label"], "type": "text/html"}
        ]
    }


def _urls(api: str) -> dict[str, str]:
    return {"source_url": PAGES[api]["source_url"], "doc_url": PAGES[api]["doc_url"]}


def _clean(text: Any) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _notes(*texts: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        if text and text not in found:
            found.append(text)
    return found


def _series_category(row: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Label and notes of a SWEA series.

    ``midDescription`` is the label, unless it is a long notice (some STIBOR
    series): then ``shortDescription`` is used and the notice becomes a note.
    """
    short = _clean(row.get("shortDescription"))
    mid = _clean(row.get("midDescription"))
    long = (row.get("longDescription") or "").strip()
    label = mid if mid and len(mid) <= _LABEL_LIMIT else short or mid
    notes = [
        text for text in _notes(mid if label != mid else "", long) if text != label
    ]
    return label, notes


def build_swea_group(record: Mapping[str, Any], latest: Sequence[Any]) -> dict:
    """Build the document of one SWEA group.

    Args:
        record: A ``swea`` record from :func:`source.swea_listing`.
        latest: The ``/Observations/Latest/ByGroup/{groupId}`` response.
    """
    group, rows = record["group"], record["series"]
    group_id = group["groupId"]
    if not isinstance(latest, list):
        raise ValueError(
            f"unexpected latest-observation response {str(latest)[:200]!r}"
        )
    latest_dates = {
        str(item.get("seriesId")): str(item.get("date"))
        for item in latest
        if isinstance(item, Mapping) and item.get("date")
    }
    codes = [row["seriesId"] for row in rows]
    if unknown := sorted(set(latest_dates) - set(codes)):
        raise ValueError(f"latest observations for unlisted series {unknown}")
    first = min(row["observationMinDate"] for row in rows)
    last = max(
        [row["observationMaxDate"] for row in rows]
        + [latest_dates[code] for code in codes if code in latest_dates]
    )

    labels, notes, units, native = {}, {}, {}, {}
    unit = SWEA_UNITS.get(group_id)
    for row in rows:
        code = row["seriesId"]
        labels[code], series_notes = _series_category(row)
        if series_notes:
            notes[code] = series_notes
        if unit:
            units[code] = {"label": unit}
        native[code] = {
            "observationMinDate": row["observationMinDate"],
            "observationMaxDate": row["observationMaxDate"],
            "seriesClosed": row["seriesClosed"],
            "shortDescription": _clean(row.get("shortDescription")),
        }
        if code in latest_dates:
            native[code]["latestObservationDate"] = latest_dates[code]
    category: dict[str, Any] = {"index": codes, "label": labels}
    if notes:
        category["note"] = notes
    if units:
        category["unit"] = units
    dates = calendar_dates(first, last)
    logger.debug(
        "SWEA group %d: %d series, %d dates %s..%s (%d with a latest observation)",
        group_id,
        len(codes),
        len(dates),
        first,
        last,
        len(latest_dates),
    )
    sources = sorted({_clean(row.get("source")) for row in rows} - {""})
    chain = group["chain"]

    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": record["dataset_code"],
        "language": LANGUAGE,
        **_urls("swea"),
        "metadata_url": series_by_group_url(group_id),
        "data_url": SWEA_OBSERVATIONS,
        "discontinued": all(row["seriesClosed"] for row in rows),
        "time_unit": "daily",
        "first_period": first,
        "last_period": last,
    }
    if group["description"]:
        nordicintel["description"] = group["description"]
    if len(chain) > 1:
        nordicintel["subject"] = {"code": chain[1]["id"], "label": chain[1]["label"]}
    nordicintel["paths"] = [list(chain)]
    return {
        "version": "2.0",
        "class": "dataset",
        "label": group["name"],
        "source": "; ".join(sources) or SOURCE,
        "updated": max(row["observationMaxDate"] for row in rows),
        "note": _notes(_clean(group["description"]), SWEA_TIME_NOTE),
        "id": [SERIES, TIME],
        "size": [len(codes), len(dates)],
        "role": {"metric": [SERIES], "time": [TIME]},
        "dimension": {
            SERIES: {
                "label": "Series",
                "category": category,
                "extension": {
                    "nordicintel": {"elimination": False},
                    PROVIDER_CODE: {"categories": native},
                },
            },
            TIME: {
                "label": "Date",
                "category": {"index": dates},
                "extension": {"nordicintel": {"elimination": False}},
            },
        },
        "link": _link("swea"),
        "value": [],
        "extension": {
            "nordicintel": nordicintel,
            PROVIDER_CODE: {
                "api": "swea",
                "group_id": group_id,
                "source_record": {
                    "group": {
                        "groupId": group_id,
                        "name": group["name"],
                        "description": group["description"],
                    },
                    "series": [dict(row) for row in rows],
                },
            },
        },
    }


def _latest_vintage(body: Any, series_id: str, policy_round: str) -> Mapping:
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, list):
        raise ValueError(f"unexpected forecasts response {str(body)[:200]!r}")
    for entry in data:
        if not isinstance(entry, Mapping) or entry.get("external_id") != series_id:
            continue
        vintages = entry.get("vintages")
        for vintage in vintages if isinstance(vintages, list) else [vintages]:
            if (
                isinstance(vintage, Mapping)
                and (vintage.get("metadata") or {}).get("policy_round") == policy_round
            ):
                return vintage
    raise ValueError(f"no {policy_round} vintage of {series_id} in the response")


def build_mpd(record: Mapping[str, Any], probe: Any) -> dict:
    """Build the document of one forecast series.

    Args:
        record: An ``mpd`` record from :func:`source.mpd_listing`.
        probe: The ``/forecasts?series=<id>&policy_round_name=<latest round>``
            response.
    """
    row, rounds = record["series"], list(record["policy_rounds"])
    series_id, meta = row["series_id"], row["metadata"]
    latest_round = rounds[-1]
    vintage = _latest_vintage(probe, series_id, latest_round)
    observed = sorted(
        {
            str(item["dt"])
            for item in vintage.get("observations") or []
            if isinstance(item, Mapping) and item.get("dt")
        }
    )
    if not observed:
        raise ValueError(f"the {latest_round} vintage of {series_id} is empty")
    for period in observed:
        date.fromisoformat(period)  # ValueError for a non-date period
    frequency = infer_frequency(observed, series_id)
    logger.debug(
        "Forecast %s: %s vintage has %d periods, inferred %s frequency",
        series_id,
        latest_round,
        len(observed),
        frequency,
    )
    start = meta.get("start_date") or observed[0]
    first = min(start, observed[0])
    periods = sorted(set(period_ends(first, observed[-1], frequency)) | set(observed))
    description = _clean(meta.get("description"))
    unit_label = _clean(meta.get("unit"))
    unit: dict[str, Any] = {}
    if unit_label:
        unit["label"] = unit_label
    if isinstance(meta.get("decimals"), int):
        unit["decimals"] = meta["decimals"]
    series_category: dict[str, Any] = {
        "index": [series_id],
        "label": {series_id: description},
    }
    if unit:
        series_category["unit"] = {series_id: unit}
    vintage_meta = dict(vintage.get("metadata") or {})
    published = str(vintage_meta.get("policy_round_end_dtm") or "")[:10]
    label = (
        f"{description}, {unit_label} ({series_id})"
        if unit_label
        else (f"{description} ({series_id})")
    )

    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": f"{label}: forecasts by policy round",
        "source": _clean(meta.get("source_agency")) or SOURCE,
    }
    try:
        if 1900 <= date.fromisoformat(published).year <= 2099:
            document["updated"] = published
    except ValueError:
        pass
    document.update(
        {
            "note": _notes(_clean(meta.get("note")), MPD_NOTE),
            "id": [SERIES, POLICY_ROUND, TIME],
            "size": [1, len(rounds), len(periods)],
            "role": {"metric": [SERIES], "time": [TIME]},
            "dimension": {
                SERIES: {
                    "label": "Series",
                    "category": series_category,
                    "extension": {"nordicintel": {"elimination": False}},
                },
                POLICY_ROUND: {
                    "label": "Policy round",
                    "category": {"index": rounds},
                    "extension": {"nordicintel": {"elimination": False}},
                },
                TIME: {
                    "label": "Period",
                    "category": {"index": periods},
                    "extension": {"nordicintel": {"elimination": False}},
                },
            },
            "link": _link("mpd"),
            "value": [],
            "extension": {
                "nordicintel": {
                    "provider_code": PROVIDER_CODE,
                    "dataset_code": record["dataset_code"],
                    "language": LANGUAGE,
                    **_urls("mpd"),
                    "metadata_url": MPD_SERIES_IDS,
                    "data_url": f"{MPD_FORECASTS}?series={series_id}",
                    "time_unit": TIME_UNITS[frequency],
                    "first_period": first,
                    "last_period": periods[-1],
                },
                PROVIDER_CODE: {
                    "api": "mpd",
                    "series_id": series_id,
                    "latest_policy_round": latest_round,
                    "latest_vintage": vintage_meta,
                    "source_record": dict(row),
                },
            },
        }
    )
    return document


def build_swestr(latest: Any) -> dict:
    """Build the SWESTR document from the ``/swestr/v1/latest/SWESTR`` response."""
    if (
        not isinstance(latest, Mapping)
        or not isinstance(latest.get("date"), str)
        or not isinstance(latest.get("rate"), int | float)
    ):
        raise ValueError(f"unexpected latest SWESTR response {str(latest)[:200]!r}")
    last = latest["date"]
    dates = calendar_dates(SWESTR_FIRST_DATE, last)
    logger.debug(
        "SWESTR: %d dates %s..%s from the latest fixing",
        len(dates),
        SWESTR_FIRST_DATE,
        last,
    )
    labels, notes, units = {}, {}, {}
    for code in SWESTR_MEASURES:
        label, unit, note = SWESTR_MEASURE_TEXT[code]
        labels[code], units[code] = label, {"label": unit}
        if note:
            notes[code] = [note]
    flags = {
        key: latest.get(key)
        for key in (
            "publicationTime",
            "republication",
            "alternativeCalculation",
            "alternativeCalculationReason",
        )
    }
    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": "SWESTR, Swedish krona Short Term Rate",
        "source": SOURCE,
    }
    published = str(latest.get("publicationTime") or "")[:10]
    try:
        date.fromisoformat(published)
        document["updated"] = published
    except ValueError:
        pass
    document.update(
        {
            "note": [
                "SWESTR is published on Swedish banking days at about 07:00 UTC for "
                "the previous banking day; other dates have no data.",
                SWESTR_STATUS_NOTE,
            ],
            "id": [MEASURE, TIME],
            "size": [len(SWESTR_MEASURES), len(dates)],
            "role": {"metric": [MEASURE], "time": [TIME]},
            "dimension": {
                MEASURE: {
                    "label": "Measure",
                    "category": {
                        "index": list(SWESTR_MEASURES),
                        "label": labels,
                        "note": notes,
                        "unit": units,
                    },
                    "extension": {"nordicintel": {"elimination": False}},
                },
                TIME: {
                    "label": "Date",
                    "category": {"index": dates},
                    "extension": {"nordicintel": {"elimination": False}},
                },
            },
            "link": _link("swestr"),
            "value": [],
            "extension": {
                "nordicintel": {
                    "provider_code": PROVIDER_CODE,
                    "dataset_code": "swestr",
                    "language": LANGUAGE,
                    **_urls("swestr"),
                    "metadata_url": SWESTR_LATEST_URL,
                    "data_url": SWESTR_DATA_URL,
                    "discontinued": False,
                    "time_unit": "daily",
                    "first_period": SWESTR_FIRST_DATE,
                    "last_period": last,
                },
                PROVIDER_CODE: {
                    "api": "swestr",
                    "interest_rate_id": SWESTR_ID,
                    "latest_publication": flags,
                },
            },
        }
    )
    return document
