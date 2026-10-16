"""Shared fixtures: recorded Riksbank responses and an in-process Riksbank imitation.

``fixtures/riksbank.json`` holds harvest responses keyed ``GET <url>?<sorted query>``,
built on 2026-10-09 from the live samples in ``docs/api/riksbank/samples/``:

- ``swea-groups.json`` (complete group tree) and ``swea-series.json`` trimmed to
  groups 2, 3, 151, 155 and four group-130 currencies (EUR, USD, JPY, ATS);
- latest observations: group 130 from ``swea-observations-bygroup-130-latest.json``
  (trimmed), groups 2, 151 and 155 recorded live on 2026-10-09, group 3 synthetic;
- ``mpd-series-ids.json`` trimmed to SEQGDPNAYSA, SEMCPIFNAYNA and SEAWAGNAYCA,
  ``mpd-policy-rounds.json`` complete, the 2026:3 SEQGDPNAYSA vintage from
  ``mpd-forecasts-gdp-2026-3.json`` (last 24 periods), synthetic 2026:3 vintages
  for SEMCPIFNAYNA and SEAWAGNAYCA;
- ``swestr-latest.json``.

``fixtures/data.json`` holds the retrieval tables: SWEA observations from
``swea-observations-bygroup-2.json`` and ``swea-observations-sekeurpmi.json`` (plus
SECBMARGEFF's last value, recorded live), the group 2 and 130 series rows,
``mpd-forecasts.trimmed.json`` (2 series × rounds 2026:3 and 2026:2), and SWESTR
fixings of 2026-10-01, 10-02 and 10-08 recorded live plus one synthetic
republished, alternatively calculated fixing on 2026-10-05.
"""

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from apps.harvest.metadata_adapters.riksbank.client import Unreachable
from tests.support.binding import binding

FIXTURES = Path("tests/fixtures/riksbank")
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "riksbank.json").read_text(encoding="utf-8")
)
TABLES: dict[str, Any] = json.loads(
    (FIXTURES / "data.json").read_text(encoding="utf-8")
)
API = "https://api.riksbank.se"


def fixture_key(url: str, params: dict[str, str] | None = None) -> str:
    return f"GET {url}?{urlencode(sorted(params.items()))}" if params else f"GET {url}"


class RecordedClient:
    """Answer harvester requests from recorded responses, like ``RiksbankClient``.

    Unrecorded requests raise ``LookupError``; keys in ``failing`` raise
    ``TimeoutError`` to imitate an unreachable host; keys in ``missing`` answer
    None, as the client does for HTTP 204 (and 404 with ``missing_ok``).
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = copy.deepcopy(RECORDED)
        self.failing: set[str] = set()
        self.missing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    async def get_json(self, url, params=None, *, missing_ok=False) -> Any:
        key = fixture_key(url, params)
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise TimeoutError(f"timed out: {key}")
        if key in self.missing:
            return None
        if key not in self.responses:
            raise LookupError(f"not recorded: {key}")
        return copy.deepcopy(self.responses[key])


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``RiksbankClient`` after its retries."""

    async def get_json(self, url, params=None, *, missing_ok=False):
        try:
            return await super().get_json(url, params, missing_ok=missing_ok)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


PROVIDER = binding("riksbank", "riksbank", languages=["en"], retry_cooldown_seconds=0)
ALL = {
    "en/swea_group_2",
    "en/swea_group_130",
    "en/swea_group_151",
    "en/swea_group_155",
    "en/mpd_SEQGDPNAYSA",
    "en/mpd_SEMCPIFNAYNA",
    "en/mpd_SEAWAGNAYCA",
    "en/swestr",
}


def execute(source, data, output, **selection):
    """Run the adapter against ``source`` and collect the output into ``data``."""
    import asyncio

    from apps.harvest.metadata_adapters.riksbank.harvest import ADAPTER
    from harvest import lifecycle

    result = asyncio.run(
        lifecycle.run(
            ADAPTER, PROVIDER, data, output, selection=selection, client=source
        )
    )
    lifecycle.collect(ADAPTER, data, output)
    return result


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


# --- Retrieval: an in-process imitation of the three Riksbank APIs --------------


def _within(day: str, first: str, last: str) -> bool:
    return (
        date.fromisoformat(first) <= date.fromisoformat(day) <= date.fromisoformat(last)
    )


class FakeRiksbank:
    """Answer observation requests from ``fixtures/data.json`` as the Riksbank does.

    Reproduces behaviour observed on 2026-10-09: SWEA answers HTTP 204 (no body)
    for unknown series, unknown groups and empty ranges; the forecast API answers
    404 for a series without a vintage in the round and for unknown series; SWESTR
    answers 404 for an unknown interest rate id.
    """

    def __init__(self) -> None:
        self.tables = copy.deepcopy(TABLES)
        self.requests: list[str] = []
        self.accept: list[str | None] = []
        self.fail_status: int | None = None

    def app(self):
        from aiohttp import web

        app = web.Application()
        app.router.add_get(
            "/swea/v1/Observations/{series}/{first}/{last}", self.observations
        )
        app.router.add_get("/swea/v1/Series/ByGroup/{group}", self.series_by_group)
        app.router.add_get(
            "/monetary_policy_data/v1/forecasts/series_ids", self.series_ids
        )
        app.router.add_get("/monetary_policy_data/v1/forecasts", self.forecasts)
        app.router.add_get("/swestr/v1/{rate}", self.swestr)
        return app

    def _record(self, request):
        """Log the request; return an error response when failing is configured."""
        from aiohttp import web

        self.requests.append(str(request.rel_url))
        self.accept.append(request.headers.get("Accept"))
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        return None

    async def observations(self, request):
        from aiohttp import web

        if (failure := self._record(request)) is not None:
            return failure
        info = request.match_info
        rows = [
            row
            for row in self.tables["swea_observations"].get(info["series"], [])
            if _within(row["date"], info["first"], info["last"])
        ]
        return web.json_response(rows) if rows else web.Response(status=204)

    async def series_by_group(self, request):
        from aiohttp import web

        if (failure := self._record(request)) is not None:
            return failure
        group = int(request.match_info["group"])
        rows = [r for r in self.tables["swea_series"] if r["groupId"] == group]
        return web.json_response(rows) if rows else web.Response(status=204)

    async def series_ids(self, request):
        from aiohttp import web

        if (failure := self._record(request)) is not None:
            return failure
        return web.json_response(self.tables["series_ids"])

    async def forecasts(self, request):
        from aiohttp import web

        if (failure := self._record(request)) is not None:
            return failure
        series, policy_round = (
            request.query["series"],
            request.query["policy_round_name"],
        )
        for entry in self.tables["forecasts"]["data"]:
            if entry["external_id"] != series:
                continue
            for vintage in entry["vintages"]:
                if vintage["metadata"]["policy_round"] == policy_round:
                    body = {**entry, "vintages": vintage}  # filtered: one object
                    return web.json_response({"data": [body]})
        return web.Response(status=404)

    async def swestr(self, request):
        from aiohttp import web

        if (failure := self._record(request)) is not None:
            return failure
        if request.match_info["rate"] != "SWESTR":
            return web.Response(status=404)
        first = request.query["fromDate"]
        last = request.query.get("toDate", "2099-12-31")
        return web.json_response(
            [r for r in self.tables["swestr"] if _within(r["date"], first, last)]
        )


@pytest.fixture
def fake_riksbank() -> FakeRiksbank:
    return FakeRiksbank()


DATASETS = {
    "swea": {
        "dataset_code": "swea_group_2",
        "data_url": "/swea/v1/Observations",
        "metadata_url": "/swea/v1/Series/ByGroup/2",
        "metadata": {
            "/id": ["Series", "Time"],
            "/extension/riksbank": {"api": "swea", "group_id": 2},
        },
    },
    "mpd": {
        "dataset_code": "mpd_SEQGDPNAASA",
        "data_url": "/monetary_policy_data/v1/forecasts?series=SEQGDPNAASA",
        "metadata_url": "/monetary_policy_data/v1/forecasts/series_ids",
        "metadata": {
            "/id": ["Series", "PolicyRound", "Time"],
            "/extension/riksbank": {"api": "mpd", "series_id": "SEQGDPNAASA"},
        },
    },
    "swestr": {
        "dataset_code": "swestr",
        "data_url": "/swestr/v1/SWESTR",
        "metadata_url": "/swestr/v1/latest/SWESTR",
        "metadata": {
            "/id": ["Measure", "Time"],
            "/extension/riksbank": {"api": "swestr", "interest_rate_id": "SWESTR"},
        },
    },
}


async def retrieve_from(fake: FakeRiksbank, api: str, selection, **overrides):
    """Call ``retrieve`` for ``api`` against ``fake``; return the fragment."""
    import aiohttp
    from aiohttp.test_utils import TestServer

    from retrieval.data_adapters.riksbank import retrieve

    async with TestServer(fake.app()) as server:
        async with aiohttp.ClientSession() as session:
            dataset = DATASETS[api]
            arguments = {
                "provider_code": "riksbank",
                "dataset_code": dataset["dataset_code"],
                "language": "en",
                "selection": selection,
                "data_url": str(server.make_url(dataset["data_url"])),
                "metadata_url": str(server.make_url(dataset["metadata_url"])),
                "metadata": dataset["metadata"],
            }
            for name in ("data_url", "metadata_url"):
                if isinstance(overrides.get(name), str) and overrides[name].startswith(
                    "/"
                ):
                    overrides[name] = str(server.make_url(overrides[name]))
            fragment = await retrieve(session, **{**arguments, **overrides})
            assert not session.closed
            return fragment


def check_fragment(fragment: dict[str, Any], selection) -> None:
    """Validate a fragment: result schema plus the contract's consumer checks."""
    from schema.validation import validate_retrieval

    validate_retrieval(fragment, selection=selection)
    ids = fragment["id"]
    assert ids == list(fragment["dimension"]) == list(selection)
    total = 1
    for name, size in zip(ids, fragment["size"], strict=True):
        index = fragment["dimension"][name]["category"]["index"]
        assert index == list(selection[name]) and size == len(index)
        total *= size
    for field in ("value", "status"):
        content = fragment.get(field)
        if isinstance(content, list):
            assert len(content) == total
        elif isinstance(content, dict):
            assert all(int(key) < total for key in content)


@pytest.fixture(name="retrieve_from")
def retrieve_from_fixture():
    return retrieve_from


@pytest.fixture(name="check_fragment")
def check_fragment_fixture():
    return check_fragment
