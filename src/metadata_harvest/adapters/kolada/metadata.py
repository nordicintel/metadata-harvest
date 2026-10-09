"""Native Kolada metadata mapping, without portal enrichment or observations."""

import hashlib
import json
import re

BASE_URL = "https://api.kolada.se/v3"
SCOPES = {"municipality": "data", "ou": "oudata"}
GENDERS = {"m": "Män", "k": "Kvinnor", "t": "Totalt"}


def reference_rows(rows: list[dict]) -> dict:
    result = {}
    for row in rows:
        row = dict(row)
        if isinstance(row.get("members"), str):
            row["members"] = json.loads(row["members"])
        if "members" in row and (
            not isinstance(row["members"], list)
            or any(
                not isinstance(m, dict)
                or not isinstance(m.get("member_id", m.get("id")), str)
                for m in row["members"]
            )
        ):
            raise ValueError(f"Malformed group members: {row.get('id')}")
        result[row["id"]] = row
    return result


def category(codes: list[str], labels: dict[str, str], label: str) -> dict:
    return {
        "label": label,
        "category": {
            "index": {code: position for position, code in enumerate(codes)},
            "label": {code: labels.get(code, code) for code in codes},
        },
    }


def context_for(kpi: dict, scope: str, coverage: dict, references: dict) -> dict:
    """Only referenced source metadata affects a document's comparison key."""
    entities = references["ou" if scope == "ou" else "municipality"]
    ids = coverage.get("ou_ids" if scope == "ou" else "municipality_ids", [])
    groups = references["municipality_groups"]
    return {
        "entities": {i: entities.get(i, {"id": i}) for i in ids},
        "municipality_groups": {
            i: groups.get(i, {"id": i})
            for i in coverage.get("municipality_group_ids", [])
        },
        "kpi_groups": {
            i: {k: v for k, v in group.items() if k != "members"}
            for i, group in references["kpi_groups"].items()
            if any(
                m.get("member_id", m.get("id")) == kpi["id"]
                for m in group.get("members", [])
            )
        },
    }


def context_digest(context: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            context, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def build_dataset(
    kpi: dict,
    scope: str,
    coverage: dict,
    references: dict,
    *,
    modified: str | None = None,
) -> dict:
    """Map current coverage; caller validates newly fetched replacements."""
    context = context_for(kpi, scope, coverage, references)
    years = [str(y) for y in sorted(coverage["years"])]
    entity_rows = {**context["entities"], **context["municipality_groups"]}
    areas = list(coverage.get("ou_ids" if scope == "ou" else "municipality_ids", []))
    areas += (
        coverage.get("municipality_group_ids", []) if scope == "municipality" else []
    )
    areas = sorted(set(areas))
    dimension = {
        "year": category(years, {}, "År"),
        scope: category(
            areas,
            {i: r.get("title", i) for i, r in entity_rows.items()},
            "Enhet" if scope == "ou" else "Kommun, region eller grupp",
        ),
    }
    if set(coverage.get("genders", [])) & {"M", "K", "m", "k"}:
        dimension["gender"] = category(list(GENDERS), GENDERS, "Kön")
    shared = {
        "provider_code": "kolada",
        "dataset_code": f"{scope}/{kpi['id']}",
        "language": "sv",
        "metadata_url": f"{BASE_URL}/kpi/{kpi['id']}",
        "data_url": f"{BASE_URL}/{SCOPES[scope]}/kpi/{kpi['id']}",
        "doc_url": f"{BASE_URL}/docs",
        "time_unit": "annual",
    }
    if years:
        shared.update(first_period=years[0], last_period=years[-1])
    if kpi.get("description"):
        shared["description"] = kpi["description"]
    result = {
        "version": "2.0",
        "class": "dataset",
        "label": kpi["title"],
        "id": list(dimension),
        "size": [len(d["category"]["index"]) for d in dimension.values()],
        "dimension": dimension,
        "role": {"time": ["year"]},
        "value": [],
        "extension": {
            "nordicintel": shared,
            "kolada": {
                "kpi_id": kpi["id"],
                "scope": scope,
                "source_record": kpi,
                "references_digest": context_digest(context),
                "kpi_groups": context["kpi_groups"],
                "municipality_groups": {
                    identifier: {
                        "title": group.get("title", identifier),
                        "members": [
                            m.get("member_id", m.get("id"))
                            for m in group.get("members", [])
                        ],
                    }
                    for identifier, group in context["municipality_groups"].items()
                },
                **(
                    {
                        "ou_municipality": {
                            i: row.get("municipality")
                            for i, row in context["entities"].items()
                        }
                    }
                    if scope == "ou"
                    else {}
                ),
                "status_labels": {
                    "m": "Missing",
                    "p": "Privacy",
                    "e": "Returned without a value or stated reason",
                },
            },
        },
    }
    if scope == "municipality":
        result["role"]["geo"] = [scope]
    if modified:
        result["updated"] = modified[:10]
    source = re.search(r"[Kk]äll(?:a|or)\s*:?\s*(.+)", kpi.get("description") or "")
    if source:
        result["source"] = source[1].split(". ")[0].rstrip(".")
    return result


async def fetch_metadata(record: dict, *, client) -> dict | None:
    """Fetch current coverage for one KPI scope; never persist observation values."""
    coverage = await client.coverage(
        record["scope"], record["kpi"]["id"], record["years"], record["references"]
    )
    if not coverage["years"]:
        return None
    return build_dataset(
        record["kpi"],
        record["scope"],
        coverage,
        record["references"],
        modified=record.get("modified"),
    )
