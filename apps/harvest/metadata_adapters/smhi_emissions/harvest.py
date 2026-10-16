"""Monthly rebuild of Nationella emissionsdatabasen with independent substance failures.

One ``getfilteroptions/`` request is the only scope: it lists the sector tree and
the substances. Each substance's national ``getdata`` probe supplies its years and
submission round and is its endpoint check; the probe body is the substance's
source entry and the tree a shared file. A full run is 30 requests, so every run
refetches every substance. A quarantined substance (listed with conflicting
identities) is a metadata failure that keeps its last stored probe.
"""

from dataclasses import asdict

from apps.harvest.metadata_adapters.smhi_emissions.client import (
    DATA_URL,
    FILTER_OPTIONS_URL,
    PROVIDER_CODE,
    SmhiClient,
    dataset_code,
    probe_parameters,
)
from apps.harvest.metadata_adapters.smhi_emissions.mapping import MAPPING_VERSION
from apps.harvest.metadata_adapters.smhi_emissions.source import (
    FilterOptions,
    parse_filter_options,
)
from harvest.lifecycle import Adapter, Run, Scope, Stored, Task


def _select(run: Run, options: FilterOptions) -> list[str]:
    """Selected substance codes (exact, else case-insensitive); unknown ones fail."""
    listed = [*options.substances, *options.quarantined]
    subjects = run.selection.get("subjects")
    if subjects is None:
        return listed
    selected = []
    for name in subjects:
        matches = [c for c in listed if c == name] or [
            c for c in listed if c.casefold() == name.casefold()
        ]
        if len(matches) == 1:
            selected.append(matches[0])
        else:
            run.fail("discovery", f"*/{name}", LookupError("Unknown SMHI substance"))
    return list(dict.fromkeys(selected))


def submission_of(body) -> int | None:
    """``primary_submission`` read straight from the probe body, else None."""
    value = body.get("primary_submission") if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def discover(run: Run) -> list[Scope]:
    scope = Scope("filteroptions", None)

    async def load() -> list[Task]:
        tree = await run.client.get_json(FILTER_OPTIONS_URL)
        options = parse_filter_options(tree)
        if not options.substances and not options.quarantined:
            raise ValueError("getfilteroptions lists no substances")
        run.store.put_shared(
            PROVIDER_CODE, "filteroptions.json", tree, retrieved_at=run.stamp
        )
        for code, reason in options.quarantined.items():
            run.log.warning("Quarantined substance %s: %s", code, reason)
        for resource, error in options.problems:
            run.fail("discovery", resource, error)
        scope.complete = options.complete
        run.report["tree_complete"] = options.complete
        run.state["mapping_version"] = MAPPING_VERSION
        return [
            Task(f"{language}/{dataset_code(code)}", (code, options))
            for code in _select(run, options)
            for language in run.languages
        ]

    scope.load = load
    return [scope]


async def fetch(run: Run, task: Task) -> Stored:
    code, options = task.item
    if code in options.quarantined:  # keep the last probe until it is stale
        raise ValueError(options.quarantined[code])
    substance = options.substances[code]
    body = await run.client.get_json(DATA_URL, probe_parameters(code))
    submission = submission_of(body)
    submissions = run.report.setdefault("submissions", [])
    if submission is not None and submission not in submissions:
        submissions.append(submission)
        submissions.sort()
    return Stored(
        responses={"getdata": body},
        derived={"substance": asdict(substance)},
        updated=submission,
        info={"mapping_version": MAPPING_VERSION, "submission": submission},
    )


ADAPTER = Adapter(
    name="smhi_emissions",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-smhi-emissions.yml",
    client=lambda binding: SmhiClient(),
    discover=discover,
    fetch=fetch,
    options={"subject": "subjects"},
    languages=("sv",),
)
