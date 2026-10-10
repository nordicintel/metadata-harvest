"""Explicit, lossless profile 1 reader and profile 2 metadata transformation."""

from copy import deepcopy

from apps.configuration import load_configuration
from apps.harvest.sources import resolve_source, service_for_url
from apps.model.models import Dataset, SourceIdentity
from apps.model.vocabulary import CONTEXT, PROFILE1_CONTEXT


def normalize_agent_homepages(document: dict) -> None:
    """Correct profile 1's known Agent.homepage alias without touching source evidence."""
    if document.get("ni:profileVersion") != "1.0.0":
        return

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("@type") == "foaf:Agent" and "foaf:page" in value:
                previous = value.pop("foaf:page")
                value["foaf:homepage"] = list(
                    dict.fromkeys(value.get("foaf:homepage", []) + previous)
                )
            for key, item in value.items():
                if not key.startswith("ni:"):
                    visit(item)

    visit(document)


def upgrade_document(document: dict) -> tuple[dict, dict]:
    """Change configuration-derived relationships while preserving source payloads.

    Statistical structures, codes and operational options are never reconstructed.
    The ledger records replaced graph identities; source identifiers remain exact.
    """
    if document.get("ni:profileVersion") not in {"1.0.0", "2.0.0"}:
        raise ValueError("Unsupported source metadata profile")
    result = deepcopy(document)
    normalize_agent_homepages(result)
    if document.get("@context") not in (None, CONTEXT, PROFILE1_CONTEXT):
        raise ValueError("Unsupported source JSON-LD context")
    result["@context"] = CONTEXT
    result["ni:profileVersion"] = "2.0.0"
    catalogs = {}
    services = []
    old_catalogs = {c["@id"]: c for c in document.get("catalogs", [])}
    findings = []
    for distribution in result.get("dcat:distribution", []):
        binding = distribution.get("ni:retrieval")
        old_services = distribution.get("dcat:accessService", [])
        if binding:
            source = SourceIdentity.model_validate(binding["source"])
            binding["source"] = source.model_dump(mode="json", exclude_none=True)
            options = binding.get("options", {})
            extras = options.get(
                "ni:additionalAttributes", options.get("additional_attributes", {})
            )
            native = options.get("source_record")
            if native is None:
                native = next(
                    (
                        v["source_record"]
                        for v in extras.values()
                        if isinstance(v, dict) and "source_record" in v
                    ),
                    {},
                )
            urls = distribution.get("dcat:accessURL", [])
            selected, service = resolve_source(
                source, urls[0] if urls else None, native, dataset_id=result["@id"]
            )
            for catalog in selected:
                value = catalog.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                previous = old_catalogs.get(catalog.id, {})
                # Preserve descriptive assertions and unfamiliar properties from the old record.
                for key, item in previous.items():
                    if key not in value:
                        value[key] = item
                    elif key in {"dct:title", "dct:description"}:
                        for lang, texts in item.items():
                            value[key][lang] = list(
                                dict.fromkeys(value[key].get(lang, []) + texts)
                            )
                catalogs[catalog.id] = value
                if any(
                    item not in (None, [], {}) and value.get(key) != item
                    for key, item in previous.items()
                ):
                    result.setdefault("ni:sourceEvidence", {}).setdefault(
                        "previousCatalogAssertions", []
                    ).append(previous)
            if not selected:
                findings.append(
                    {
                        "distribution": distribution["@id"],
                        "code": "catalog_attribution_missing",
                        "source_code": source.source_code,
                    }
                )
            new_services = [
                service.model_dump(mode="json", by_alias=True, exclude_none=True)
            ]
        else:
            new_services = []
            urls = distribution.get("dcat:accessURL", []) + distribution.get(
                "dcat:downloadURL", []
            )
            for old in old_services:
                adapter = old.get("ni:adapter")
                if adapter:
                    service = service_for_url(adapter, next(iter(urls), None))
                    new_services.append(
                        service.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                    )
        for old in old_services:
            for new in new_services:
                if old.get("ni:adapter") == new.get("ni:adapter"):
                    # Profile 1 services normally contained only adapter-level metadata.
                    for key, value in old.items():
                        if key not in new:
                            new[key] = value
                    if any(
                        key != "@id"
                        and value not in (None, [], {})
                        and new.get(key) != value
                        for key, value in old.items()
                    ):
                        result.setdefault("ni:sourceEvidence", {}).setdefault(
                            "previousServiceAssertions", []
                        ).append({"distribution": distribution["@id"], "service": old})
        distribution["dcat:accessService"] = new_services
        services.append(
            {
                "distribution": distribution["@id"],
                "old": [s["@id"] for s in old_services],
                "new": [s["@id"] for s in new_services],
            }
        )
    # Publication filtering can remove every harvested distribution while the
    # conceptual dataset still belongs to a known catalog. Keep that membership.
    configuration = load_configuration()
    for identity, previous in old_catalogs.items():
        if identity in catalogs:
            continue
        configured = next(
            (
                c
                for c in configuration.catalogs.values()
                if identity == c.id or identity.startswith(c.id + ":")
            ),
            None,
        )
        if configured:
            catalogs[identity] = deepcopy(previous)
            catalogs[identity]["dcat:service"] = [
                {"@id": s} for s in configured.services
            ]
            if configured.publisher:
                catalogs[identity]["dct:publisher"] = [
                    configuration.organisations[configured.publisher].model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                ]
        else:
            # A replaced host-level membership is evidence of the correction,
            # not a valid catalog assertion to duplicate in the new graph.
            result.setdefault("ni:sourceEvidence", {}).setdefault(
                "previousCatalogAssertions", []
            ).append(previous)
    result["catalogs"] = list(catalogs.values())
    if findings:
        result.setdefault("ni:sourceEvidence", {})["attribution_findings"] = findings
    return result, {
        "dataset": result["@id"],
        "old_catalogs": list(old_catalogs),
        "new_catalogs": list(catalogs),
        "services": services,
        "findings": findings,
        "property_mapping": {"Agent.foaf:page": "Agent.foaf:homepage"},
    }


def read_dataset(document: dict) -> Dataset:
    """Read either supported profile explicitly; current validators stay strict."""
    if document.get("ni:profileVersion") == "1.0.0":
        document, _ = upgrade_document(document)
    return Dataset.from_jsonld(document)
