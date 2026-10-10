"""Loss-preserving conversion and assembly of statistical metadata.

The legacy reader is the transition boundary, not a second published contract.
"""

from __future__ import annotations

import calendar
import json
import re
from copy import deepcopy
from datetime import date
from urllib.parse import quote

from harvesting.models import (
    AdapterOptions,
    CodeList,
    Component,
    Concept,
    ContactPoint,
    Dataset,
    DataStructureDefinition,
    Dimension,
    Distribution,
    HarvestContribution,
    PeriodOfTime,
    QueryDimension,
    Reference,
    RetrievalBinding,
    SourceIdentity,
)
from harvesting.sources import deployment, resolve_source
from retrieval._semantics import category_codes, validate_cube


def stable_id(kind: str, *parts: str) -> str:
    return "urn:nordicintel:" + kind + ":" + ":".join(quote(p, safe="") for p in parts)


def language_text(value, language):
    return {language: [value]} if isinstance(value, str) else {language: value}


def _period(value, *, end=False):
    if not isinstance(value, str):
        return None
    if value.startswith("0000"):
        return None
    if re.fullmatch(r"\d{4}", value):
        return value + ("-12-31" if end else "-01-01")
    month = re.fullmatch(r"(\d{4})(?:M|-)(\d{2})", value)
    quarter = re.fullmatch(r"(\d{4})K([1-4])", value)
    if quarter:
        year, q = map(int, quarter.groups())
        m = q * 3 if end else (q - 1) * 3 + 1
        return date(year, m, calendar.monthrange(year, m)[1] if end else 1).isoformat()
    if month:
        year, m = map(int, month.groups())
        try:
            return date(
                year, m, calendar.monthrange(year, m)[1] if end else 1
            ).isoformat()
        except ValueError:
            return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


RESOLUTIONS = {
    "annual": "P1Y",
    "quarterly": "P3M",
    "monthly": "P1M",
    "weekly": "P1W",
    "daily": "P1D",
}
ROOT_FIELDS = {
    "label": "dct:title",
    "updated": "dct:modified",
    "note": "skos:note",
    "dimension": "statdcatap:dimension",
    "id": "qb:component/qb:order",
    "size": "ni:retrieval/dimensions/categories",
    "role": "statdcatap:dimension/ni:role",
    "class": "@type",
    "version": "ni:profileVersion",
    "value": "metadata-only invariant",
    "extension": "mapped by namespace",
}
SHARED_FIELDS = {
    "provider_code": "ni:retrieval/source/provider_code",
    "dataset_code": "dct:identifier",
    "language": "language tags and ni:retrieval/source/language",
    "description": "dct:description",
    "data_url": "dcat:distribution/dcat:accessURL",
    "metadata_url": "ni:retrieval/metadata_url",
    "source_url": "dcat:landingPage",
    "doc_url": "foaf:page",
    "first_period": "dct:temporal/dcat:startDate",
    "last_period": "dct:temporal/dcat:endDate",
    "time_unit": "dcat:temporalResolution",
    "official_statistics": "ni:officialStatistics",
    "discontinued": "ni:discontinued",
    "next_release": "ni:nextRelease",
    "contacts": "dcat:contactPoint",
    "subject": "dcat:theme",
}


def convert_dataset(document: dict, *, adapter: str) -> HarvestContribution:
    """Convert one accepted source contribution, without changing the input."""
    if document.get("@type") == "dcat:Dataset":
        raise ValueError("Already assembled; use Dataset.from_jsonld")
    validate_cube(document)
    if document.get("value") != [] or "status" in document:
        raise ValueError("Metadata conversion cannot contain observations")
    shared = deepcopy(document["extension"]["nordicintel"])
    source = SourceIdentity(
        adapter=adapter,
        **{k: shared[k] for k in ("provider_code", "dataset_code", "language")},
    )
    language = source.language
    code = source.dataset_code
    if adapter == "kolada":
        code = (
            document["extension"].get("kolada", {}).get("kpi_id")
            or code.split("/", 1)[-1]
        )
    registered = deployment(adapter, source.provider_code)
    owner_code = (
        registered["owner"]["code"] if registered["owner"] else source.provider_code
    )
    identity_parts = (
        [owner_code, code]
        if source.provider_code == owner_code
        else [owner_code, source.provider_code, code]
    )
    if adapter == "entrysoft":
        identity_parts = [owner_code, "rowstore", code]
    dataset_id = stable_id("dataset", *identity_parts)
    source_key = owner_code if adapter == "entrysoft" else source.provider_code
    distribution_id = stable_id(
        "distribution",
        source_key,
        adapter,
        source.dataset_code,
        language,
    )
    roles = {
        code: role for role, codes in document.get("role", {}).items() for code in codes
    }
    dimensions, queries, schemes = [], [], []
    for code in document["id"]:
        native = deepcopy(document["dimension"][code])
        category = native.pop("category")
        codes = category_codes(category)
        scheme_id = stable_id(
            "codelist",
            source_key,
            adapter,
            source.dataset_code,
            code,
        )
        children = category.get("child", {})
        concepts = []
        for item in codes:
            concepts.append(
                Concept(
                    id=scheme_id + ":" + quote(item, safe=""),
                    code=item,
                    label=language_text(category["label"][item], language)
                    if item in category.get("label", {})
                    else {},
                    notes=language_text(category["note"][item], language)
                    if item in category.get("note", {})
                    else {},
                    unit=category.get("unit", {}).get(item),
                    coordinates=category.get("coordinates", {}).get(item),
                    broader=[
                        scheme_id + ":" + quote(parent, safe="")
                        for parent, values in children.items()
                        if item in values
                    ],
                )
            )
        category_extras = {
            k: v
            for k, v in category.items()
            if k not in {"index", "label", "note", "unit", "coordinates", "child"}
        }
        scheme = CodeList(
            id=scheme_id,
            label=language_text(native.get("label", code), language),
            codes=concepts,
            source_identity={
                "provider_code": source.provider_code,
                "dataset_code": source.dataset_code,
                "dimension": code,
            },
            additional_attributes={adapter: category_extras} if category_extras else {},
        )
        # The original category order is operational and independent of RDF order.
        queries.append(
            QueryDimension(
                code=code,
                categories=codes,
                index_encoding="dict"
                if isinstance(category.get("index"), dict)
                else "list"
                if "index" in category
                else "omitted",
            )
        )
        extras = native.pop("extension", {})
        canonical = extras.pop("nordicintel", {})
        kwargs = {}
        for key in ("elimination", "elimination_value"):
            if key in canonical:
                kwargs[key] = canonical.pop(key)
        dim = Dimension(
            id=stable_id(
                "dimension",
                source_key,
                adapter,
                source.dataset_code,
                code,
            ),
            code=code,
            label=language_text(native.pop("label"), language)
            if "label" in native
            else {},
            notes=language_text(native.pop("note"), language)
            if "note" in native
            else {},
            code_list=scheme,
            role=roles.get(code),
            links=native.pop("link", {}),
            additional_attributes={
                **extras,
                **({"nordicintel": canonical} if canonical else {}),
                **({adapter + ":fields": native} if native else {}),
            },
            **kwargs,
        )
        dimensions.append(dim)
        schemes.append(scheme)
    structure = DataStructureDefinition(
        id=distribution_id + ":structure",
        components=[
            Component(dimension=d, order=i) for i, d in enumerate(dimensions, 1)
        ],
    )
    diagnostics = []
    temporal = {}
    notation = {}
    for key, dest in (("first_period", "start_date"), ("last_period", "end_date")):
        converted = _period(shared.get(key), end=key == "last_period")
        if converted:
            temporal[dest] = converted
            value = shared.pop(key)
            notation[dest] = (
                "year"
                if len(value) == 4
                else "quarter"
                if "K" in value
                else "month-M"
                if "M" in value
                else "month"
                if len(value) == 7
                else "date"
            )
    try:
        periods = (
            [PeriodOfTime(**temporal, additional_attributes={"source": notation})]
            if temporal
            else []
        )
    except ValueError:
        periods = []
        for key in ("first_period", "last_period"):
            if key in document["extension"]["nordicintel"]:
                shared[key] = document["extension"]["nordicintel"][key]
        diagnostics.append("Malformed optional temporal coverage preserved")
    resolution = RESOLUTIONS.get(shared.get("time_unit"))
    if resolution:
        shared.pop("time_unit")
    title = language_text(document["label"], language)
    description = (
        language_text(shared.pop("description"), language)
        if isinstance(shared.get("description"), str)
        else {}
    )
    flags = {}
    for key in ("official_statistics", "discontinued"):
        if type(shared.get(key)) is bool:
            flags[key] = shared.pop(key)
    contacts = []
    raw_contacts = shared.get("contacts", [])
    if isinstance(raw_contacts, list) and all(
        isinstance(c, dict) for c in raw_contacts
    ):
        shared.pop("contacts", None)
        for original_contact in raw_contacts:
            contact = deepcopy(original_contact)
            kwargs = {}
            for old, new in (
                ("name", "name"),
                ("email", "email"),
                ("phone", "telephone"),
                ("organization", "organisation"),
                ("address", "address"),
                ("url", "url"),
            ):
                if old in contact and (
                    old == "address"
                    or contact[old] is None
                    or isinstance(contact[old], str)
                ):
                    kwargs[new] = contact.pop(old)
                elif old in contact:
                    diagnostics.append("Malformed optional contact property: " + old)
            contacts.append(
                ContactPoint(
                    **kwargs,
                    additional_attributes={adapter: contact} if contact else {},
                )
            )
    elif "contacts" in shared:
        diagnostics.append("Malformed optional contacts preserved")
    subject = shared.get("subject")
    if (
        isinstance(subject, dict)
        and isinstance(subject.get("code", subject.get("label")), str)
        and subject.get("code", subject.get("label"))
    ):
        shared.pop("subject")
    else:
        if subject is not None:
            diagnostics.append("Malformed optional subject preserved")
        subject = None
    themes = (
        [
            Concept(
                code=subject.get("code", subject.get("label", "")),
                label=language_text(subject["label"], language)
                if subject.get("label")
                else {},
                additional_attributes={
                    adapter: {
                        k: v for k, v in subject.items() if k not in {"code", "label"}
                    }
                },
            )
        ]
        if subject
        else []
    )
    data_url = shared.pop("data_url", None)
    metadata_url = shared.pop("metadata_url", None)
    landing_page = [shared.pop("source_url")] if shared.get("source_url") else []
    documentation = [shared.pop("doc_url")] if shared.get("doc_url") else []
    next_release = shared.get("next_release")
    if isinstance(next_release, str) or next_release is None:
        shared.pop("next_release", None)
    else:
        next_release = None
        diagnostics.append("Malformed optional next release preserved")
    for key in ("provider_code", "dataset_code", "language"):
        shared.pop(key, None)
    options = AdapterOptions.model_validate(
        deepcopy(document["extension"].get(adapter, {}))
    )
    native_extensions = {
        k: v
        for k, v in document["extension"].items()
        if k not in {"nordicintel", adapter}
    }
    root_extras = {k: deepcopy(v) for k, v in document.items() if k not in ROOT_FIELDS}
    evidence = {"adapter_namespace_present": adapter in document["extension"]}
    if diagnostics:
        evidence["diagnostics"] = diagnostics
    if isinstance(subject, dict) and "code" not in subject:
        evidence["subject_code_absent"] = True
    empty_values = {}

    def remember_empty(value, path=()):
        if value is None or value == [] or value == {} or value == "":
            empty_values[
                "/".join(str(k).replace("~", "~0").replace("/", "~1") for k in path)
            ] = value
        elif isinstance(value, dict):
            for k, child in value.items():
                remember_empty(child, (*path, k))
        elif isinstance(value, list):
            for k, child in enumerate(value):
                remember_empty(child, (*path, k))

    remember_empty(document)
    if empty_values:
        evidence["empty_values"] = empty_values
    if native_extensions:
        evidence["extensions"] = native_extensions
    if root_extras:
        evidence["document_fields"] = root_extras
    record = document["extension"].get(adapter, {}).get("source_record", {})
    if not record:
        record = next(
            (
                v.get("source_record", {})
                for v in document["extension"].values()
                if isinstance(v, dict) and v.get("source_record")
            ),
            {},
        )
    catalogs, service = resolve_source(source, data_url, record, dataset_id=dataset_id)
    distribution = Distribution(
        id=distribution_id,
        modified=document.get("updated"),
        notes=language_text(document["note"], language) if "note" in document else {},
        temporal_resolution=[resolution] if resolution else [],
        landing_page=landing_page,
        documentation=documentation,
        contacts=contacts,
        themes=themes,
        next_release=next_release,
        title=title,
        description=description,
        access_url=[data_url] if data_url else [],
        services=[service],
        structure=structure,
        retrieval=RetrievalBinding(
            source=source,
            dimensions=queries,
            metadata_url=metadata_url,
            options=options,
        ),
        temporal=periods,
        source_evidence=evidence,
        additional_attributes={"nordicintel": shared} if shared else {},
        **flags,
    )
    dataset = Dataset(
        id=dataset_id,
        identifiers=[
            source.dataset_code
            if adapter != "kolada"
            else source.dataset_code.split("/", 1)[-1]
        ],
        catalogs=catalogs,
        source_evidence={
            "attribution_findings": [
                {
                    "distribution": distribution_id,
                    "code": "catalog_attribution_missing",
                    "source_code": source.source_code,
                }
            ]
        }
        if not catalogs
        else {},
        title=title,
        description=description,
        modified=document.get("updated"),
        notes=language_text(document["note"], language) if "note" in document else {},
        temporal=periods,
        temporal_resolution=[resolution] if resolution else [],
        landing_page=landing_page,
        documentation=documentation,
        contacts=contacts,
        themes=themes,
        dimensions=[Reference(id=dimension.id) for dimension in dimensions],
        structure=Reference(id=structure.id),
        distributions=[distribution],
        next_release=next_release,
        additional_attributes={"nordicintel": shared} if shared else {},
        **flags,
    )
    audit = {}

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            if not value:
                audit[path] = "preserved empty value or metadata-only invariant"
            for i, child in enumerate(value):
                walk(child, path + "/" + str(i))
        else:
            parts = path.split("/")
            field = parts[1]
            if path.startswith("/extension/nordicintel/"):
                name = parts[3]
                audit[path] = SHARED_FIELDS.get(
                    name, "ni:additionalAttributes/nordicintel"
                )
                if name in shared:
                    audit[path] = "ni:additionalAttributes/nordicintel"
            elif field == "dimension":
                audit[path] = (
                    "ni:structure/qb:component/qb:dimension (typed fields and source attributes)"
                )
            elif path.startswith("/extension/" + adapter + "/"):
                audit[path] = (
                    "ni:retrieval/options (typed fields and source attributes)"
                )
            else:
                audit[path] = ROOT_FIELDS.get(
                    field, "ni:sourceEvidence/document_fields"
                )

    walk(document)
    return HarvestContribution(
        source=source, dataset=dataset, codelists=schemes, attribute_mapping=audit
    )


def _extra(model):
    values = dict(model.additional_attributes.get("custom", {}))
    values.update(model.model_extra or {})
    return values


def legacy_document(distribution: Distribution, dataset: Dataset | None = None) -> dict:
    """Compatibility projection for existing observation retrievers and checkpoints."""
    binding = distribution.retrieval
    if binding is None or distribution.structure is None:
        raise ValueError("Distribution has no implemented retrieval binding")
    source = binding.source
    lang = source.language
    result = {
        "class": "dataset",
        "version": "2.0",
        "value": [],
        "id": [],
        "size": [],
        "dimension": {},
    }
    result.update(deepcopy(distribution.source_evidence.get("document_fields", {})))
    dimensions = {
        c.dimension.code: c.dimension for c in distribution.structure.components
    }
    roles = {}
    for query in binding.dimensions:
        dimension = dimensions[query.code]
        result["id"].append(query.code)
        result["size"].append(len(query.categories))
        codes = {code.code: code for code in dimension.code_list.codes}
        category = {}
        if query.index_encoding != "omitted":
            category["index"] = (
                {v: i for i, v in enumerate(query.categories)}
                if query.index_encoding == "dict"
                else list(query.categories)
            )
        for key, attr in (
            ("label", "label"),
            ("note", "notes"),
            ("unit", "unit"),
            ("coordinates", "coordinates"),
        ):
            values = {}
            for code, concept in codes.items():
                value = getattr(concept, attr)
                if key in {"label", "note"}:
                    value = value.get(lang)
                    if value and key == "label":
                        value = value[0]
                if value is not None and value != {}:
                    if value != []:
                        values[code] = value
            if values:
                category[key] = values
        parents = {c.id: c.code for c in codes.values()}
        for code, concept in codes.items():
            for parent in concept.broader:
                category.setdefault("child", {}).setdefault(parents[parent], []).append(
                    code
                )
        for values in dimension.code_list.additional_attributes.values():
            category.update(deepcopy(values))
        item = {"category": category}
        if dimension.label.get(lang):
            item["label"] = dimension.label[lang][0]
        if dimension.notes.get(lang):
            item["note"] = dimension.notes[lang]
        if dimension.links:
            item["link"] = deepcopy(dimension.links)
        extensions = deepcopy(dimension.additional_attributes)
        item.update(extensions.pop(source.adapter + ":fields", {}))
        for key in ("elimination", "elimination_value"):
            value = getattr(dimension, key)
            if value is not None:
                extensions.setdefault("nordicintel", {})[key] = value
        if extensions:
            item["extension"] = extensions
        result["dimension"][query.code] = item
        if dimension.role:
            roles.setdefault(dimension.role, []).append(query.code)
    if roles:
        result["role"] = roles
    shared = deepcopy(distribution.additional_attributes.get("nordicintel", {}))
    shared.update(
        provider_code=source.provider_code,
        dataset_code=source.dataset_code,
        language=lang,
    )
    if distribution.access_url:
        shared["data_url"] = distribution.access_url[0]
    if binding.metadata_url:
        shared["metadata_url"] = binding.metadata_url
    for key in ("official_statistics", "discontinued"):
        value = getattr(distribution, key)
        if value is not None:
            shared[key] = value
    description = distribution.description.get(lang)
    if description:
        shared["description"] = description[0]
    options = binding.options.model_dump(exclude_none=True)
    options.pop("additional_attributes", None)
    options.update(_extra(binding.options))
    extensions = deepcopy(distribution.source_evidence.get("extensions", {}))
    extensions.update(nordicintel=shared)
    extensions[source.adapter] = options
    result["extension"] = extensions
    result["label"] = distribution.title.get(lang, [""])[0]
    if dataset is not None:
        dataset = distribution
        if dataset.next_release is not None:
            shared["next_release"] = dataset.next_release
        if dataset.contacts:
            contacts = []
            for contact in dataset.contacts:
                values = {}
                for old, new in (
                    ("name", "name"),
                    ("email", "email"),
                    ("telephone", "phone"),
                    ("organisation", "organization"),
                    ("address", "address"),
                    ("url", "url"),
                ):
                    if (value := getattr(contact, old)) is not None:
                        values[new] = value
                for extras in contact.additional_attributes.values():
                    values.update(deepcopy(extras))
                contacts.append(values)
            shared["contacts"] = contacts
        if dataset.themes:
            theme = dataset.themes[0]
            shared["subject"] = {"code": theme.code}
            if theme.label.get(lang):
                shared["subject"]["label"] = theme.label[lang][0]
            for extras in theme.additional_attributes.values():
                shared["subject"].update(deepcopy(extras))
        if dataset.modified:
            result["updated"] = dataset.modified
        if dataset.notes.get(lang):
            result["note"] = dataset.notes[lang]
        if dataset.landing_page:
            shared["source_url"] = dataset.landing_page[0]
        if dataset.documentation:
            shared["doc_url"] = dataset.documentation[0]
        for period in distribution.temporal:
            for field, bound in (
                ("first_period", period.start_date),
                ("last_period", period.end_date),
            ):
                if bound and field not in shared:
                    notation = period.additional_attributes.get("source", {}).get(
                        "start_date" if field == "first_period" else "end_date"
                    )
                    shared[field] = (
                        bound[:4]
                        if notation == "year"
                        else bound[:4] + "K" + str((int(bound[5:7]) - 1) // 3 + 1)
                        if notation == "quarter"
                        else bound[:4] + "M" + bound[5:7]
                        if notation == "month-M"
                        else bound[:7]
                        if notation == "month"
                        else bound
                    )
        if dataset.temporal_resolution:
            inverse = {v: k for k, v in RESOLUTIONS.items()}
            if unit := inverse.get(dataset.temporal_resolution[0]):
                shared["time_unit"] = unit
    if not distribution.source_evidence.get("adapter_namespace_present", True):
        result["extension"].pop(source.adapter, None)
    if distribution.source_evidence.get("subject_code_absent") and "subject" in shared:
        shared["subject"].pop("code", None)
    for path, value in distribution.source_evidence.get("empty_values", {}).items():
        parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")]
        target = result
        for part in parts[:-1]:
            if isinstance(target, list):
                target = target[int(part)]
            else:
                target = target.setdefault(part, {})
        if isinstance(target, list):
            target[int(parts[-1])] = deepcopy(value)
        else:
            target[parts[-1]] = deepcopy(value)
    return result


def _union(left, right):
    values = {
        json.dumps(
            v.model_dump(mode="json") if hasattr(v, "model_dump") else v,
            sort_keys=True,
            ensure_ascii=False,
        ): v
        for v in [*left, *right]
    }
    return [values[key] for key in sorted(values)]


def _merge_text(target, source):
    for lang, texts in source.items():
        target[lang] = list(dict.fromkeys(target.get(lang, []) + texts))


def assemble(
    contributions: list[HarvestContribution], *, include_codelists=True
) -> tuple[list[Dataset], list[CodeList]]:
    """Combine languages and scopes; retain each independently replaceable contribution."""
    groups, schemes = {}, {}
    for contribution in sorted(
        contributions, key=lambda c: (c.dataset.id, c.dataset.distributions[0].id)
    ):
        current = groups.setdefault(contribution.dataset.id, [])
        current.append(contribution.dataset)
        for scheme in contribution.codelists if include_codelists else ():
            if scheme.id not in schemes:
                schemes[scheme.id] = scheme.model_copy(deep=True)
                continue
            schemes[scheme.id] = merge_codelists(schemes[scheme.id], scheme)
    datasets = []
    for _identity, parts in sorted(groups.items()):
        base = parts[0].model_copy(deep=True)
        for part in parts[1:]:
            for key, value in part.source_evidence.items():
                if key not in base.source_evidence:
                    base.source_evidence[key] = deepcopy(value)
                elif isinstance(value, list) and isinstance(
                    base.source_evidence[key], list
                ):
                    base.source_evidence[key] = _union(base.source_evidence[key], value)
                elif base.source_evidence[key] != value:
                    base.source_evidence.setdefault(
                        "conflicting_assertions", []
                    ).append({"property": key, "value": deepcopy(value)})
            for key in ("title", "description", "notes", "keywords"):
                _merge_text(getattr(base, key), getattr(part, key))
            for key in (
                "catalogs",
                "identifiers",
                "temporal",
                "temporal_resolution",
                "landing_page",
                "documentation",
                "contacts",
                "themes",
                "dimensions",
                "distributions",
            ):
                setattr(base, key, _union(getattr(base, key), getattr(part, key)))
            if part.modified and (
                base.modified is None or part.modified > base.modified
            ):
                base.modified = part.modified
        for key in ("official_statistics", "discontinued"):
            values = {getattr(part, key) for part in parts}
            setattr(base, key, values.pop() if len(values) == 1 else None)
        # Distribution-level structures remain authoritative, even across translations.
        base.structure = (
            Reference(id=base.distributions[0].structure.id)
            if len(base.distributions) == 1
            and base.distributions[0].structure is not None
            else None
        )
        datasets.append(base)
    return datasets, [schemes[k] for k in sorted(schemes)]


def convert_codelist(record: dict) -> CodeList:
    """Normalize native codelist attributes without treating aggregation as hierarchy."""
    native = deepcopy(record["codelist"])
    provider, language = record["provider_code"], record["language"]
    identifier = native.pop("id")
    scheme_id = stable_id("codelist", provider, "native", identifier)
    codes = []
    for entry in native.pop("values", []):
        entry = deepcopy(entry)
        code = entry.pop("code")
        label = entry.pop("label", None)
        mapping = entry.pop("valueMap", None)
        codes.append(
            Concept(
                id=scheme_id + ":" + quote(code, safe=""),
                code=code,
                label=language_text(label, language) if label is not None else {},
                aggregation_map=mapping,
                additional_attributes={"source": entry} if entry else {},
            )
        )
    label = native.pop("label", None)
    native.pop("language", None)
    return CodeList(
        id=scheme_id,
        label=language_text(label, language) if label is not None else {},
        codes=codes,
        source_identity={
            "provider_code": provider,
            "native_id": identifier,
            "languages": [language],
        },
        additional_attributes={
            "source": native,
            "envelope": {
                k: v
                for k, v in record.items()
                if k not in {"provider_code", "language", "codelist"}
            },
        },
    )


def merge_codelists(left: CodeList, right: CodeList) -> CodeList:
    """Join language labels and retain genuinely conflicting non-language assertions."""
    if left.id != right.id:
        raise ValueError("Cannot merge different codelist identities")
    result = left.model_copy(deep=True)
    _merge_text(result.label, right.label)
    codes = {c.code: c for c in result.codes}
    for incoming in right.codes:
        if incoming.code not in codes:
            result.codes.append(incoming.model_copy(deep=True))
            continue
        code = codes[incoming.code]
        _merge_text(code.label, incoming.label)
        _merge_text(code.notes, incoming.notes)
        for field in (
            "unit",
            "coordinates",
            "aggregation_map",
            "broader",
            "additional_attributes",
        ):
            a, b = getattr(code, field), getattr(incoming, field)
            if (
                json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)
                and b is not None
            ):
                _retain_conflict(code.additional_attributes, field, a, b)
    for namespace, values in right.additional_attributes.items():
        target = result.additional_attributes.setdefault(namespace, {})
        for key, value in values.items():
            if key not in target:
                target[key] = deepcopy(value)
            elif json.dumps(target[key], sort_keys=True) != json.dumps(
                value, sort_keys=True
            ):
                _retain_conflict(
                    result.additional_attributes,
                    namespace + "/" + key,
                    target[key],
                    value,
                )
    for language, order in right.category_order.items():
        if language not in result.category_order:
            result.category_order[language] = list(order)
        elif result.category_order[language] != order:
            _retain_conflict(
                result.additional_attributes,
                "category_order/" + language,
                result.category_order[language],
                order,
            )
    languages = sorted(
        set(
            left.source_identity.get("languages", [])
            + right.source_identity.get("languages", [])
        )
    )
    if languages:
        result.source_identity["languages"] = languages
    return result


def _retain_conflict(attributes, key, left, right):
    # Snapshot before changing the container: left may be the container itself.
    values = deepcopy(attributes.get("conflicts", {}).get(key, []) + [left, right])
    unique = {}
    for value in values:
        unique.setdefault(json.dumps(value, sort_keys=True), value)
    attributes.setdefault("conflicts", {})[key] = list(unique.values())
