"""Shared fixtures: recorded SMHI emissions responses served by stand-ins.

Provenance of ``fixtures/``:

- ``getfilteroptions.json``, ``getdata-national-CO2.json`` and
  ``getdata-stockholm-NOx-transport-sub2025.json``: recorded 2026-10-09 by the
  research in ``docs/api/smhi-emissionsdatabasen/samples/`` (same file names). The
  Stockholm sample was requested with ``submission=2025``.
- ``getdata-stockholms-lan-CO2.json`` (``county=1&municipality=0&hsektor=0``),
  ``getdata-national-CO2-transporter.json`` (``hsektor=4``),
  ``getdata-kiruna-CH4-jordbruk.json`` (``county=25&municipality=2584&hsektor=7``),
  ``getdata-national-Dioxin.json``, ``getdata-unknown-county-CO2.json``
  (``county=99``) and ``getdata-unknown-substance.html`` (``sub=XYZ``, HTTP 500):
  recorded 2026-10-09 with the adapter's bounded probes.
- Synthetic: the national probes of the 27 other substances are the recorded CO2
  probe with its substance key renamed to that substance's ``long_name``
  (:func:`synthetic_probe`). Individual tests derive further labelled synthetic
  cases (failing probes, unknown sector labels, malformed rows) in code.
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from apps.harvest.metadata_adapters.smhi_emissions.api import (
    DATA_URL,
    FILTER_OPTIONS_URL,
    probe_parameters,
)

FIXTURES = Path("tests/fixtures/smhi_emissions")


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


FILTER_OPTIONS = load("getfilteroptions.json")
NATIONAL_CO2 = load("getdata-national-CO2.json")
NATIONAL_DIOXIN = load("getdata-national-Dioxin.json")


def substances(tree=None) -> dict[str, str]:
    """``{code: long_name}`` of every substance in the tree, first-seen order."""
    found: dict[str, str] = {}
    for sector in tree or FILTER_OPTIONS:
        for subsector in sector["subsectors"]:
            for entry in subsector["substances"]:
                for code, details in entry.items():
                    found.setdefault(code, details["long_name"])
    return found


def synthetic_probe(long_name: str) -> dict[str, Any]:
    """Synthetic: the recorded national CO2 probe under another substance name."""
    body = copy.deepcopy(NATIONAL_CO2)
    body["data"] = {long_name: body["data"].pop("Koldioxid (CO2)")}
    return body


def fixture_key(url: str, params: dict[str, str] | None = None) -> str:
    """``GET url?sortedquery``: the recorded-response key."""
    query = f"?{urlencode(sorted(params.items()))}" if params else ""
    return f"GET {url}{query}"


def probe_key(code: str) -> str:
    return fixture_key(DATA_URL, probe_parameters(code))


def recorded_responses() -> dict[str, Any]:
    responses = {fixture_key(FILTER_OPTIONS_URL): copy.deepcopy(FILTER_OPTIONS)}
    for code, long_name in substances().items():
        body = {"CO2": NATIONAL_CO2, "Dioxin": NATIONAL_DIOXIN}.get(code)
        responses[probe_key(code)] = copy.deepcopy(body or synthetic_probe(long_name))
    return responses


class RecordedClient:
    """Answer harvester requests from recorded responses, like ``SmhiClient``.

    Unrecorded requests raise ``LookupError``; keys in ``failing`` raise
    ``TimeoutError`` to imitate an unreachable host.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = recorded_responses()
        self.failing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    def _answer(self, url: str, params: dict[str, str] | None) -> Any:
        key = fixture_key(url, params)
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise TimeoutError(f"timed out: {key}")
        if key not in self.responses:
            raise LookupError(f"not recorded: {key}")
        return copy.deepcopy(self.responses[key])

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        return self._answer(url, params)

    @property
    def tree(self) -> list[dict[str, Any]]:
        return self.responses[fixture_key(FILTER_OPTIONS_URL)]


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


# --- Retrieval: an in-process imitation of getdata/ and getfilteroptions/ -------

RECORDED_DATA = {
    ("0", "0", "0", "CO2"): "getdata-national-CO2.json",
    ("0", "0", "4", "CO2"): "getdata-national-CO2-transporter.json",
    ("1", "0", "0", "CO2"): "getdata-stockholms-lan-CO2.json",
    ("1", "180", "4", "NOx"): "getdata-stockholm-NOx-transport-sub2025.json",
    ("25", "2584", "7", "CH4"): "getdata-kiruna-CH4-jordbruk.json",
    ("0", "0", "0", "Dioxin"): "getdata-national-Dioxin.json",
}
"""(county, municipality, hsektor, sub) -> recorded getdata response."""
SERVER_ERROR = (FIXTURES / "getdata-unknown-substance.html").read_text(encoding="utf-8")


class FakeSmhi:
    """Answer ``getdata/`` and ``getfilteroptions/`` as SMHI did on 2026-10-09.

    Recorded queries return their recordings. An unknown ``sub`` gives Django's
    HTTP 500 page; any other unrecorded query of a known substance gives
    ``{"data": {<long_name>: {}}}``, as SMHI does for an unknown county.
    """

    def __init__(self) -> None:
        self.data = {key: load(name) for key, name in RECORDED_DATA.items()}
        self.tree = copy.deepcopy(FILTER_OPTIONS)
        self.queries: list[dict[str, str]] = []
        self.listings = 0
        self.fail_status: int | None = None

    async def getdata(self, request):
        from aiohttp import web

        query = dict(request.query)
        self.queries.append(query)
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        names = substances()
        if query.get("sub") not in names:
            return web.Response(status=500, text=SERVER_ERROR, content_type="text/html")
        key = (query["county"], query["municipality"], query["hsektor"], query["sub"])
        body = self.data.get(key) or {
            "data": {names[query["sub"]]: {}},
            "primary_submission": 2026,
            "secondary_submissions": [],
        }
        return web.json_response(body)

    async def getfilteroptions(self, request):
        from aiohttp import web

        self.listings += 1
        return web.json_response(self.tree)


@pytest.fixture
def fake_smhi() -> FakeSmhi:
    return FakeSmhi()


def native(substance: str = "CO2") -> dict[str, Any]:
    """The harvested ``extension.smhi_emissions`` of a substance's document."""
    from apps.harvest.metadata_adapters.smhi_emissions.mapping import build_dataset
    from apps.harvest.metadata_adapters.smhi_emissions.source import (
        parse_filter_options,
        parse_probe,
    )

    options = parse_filter_options(FILTER_OPTIONS)
    body = synthetic_probe(options.substances[substance].long_name)
    probe = parse_probe(body, options, substance)
    document = build_dataset(options.substances[substance], options, probe)
    return document["extension"]["smhi_emissions"]


async def retrieve_from(fake: FakeSmhi, selection, *, substance="CO2", **kwargs):
    """Call ``retrieve`` against ``fake``; return the fragment."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from retrieval.data_adapters.smhi_emissions import retrieve

    app = web.Application()
    app.router.add_get("/api/getdata/", fake.getdata)
    app.router.add_get("/api/getfilteroptions/", fake.getfilteroptions)
    async with TestServer(app) as server:
        async with aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "smhi",
                "dataset_code": f"emissionsdatabasen_{substance}",
                "language": "sv",
                "selection": selection,
                "data_url": str(server.make_url("/api/getdata/")),
                "metadata_url": str(server.make_url("/api/getfilteroptions/")),
                "metadata": {
                    "/id": ["Region", "Sektor", "Tid", "ContentsCode"],
                    "/extension/smhi_emissions": (
                        native(substance)
                        if substance in substances()
                        else {**native("CO2"), "substance": {"code": substance}}
                    ),
                },
                **kwargs,
            }
            fragment = await retrieve(session, **arguments)
            assert not session.closed
            return fragment


def check_fragment(fragment: dict[str, Any], selection) -> None:
    """Validate a fragment: result schema plus the contract's consumer checks."""
    from schema.validation import validate_retrieval

    validate_retrieval(fragment, selection=selection)
    ids = fragment["id"]
    assert ids == list(fragment["dimension"])
    assert set(ids) == set(selection)
    total = 1
    for name, size in zip(ids, fragment["size"], strict=True):
        codes = fragment["dimension"][name]["category"]["index"]
        assert size == len(codes)
        assert sorted(codes) == sorted(selection[name])
        total *= size
    content = fragment["value"]
    if isinstance(content, list):
        assert len(content) == total
    else:
        assert all(int(key) < total for key in content)
    assert "status" not in fragment


@pytest.fixture(name="retrieve_from")
def retrieve_from_fixture():
    return retrieve_from


@pytest.fixture(name="check_fragment")
def check_fragment_fixture():
    return check_fragment
