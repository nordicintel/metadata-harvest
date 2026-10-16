"""Weekly Riksbank refresh across three APIs with independent dataset failures.

Each API (SWEA, the forecast API, SWESTR) is one scope: its catalogue requests
are its discovery, stored as shared source files. No ETag, Last-Modified or
timestamp is published, so every discovered dataset is probed each run; an
identical probe keeps its stored bytes. A dataset's probe (SWEA latest
observations by group, the latest forecast vintage, or the SWESTR fixing read at
discovery) is its endpoint check and its stored source entry.
"""

from urllib.parse import urlencode

from apps.harvest.metadata_adapters.riksbank.client import (
    GROUPS_URL,
    MPD_FORECASTS,
    MPD_POLICY_ROUNDS,
    MPD_SERIES_IDS,
    PROVIDER_CODE,
    SERIES_URL,
    SWESTR_LATEST_URL,
    RiksbankClient,
    latest_by_group_url,
)
from apps.harvest.metadata_adapters.riksbank.mapping import MAPPING_VERSION
from apps.harvest.metadata_adapters.riksbank.source import (
    Listing,
    mpd_listing,
    swea_listing,
    swestr_listing,
)
from harvest.lifecycle import Adapter, Run, Scope, Stored, Task
from retrieval.data_adapters.riksbank import api_of

APIS = ("swea", "mpd", "swestr")
SHARED = {
    "swea": {GROUPS_URL: "swea_groups.json", SERIES_URL: "swea_series.json"},
    "mpd": {
        MPD_SERIES_IDS: "mpd_series_ids.json",
        MPD_POLICY_ROUNDS: "mpd_policy_rounds.json",
    },
    "swestr": {SWESTR_LATEST_URL: "swestr_latest.json"},
}
"""Catalogue URLs and the shared source file each is stored as."""


def _updated(record: dict, responses: dict):
    """The source's own latest date, read from the probe without interpretation."""
    if record["api"] == "swea":
        dates = [
            row.get("date")
            for row in responses["latest"]
            if isinstance(row, dict) and isinstance(row.get("date"), str)
        ]
        return max(dates) if dates else None
    if record["api"] == "mpd":
        return record["policy_rounds"][-1]
    latest = responses["latest"]
    return latest.get("date") if isinstance(latest, dict) else None


async def discover_api(client, api: str, bodies: dict | None = None) -> Listing:
    """Read one API's catalogue; raises when the whole scope cannot be read.

    ``bodies`` receives each catalogue response keyed by its shared file name.
    """
    bodies = {} if bodies is None else bodies

    async def get(url):
        bodies[SHARED[api][url]] = body = await client.get_json(url)
        return body

    if api == "swea":
        groups = await get(GROUPS_URL)
        return swea_listing(groups, await get(SERIES_URL))
    if api == "mpd":
        series = await get(MPD_SERIES_IDS)
        return mpd_listing(series, await get(MPD_POLICY_ROUNDS))
    return swestr_listing(await get(SWESTR_LATEST_URL))


async def probe_record(client, record: dict) -> dict:
    """Probe one dataset's endpoint and return its source responses.

    Raises:
        Unreachable: The probe failed (an endpoint failure).
        ValueError: The probe answered that the resource does not exist.
    """
    if record["api"] == "swea":
        group_id = record["group"]["groupId"]
        latest = await client.get_json(latest_by_group_url(group_id))
        if latest is None:
            raise ValueError(f"no latest observations for SWEA group {group_id}")
        return {"latest": latest}
    if record["api"] == "mpd":
        series_id = record["series"]["series_id"]
        latest_round = record["policy_rounds"][-1]
        probe = await client.get_json(
            MPD_FORECASTS,
            {"series": series_id, "policy_round_name": latest_round},
            missing_ok=True,
        )
        if probe is None:
            raise ValueError(f"no {latest_round} vintage of {series_id}")
        return {"vintage": probe}
    return {"latest": record["latest"]}  # SWESTR: discovery read the fixing


def apis_for(name: str) -> set[str]:
    """The APIs whose catalogues can resolve one selected name."""
    folded = name.casefold()
    if api := api_of(folded):
        return {api}
    if folded.isdigit():
        return {"swea"}
    if folded == "swestr":
        return {"swestr"}
    return {"swea", "mpd"}  # a SWEA or forecast series id


def aliases(record: dict) -> set[str]:
    """Casefolded names selecting a record: its code and native ids."""
    names = {record["dataset_code"].casefold()}
    if record["api"] == "swea":
        names.add(str(record["group"]["groupId"]))
        names.update(row["seriesId"].casefold() for row in record["series"])
    elif record["api"] == "mpd":
        names.add(record["series"]["series_id"].casefold())
    return names


async def discover(run: Run) -> list[Scope]:
    subjects = run.selection.get("subjects")
    wanted = {name.casefold(): name for name in subjects} if subjects else None
    apis = (
        list(APIS)
        if wanted is None
        else [a for a in APIS if any(a in apis_for(n) for n in wanted)]
    )
    run.state["mapping_version"] = MAPPING_VERSION
    loaded, matched = set(), set()
    scopes = []
    for api in apis:
        scope = Scope(api, None)

        async def load(api=api, scope=scope) -> list[Task]:
            bodies: dict = {}
            listing = await discover_api(run.client, api, bodies)
            for name, body in bodies.items():
                run.store.put_shared(PROVIDER_CODE, name, body, retrieved_at=run.stamp)
            for resource, error in listing.problems:
                run.fail("discovery", resource, error)
            scope.complete = listing.complete
            records = listing.records
            if wanted is not None:
                records = [r for r in records if aliases(r) & wanted.keys()]
                for record in listing.records:
                    matched.update(aliases(record) & wanted.keys())
                matched.update(code.casefold() for code in listing.quarantined)
                loaded.add(api)
                for name in sorted(wanted.keys() - matched):
                    if apis_for(name) <= loaded:  # every candidate catalogue read
                        matched.add(name)
                        run.fail(
                            "discovery",
                            f"*/{wanted[name]}",
                            LookupError("Unknown Riksbank dataset"),
                        )
            return [
                Task(f"{language}/{record['dataset_code']}", record)
                for record in records
                for language in run.languages
            ]

        scope.load = load
        scopes.append(scope)
    return scopes


async def fetch(run: Run, task: Task) -> Stored:
    record = task.item
    responses = await probe_record(run.client, record)
    return Stored(
        responses=responses,
        derived={"record": record},
        updated=_updated(record, responses),
        info={"mapping_version": MAPPING_VERSION},
    )


def probe_url(record: dict) -> str:
    if record["api"] == "swea":
        return latest_by_group_url(record["group"]["groupId"])
    if record["api"] == "mpd":
        params = {
            "series": record["series"]["series_id"],
            "policy_round_name": record["policy_rounds"][-1],
        }
        return f"{MPD_FORECASTS}?{urlencode(params)}"
    return SWESTR_LATEST_URL


ADAPTER = Adapter(
    name="riksbank",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-riksbank.yml",
    client=lambda binding: RiksbankClient(binding.options),
    discover=discover,
    fetch=fetch,
    options={"subject": "subjects"},
    languages=("en",),
    scope_of=lambda key: api_of(key.split("/", 1)[1]) or "*",
)
