"""Weekly SDB refresh with independent subject and language failures.

SDB publishes no modification dates and a full harvest is about 200 requests, so
every run refetches every subject in every language; identical responses keep
their stored bytes. Each language's subject listing is one scope. The result
probe's first-row request is each subject's endpoint check; the probe body is
stored once per subject as a shared file, the language-specific responses as the
subject's source entry.
"""

from apps.harvest.metadata_adapters.sdb.client import (
    NotFoundError,
    SdbClient,
    Unreachable,
)
from apps.harvest.metadata_adapters.sdb.mapping import PROVIDER_CODE
from apps.harvest.metadata_adapters.sdb.parse import probe_name
from apps.harvest.metadata_adapters.sdb.probe import fetch_probe
from apps.harvest.source_store import Rejected
from harvest.lifecycle import Adapter, Run, Scope, Stored, Task


def listing_rows(body, problems: list) -> tuple[list[dict], bool]:
    """Valid, unique subject rows; malformed or duplicate rows make it incomplete."""
    if not isinstance(body, list):
        raise ValueError(f"unexpected SDB subject listing {str(body)[:200]!r}")
    complete, found = True, {}
    for row in body:
        if not (
            isinstance(row, dict)
            and isinstance(row.get("namn"), str)
            and row["namn"]
            and isinstance(row.get("text"), str)
        ):
            problems.append((str(row)[:200], ValueError("Malformed subject row")))
            complete = False
            continue
        found.setdefault(row["namn"], []).append(row)
    rows = []
    for name, matches in found.items():
        if len(matches) > 1:
            problems.append((name, ValueError("Listed more than once")))
            complete = False
        else:
            rows.append(matches[0])
    return rows, complete


async def fetch_subject(client, row: dict, language: str, probes: dict) -> dict:
    """Fetch one subject's responses in ``language``; probe once per subject.

    Returns the envelope responses ``{"variables", "categories", "resultat",
    "listing_row"}``; ``probes[code]`` holds the shared raw probe afterwards.
    """
    code = row["namn"]
    try:
        variables = await client.get(language, code)
        categories = {
            str(v["namn"]): await client.get(language, code, str(v["namn"]))
            for v in variables
        }
        if code in probes:  # probed in another language; check this endpoint too
            first = await client.get(language, code, "resultat", params={"per_sida": 1})
        else:
            probes[code] = await fetch_probe(
                client, language, code, variables, categories
            )
            first = probes[code]["first"]
    except NotFoundError as error:
        raise Unreachable(str(error)) from error
    return {
        "variables": variables,
        "categories": categories,
        "resultat": first,
        "listing_row": row,
    }


async def discover(run: Run) -> list[Scope]:
    probes: dict = {}
    wanted = set(run.selection.get("subjects") or ()) or None
    scopes = []
    for language in run.languages:
        scope = Scope(language, None)

        async def load(language=language, scope=scope) -> list[Task]:
            body = await run.client.get(language)
            problems: list = []
            rows, complete = listing_rows(body, problems)
            if language == "sv" and not rows:
                raise ValueError("SDB lists no Swedish subjects")
            try:
                run.store.put_shared(
                    PROVIDER_CODE,
                    f"listing.{language}.json",
                    body,
                    retrieved_at=run.stamp,
                )
            except Rejected:
                run.log.warning("%s listing is empty; not stored", language)
            for resource, error in problems:
                run.fail("discovery", f"{language}/{resource}", error)
            if language == "sv":
                for code in sorted((wanted or set()) - {r["namn"] for r in rows}):
                    run.fail(
                        "discovery", f"*/{code}", LookupError("Unknown SDB subject")
                    )
            scope.complete = complete
            return [
                Task(f"{language}/{row['namn']}", (language, row, probes))
                for row in rows
                if wanted is None or row["namn"] in wanted
            ]

        scope.load = load
        scopes.append(scope)
    return scopes


async def fetch(run: Run, task: Task) -> Stored:
    language, row, probes = task.item
    responses = await fetch_subject(run.client, row, language, probes)
    code = row["namn"]
    if probes[code]["language"] == language:
        run.store.put_shared(
            PROVIDER_CODE, probe_name(code), probes[code], retrieved_at=run.stamp
        )
    return Stored(responses=responses)


def forget(run: Run, key: str) -> None:
    """Drop the shared probe once no language keeps the subject."""
    subject = key.split("/", 1)[1]
    if not any(k.split("/", 1)[1] == subject for k in run.known()):
        run.store.remove_shared(PROVIDER_CODE, probe_name(subject))


ADAPTER = Adapter(
    name="sdb",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-sdb.yml",
    client=lambda binding: SdbClient(binding.options, base_url=binding.endpoint_url),
    discover=discover,
    fetch=fetch,
    options={"subject": "subjects"},
    languages=("sv", "en"),
    collection="subjects",
    forget=forget,
    scope_of=lambda key: key.split("/", 1)[0],
)
