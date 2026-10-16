"""Weekly Tillväxtverket refresh with independent dataset failures.

There is no listing endpoint: the datasets are the hard-coded registry in
:mod:`.source`, one scope. Every run checks every (selected) dataset with two small
requests, its columns/``rowCount`` (``limit=0``) and its distinct ``LOAD_DATE``
values; together they are the change signal and the endpoint check. An unchanged
signal keeps the stored source entry without distinct-value queries; otherwise the
entry is refetched (one distinct query per dimension plus the column label file,
kept as a companion file). The data.europa.eu description is read by parsing from
the accepted portal snapshot.
"""

from apps.harvest.metadata_adapters.tillvaxtverket.client import (
    PROVIDER_CODE,
    TillvaxtverketClient,
    cbase_url,
    labels_url,
)
from apps.harvest.metadata_adapters.tillvaxtverket.mapping import MAPPING_VERSION
from apps.harvest.metadata_adapters.tillvaxtverket.parse import LABELS, document_from
from apps.harvest.metadata_adapters.tillvaxtverket.source import (
    DATASETS,
    LOAD_DATE,
    REGION_DISTINCT,
    Probe,
    Spec,
    parse_labels,
    parse_probe,
)
from harvest.lifecycle import Adapter, Kept, Run, Scope, Stored, Task

DISTINCT_LIMIT = 100_000
"""``limit`` of distinct-value queries (the largest list, days, has ~4,300 rows)."""
LABELS_ENCODING = "cp1252"
"""The label files are Latin-1 (Windows-1252) text without a charset header."""


def key_of(spec: Spec) -> str:
    return f"sv/{spec.dataset_code}"


def _signal(spec: Spec, responses: dict):
    """The change signal of stored responses, or None when they do not parse."""
    try:
        return parse_probe(spec, responses["columns"], responses["load_dates"]).signal()
    except Exception:  # noqa: BLE001 - an unparsable signal counts as changed
        return None


class Dataset:
    """Requests for one registered dataset."""

    def __init__(self, client, spec: Spec):
        self.client, self.spec = client, spec
        self.url = cbase_url(spec.area, spec.name)

    async def distinct(self, *columns: str):
        params = [("dimension", column) for column in columns]
        params += [("format", "json"), ("limit", str(DISTINCT_LIMIT))]
        return await self.client.get_json(self.url, params)

    async def probe(self) -> tuple[dict, Probe | None]:
        """The change-signal responses and their parsed signal (None when the
        responses do not match the registry); Unreachable means the endpoint
        failed."""
        columns = await self.client.get_json(
            self.url, [("format", "json"), ("limit", "0")]
        )
        loads = await self.distinct(LOAD_DATE)
        responses = {"columns": columns, "load_dates": loads}
        try:
            return responses, parse_probe(self.spec, columns, loads)
        except Exception:  # noqa: BLE001 - stored anyway; parse reports
            return responses, None

    async def labels(self) -> str:
        """The column label file as text (parsed later)."""
        content = await self.client.get_text(
            labels_url(self.spec.area, self.spec.name), encoding=LABELS_ENCODING
        )
        parse_labels(content)  # a safety net: the file must be a label file
        return content

    async def cube(self) -> dict:
        """The distinct-value responses a cube is built from."""
        regions = await self.distinct(*REGION_DISTINCT)
        dimensions = {}
        for dimension in self.spec.dimensions:
            dimensions[dimension.id] = await self.distinct(
                dimension.column, *dimension.label_columns
            )
        periods = await self.distinct(self.spec.time)
        return {"regions": regions, "dimensions": dimensions, "periods": periods}


async def build_document(client, spec: Spec, *, portal=None) -> dict:
    """One dataset's document built from fresh requests (tests and live checks).

    ``portal`` names an accepted portal snapshot directory for the
    data.europa.eu description; without it the label is the dataset name.
    """
    from apps.harvest.metadata_adapters.tillvaxtverket.client import dcat_id
    from apps.harvest.metadata_adapters.tillvaxtverket.parse import portal_records

    dataset = Dataset(client, spec)
    responses, _ = await dataset.probe()
    responses.update(await dataset.cube())
    labels = parse_labels(await dataset.labels())
    record = portal_records(portal).get(dcat_id(spec.name))
    return document_from(spec, responses, labels, record)


async def discover(run: Run) -> list[Scope]:
    async def load() -> list[Task]:
        products = run.selection.get("products")
        if products is None:
            return [Task(key_of(spec), spec) for spec in DATASETS]
        wanted = {name.casefold(): name for name in products}
        names = {k.casefold() for s in DATASETS for k in (s.name, s.dataset_code)}
        for name in sorted(wanted.keys() - names):
            run.fail(
                "discovery",
                f"*/{wanted[name]}",
                LookupError("Unknown Tillväxtverket dataset"),
            )
        return [
            Task(key_of(spec), spec)
            for spec in DATASETS
            if {spec.name.casefold(), spec.dataset_code.casefold()} & wanted.keys()
        ]

    run.state["mapping_version"] = MAPPING_VERSION
    return [Scope("registry", load)]


async def fetch(run: Run, task: Task) -> Stored | Kept:
    spec = task.item
    dataset = Dataset(run.client, spec)
    stored = run.store.get(run.stored_key(task.key))
    info = run.records.get(task.key, {})
    responses, probe = await dataset.probe()
    if (
        stored is not None
        and probe is not None
        and info.get("mapping_version") == MAPPING_VERSION
        and not info.get("rebuild")
        and _signal(spec, stored["responses"]) == probe.signal()
    ):
        return Kept()
    previous_labels = None
    if stored is not None:
        for relative in run.store.entries()[run.stored_key(task.key)]["files"]:
            if relative.endswith(f".{LABELS}"):
                previous_labels = run.store.read_file(relative)
    labels_missing = False
    try:
        labels = await dataset.labels()
    except Exception as error:  # a dependency, not the value endpoint
        labels, labels_missing = previous_labels, True
        resource = f"{task.key}#labels"
        if not any(e["resource"] == resource for e in run.errors):
            run.fail("dependency", resource, error)
            run.counts["dependency_failed"] += 1
    responses.update(await dataset.cube())
    return Stored(
        responses=responses,
        updated=probe.loaded if probe is not None else None,
        files={LABELS: labels} if labels else None,
        info={"mapping_version": MAPPING_VERSION, "rebuild": labels_missing},
    )


ADAPTER = Adapter(
    name="tillvaxtverket",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-tillvaxtverket.yml",
    client=lambda binding: TillvaxtverketClient(),
    discover=discover,
    fetch=fetch,
    options={"product": "products"},
    languages=("sv",),
    counts=("dependency_failed",),
)
