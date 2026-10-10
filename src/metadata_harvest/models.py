"""NordicIntel's extensible StatDCAT profile; no network or persistence in models."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    model_serializer,
    model_validator,
)

PROFILE_VERSION = "1.0.0"
STATDCAT_REVISION = "3.0.0/0ab07dfa6a5c3cf173e7d1c4a0a0f2711078872e"
NAMESPACE = "https://github.com/nordicintel/metadata-harvest/ns#"
CONTEXT = {
    "@version": 1.1,
    "dcat": "http://www.w3.org/ns/dcat#",
    "dct": "http://purl.org/dc/terms/",
    "statdcatap": "http://data.europa.eu/s1n/",
    "qb": "http://purl.org/linked-data/cube#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "vcard": "http://www.w3.org/2006/vcard/ns#",
    "prov": "http://www.w3.org/ns/prov#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "adms": "http://www.w3.org/ns/adms#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "dcatap": "http://data.europa.eu/r5r/",
    "ni": NAMESPACE,
    **{
        key: {"@id": key, "@container": "@language"}
        for key in (
            "dct:title",
            "dct:description",
            "skos:prefLabel",
            "skos:note",
            "foaf:name",
            "dcat:keyword",
            "rdfs:label",
            "adms:versionNotes",
        )
    },
    **{
        key: {"@id": key, "@type": "@id"}
        for key in (
            "dcat:accessURL",
            "dcat:downloadURL",
            "dcat:endpointURL",
            "dcat:endpointDescription",
            "dcat:landingPage",
            "foaf:page",
            "dct:license",
            "dct:conformsTo",
            "skos:inScheme",
            "skos:broader",
        )
    },
    **{
        key: {"@id": key, "@type": "@json"}
        for key in (
            "ni:additionalAttributes",
            "ni:retrieval",
            "ni:sourceEvidence",
            "ni:matchEvidence",
            "ni:sourceIdentity",
            "ni:unit",
            "ni:coordinates",
            "ni:categoryOrder",
            "ni:aggregationMap",
            "ni:links",
            "ni:sourceAssertions",
        )
    },
    "codes": {"@reverse": "skos:inScheme"},
    "catalogs": {"@reverse": "dcat:dataset"},
    "dcat:startDate": {"@id": "dcat:startDate", "@type": "xsd:date"},
    "dcat:endDate": {"@id": "dcat:endDate", "@type": "xsd:date"},
    "dcat:temporalResolution": {
        "@id": "dcat:temporalResolution",
        "@type": "xsd:duration",
    },
}
Text = Annotated[str, Field(strict=True, min_length=1)]
LanguageMap = dict[str, list[str]]


class ExtensibleModel(BaseModel):
    """Typed known fields, with lossless JSON-valued source/custom extensions."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, validate_assignment=True
    )
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    additional_attributes: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, alias="ni:additionalAttributes"
    )

    @model_serializer(mode="wrap")
    def serialize_extensions(self, handler):
        result = handler(self)
        attributes = {
            key: dict(value) for key, value in self.additional_attributes.items()
        }
        if self.model_extra:
            custom = attributes.setdefault("custom", {})
            for key, value in self.model_extra.items():
                if key in custom and custom[key] != value:
                    raise ValueError(f"Conflicting custom attribute: {key}")
                custom[key] = result.pop(key, value)
        field = (
            "ni:additionalAttributes"
            if "ni:additionalAttributes" in result
            else "additional_attributes"
        )
        if attributes:
            result[field] = attributes
        else:
            result.pop(field, None)
        return result

    def to_jsonld(self) -> dict:
        """Serialize independently readable JSON-LD without remote contexts."""
        return {
            "@context": CONTEXT,
            **self.model_dump(mode="json", by_alias=True, exclude_none=True),
        }

    @classmethod
    def from_jsonld(cls, value: dict) -> Self:
        value = dict(value)
        context = value.pop("@context", None)
        if context is not None and context != CONTEXT:
            raise ValueError(
                "Unsupported JSON-LD context; expand/map it before importing"
            )
        return cls.model_validate(value)


class Reference(ExtensibleModel):
    id: Text = Field(alias="@id")


class Agent(ExtensibleModel):
    id: str | None = Field(default=None, alias="@id")
    type: Literal["foaf:Agent"] = Field(default="foaf:Agent", alias="@type")
    name: LanguageMap = Field(default_factory=dict, alias="foaf:name")
    email: str | None = Field(default=None, alias="foaf:mbox")
    homepage: list[str] = Field(default_factory=list, alias="foaf:page")


class ContactPoint(ExtensibleModel):
    type: Literal["vcard:Kind"] = Field(default="vcard:Kind", alias="@type")
    name: str | None = Field(default=None, alias="vcard:fn")
    email: str | None = Field(default=None, alias="vcard:hasEmail")
    telephone: str | None = Field(default=None, alias="vcard:hasTelephone")
    organisation: str | None = Field(default=None, alias="vcard:organization-name")
    address: JsonValue = Field(default=None, alias="vcard:hasAddress")
    url: str | None = Field(default=None, alias="vcard:hasURL")


class PeriodOfTime(ExtensibleModel):
    type: Literal["dct:PeriodOfTime"] = Field(default="dct:PeriodOfTime", alias="@type")
    start_date: str | None = Field(default=None, alias="dcat:startDate")
    end_date: str | None = Field(default=None, alias="dcat:endDate")

    @model_validator(mode="after")
    def dates(self):
        from datetime import date

        for value in (self.start_date, self.end_date):
            if value is not None:
                date.fromisoformat(value)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Temporal start exceeds end")
        return self


class Concept(ExtensibleModel):
    id: str | None = Field(default=None, alias="@id")
    type: Literal["skos:Concept"] = Field(default="skos:Concept", alias="@type")
    code: Text = Field(alias="skos:notation")
    label: LanguageMap = Field(default_factory=dict, alias="skos:prefLabel")
    notes: LanguageMap = Field(default_factory=dict, alias="skos:note")
    broader: list[str] = Field(default_factory=list, alias="skos:broader")
    unit: dict[str, JsonValue] | None = Field(default=None, alias="ni:unit")
    coordinates: JsonValue = Field(default=None, alias="ni:coordinates")
    aggregation_map: list[str] | None = Field(default=None, alias="ni:aggregationMap")


class CodeList(ExtensibleModel):
    id: Text = Field(alias="@id")
    type: Literal["skos:ConceptScheme"] = Field(
        default="skos:ConceptScheme", alias="@type"
    )
    label: LanguageMap = Field(default_factory=dict, alias="skos:prefLabel")
    codes: list[Concept] = Field(default_factory=list, alias="codes")
    category_order: dict[str, list[str]] = Field(
        default_factory=dict, alias="ni:categoryOrder"
    )
    source_identity: dict[str, JsonValue] = Field(
        default_factory=dict, alias="ni:sourceIdentity"
    )

    @model_validator(mode="after")
    def unique_codes(self):
        if len({code.code for code in self.codes}) != len(self.codes):
            raise ValueError("Duplicate codelist code")
        known = {code.code for code in self.codes}
        if not self.category_order:
            languages = set(self.label) | set(self.source_identity.get("languages", []))
            for language in languages or {"und"}:
                self.category_order[language] = [code.code for code in self.codes]
        for codes in self.category_order.values():
            if len(codes) != len(set(codes)) or not set(codes) <= known:
                raise ValueError("Invalid codelist category order")
        links = {code.id: code.broader for code in self.codes if code.id}
        complete = set()

        def visit(identity, active):
            if identity in active:
                raise ValueError("Codelist hierarchy contains a cycle")
            if identity not in links:
                raise ValueError("Unresolved codelist hierarchy reference")
            if identity not in complete:
                for parent in links[identity]:
                    visit(parent, active | {identity})
                complete.add(identity)

        for identity in links:
            visit(identity, set())
        return self


class Dimension(ExtensibleModel):
    id: str | None = Field(default=None, alias="@id")
    type: Literal["qb:DimensionProperty"] = Field(
        default="qb:DimensionProperty", alias="@type"
    )
    code: Text = Field(alias="dct:identifier")
    label: LanguageMap = Field(default_factory=dict, alias="skos:prefLabel")
    notes: LanguageMap = Field(default_factory=dict, alias="skos:note")
    code_list: CodeList | None = Field(default=None, alias="qb:codeList")
    role: Literal["time", "geo", "metric"] | None = Field(default=None, alias="ni:role")
    elimination: StrictBool | None = Field(default=None, alias="ni:elimination")
    elimination_value: str | None = Field(default=None, alias="ni:eliminationValue")
    links: dict[str, JsonValue] = Field(default_factory=dict, alias="ni:links")


class Measure(ExtensibleModel):
    id: Text = Field(alias="@id")
    type: Literal["qb:MeasureProperty"] = Field(
        default="qb:MeasureProperty", alias="@type"
    )
    label: LanguageMap = Field(default_factory=dict, alias="skos:prefLabel")


class Attribute(ExtensibleModel):
    id: Text = Field(alias="@id")
    type: Literal["qb:AttributeProperty"] = Field(
        default="qb:AttributeProperty", alias="@type"
    )
    label: LanguageMap = Field(default_factory=dict, alias="skos:prefLabel")


class Component(ExtensibleModel):
    dimension: Dimension = Field(alias="qb:dimension")
    order: StrictInt = Field(alias="qb:order", ge=1)


class DataStructureDefinition(ExtensibleModel):
    id: Text = Field(alias="@id")
    type: Literal["qb:DataStructureDefinition"] = Field(
        default="qb:DataStructureDefinition", alias="@type"
    )
    components: list[Component] = Field(default_factory=list, alias="qb:component")

    @model_validator(mode="after")
    def validate_structure(self):
        codes = [c.dimension.code for c in self.components]
        if len(set(codes)) != len(codes) or [c.order for c in self.components] != list(
            range(1, len(codes) + 1)
        ):
            raise ValueError("Dimension membership/order mismatch")
        for component in self.components:
            dimension = component.dimension
            if dimension.code_list is None:
                raise ValueError("Retrievable dimension requires base categories")
            known = {code.code for code in dimension.code_list.codes}
            if (
                dimension.elimination_value is not None
                and dimension.elimination_value not in known
            ):
                raise ValueError("Elimination category is unknown")
        return self


class SourceIdentity(ExtensibleModel):
    adapter: Text
    provider_code: Text
    dataset_code: Text
    language: Text


class QueryDimension(ExtensibleModel):
    code: Text
    categories: list[Text]
    index_encoding: Literal["list", "dict", "omitted"] = "list"

    @model_validator(mode="after")
    def unique_categories(self):
        if not self.categories or len(self.categories) != len(set(self.categories)):
            raise ValueError("Empty or duplicate selectable categories")
        return self


class AdapterOptions(ExtensibleModel):
    """Source-specific request metadata; adapters may introduce JSON extensions."""

    max_cells: StrictInt | None = Field(default=None, gt=0)
    scope: Literal["municipality", "ou"] | None = None
    kpi_id: str | None = None


class RetrievalBinding(ExtensibleModel):
    source: SourceIdentity
    dimensions: list[QueryDimension]
    metadata_url: str | None = None
    options: AdapterOptions = Field(default_factory=AdapterOptions)

    @model_validator(mode="after")
    def unique_dimensions(self):
        if len({d.code for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("Duplicate retrieval dimension")
        return self


class Catalog(ExtensibleModel):
    id: Text = Field(alias="@id")
    type: Literal["dcat:Catalog"] = Field(default="dcat:Catalog", alias="@type")
    title: LanguageMap = Field(default_factory=dict, alias="dct:title")
    publisher: list[Agent] = Field(default_factory=list, alias="dct:publisher")


class DataService(ExtensibleModel):
    adapter: Text = Field(alias="ni:adapter")
    publisher: list[Agent] = Field(default_factory=list, alias="dct:publisher")
    id: Text = Field(alias="@id")
    type: Literal["dcat:DataService"] = Field(default="dcat:DataService", alias="@type")
    title: LanguageMap = Field(default_factory=dict, alias="dct:title")
    description: LanguageMap = Field(default_factory=dict, alias="dct:description")
    endpoint_url: list[str] = Field(default_factory=list, alias="dcat:endpointURL")
    endpoint_description: list[str] = Field(
        default_factory=list, alias="dcat:endpointDescription"
    )

    @model_validator(mode="after")
    def shared_adapter_identity(self):
        from metadata_harvest.registry import ADAPTERS

        if self.adapter not in ADAPTERS or not ADAPTERS[self.adapter].available:
            raise ValueError("DataService must name an implemented adapter")
        if self.id != "urn:nordicintel:service:" + self.adapter:
            raise ValueError("DataService identity is shared globally per adapter")
        if self.publisher or self.endpoint_url or self.endpoint_description:
            raise ValueError(
                "Provider and endpoint details belong to catalogs/distributions, not shared adapter services"
            )
        return self


class VocabularyResource(ExtensibleModel):
    """A standard linked resource with its supplied labels and source details."""

    id: str | None = Field(default=None, alias="@id")
    label: LanguageMap = Field(default_factory=dict, alias="rdfs:label")
    identifiers: list[str] = Field(default_factory=list, alias="dct:identifier")


class DescriptiveResource(ExtensibleModel):
    """Standard descriptive properties shared by catalogued data resources."""

    issued: str | None = Field(default=None, alias="dct:issued")
    status: list[VocabularyResource] = Field(default_factory=list, alias="adms:status")
    languages: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:language"
    )
    conforms_to: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:conformsTo"
    )
    resource_types: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:type"
    )
    rights: list[VocabularyResource] = Field(default_factory=list, alias="dct:rights")
    access_rights: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:accessRights"
    )
    applicable_legislation: list[VocabularyResource] = Field(
        default_factory=list, alias="dcatap:applicableLegislation"
    )
    spatial_resolution: list[float] = Field(
        default_factory=list, alias="dcat:spatialResolutionInMeters"
    )
    source_assertions: dict[str, JsonValue] = Field(
        default_factory=dict, alias="ni:sourceAssertions"
    )


class Distribution(DescriptiveResource):
    format: list[VocabularyResource] = Field(default_factory=list, alias="dct:format")
    media_type: list[VocabularyResource] = Field(
        default_factory=list, alias="dcat:mediaType"
    )
    availability: list[VocabularyResource] = Field(
        default_factory=list, alias="dcatap:availability"
    )
    byte_size: float | None = Field(default=None, alias="dcat:byteSize", ge=0)

    modified: str | None = Field(default=None, alias="dct:modified")
    notes: LanguageMap = Field(default_factory=dict, alias="skos:note")
    temporal_resolution: list[str] = Field(
        default_factory=list, alias="dcat:temporalResolution"
    )
    landing_page: list[str] = Field(default_factory=list, alias="dcat:landingPage")
    documentation: list[str] = Field(default_factory=list, alias="foaf:page")
    contacts: list[ContactPoint] = Field(
        default_factory=list, alias="dcat:contactPoint"
    )
    themes: list[Concept] = Field(default_factory=list, alias="dcat:theme")
    next_release: str | None = Field(default=None, alias="ni:nextRelease")
    id: Text = Field(alias="@id")
    type: Literal["dcat:Distribution"] = Field(
        default="dcat:Distribution", alias="@type"
    )
    title: LanguageMap = Field(default_factory=dict, alias="dct:title")
    description: LanguageMap = Field(default_factory=dict, alias="dct:description")
    access_url: list[str] = Field(default_factory=list, alias="dcat:accessURL")
    download_url: list[str] = Field(default_factory=list, alias="dcat:downloadURL")
    license: list[str] = Field(default_factory=list, alias="dct:license")
    services: list[DataService] = Field(
        default_factory=list, alias="dcat:accessService"
    )
    structure: DataStructureDefinition | None = Field(
        default=None, alias="ni:structure"
    )
    retrieval: RetrievalBinding | None = Field(default=None, alias="ni:retrieval")
    temporal: list[PeriodOfTime] = Field(default_factory=list, alias="dct:temporal")
    official_statistics: StrictBool | None = Field(
        default=None, alias="ni:officialStatistics"
    )
    discontinued: StrictBool | None = Field(default=None, alias="ni:discontinued")
    source_evidence: dict[str, JsonValue] = Field(
        default_factory=dict, alias="ni:sourceEvidence"
    )

    @model_validator(mode="after")
    def usable_binding(self):
        if self.retrieval is not None:
            if self.structure is None:
                raise ValueError("Retrieval requires a structure")
            if [service.adapter for service in self.services] != [
                self.retrieval.source.adapter
            ]:
                raise ValueError(
                    "Retrievable distribution requires its shared adapter service"
                )
            axes = self.retrieval.dimensions
            dimensions = [c.dimension for c in self.structure.components]
            if [a.code for a in axes] != [d.code for d in dimensions]:
                raise ValueError("Retrieval and structure dimension order differ")
            for axis, dimension in zip(axes, dimensions, strict=True):
                if set(axis.categories) != {c.code for c in dimension.code_list.codes}:
                    raise ValueError("Retrieval and structure categories differ")
        return self


class Dataset(DescriptiveResource):
    creators: list[Agent] = Field(default_factory=list, alias="dct:creator")
    frequency: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:accrualPeriodicity"
    )
    spatial: list[VocabularyResource] = Field(default_factory=list, alias="dct:spatial")
    provenance: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:provenance"
    )
    related: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:relation"
    )
    source: list[VocabularyResource] = Field(default_factory=list, alias="dct:source")
    in_series: list[VocabularyResource] = Field(
        default_factory=list, alias="dcat:inSeries"
    )
    referenced_by: list[VocabularyResource] = Field(
        default_factory=list, alias="dct:isReferencedBy"
    )
    version: str | None = Field(default=None, alias="dcat:version")
    version_notes: LanguageMap = Field(default_factory=dict, alias="adms:versionNotes")
    catalogs: list[Catalog] = Field(default_factory=list, alias="catalogs")
    id: Text = Field(alias="@id")
    type: Literal["dcat:Dataset"] = Field(default="dcat:Dataset", alias="@type")
    profile_version: Literal["1.0.0"] = Field(
        default=PROFILE_VERSION, alias="ni:profileVersion"
    )
    title: LanguageMap = Field(default_factory=dict, alias="dct:title")
    description: LanguageMap = Field(default_factory=dict, alias="dct:description")
    identifiers: list[str] = Field(default_factory=list, alias="dct:identifier")
    modified: str | None = Field(default=None, alias="dct:modified")
    temporal: list[PeriodOfTime] = Field(default_factory=list, alias="dct:temporal")
    temporal_resolution: list[str] = Field(
        default_factory=list, alias="dcat:temporalResolution"
    )
    landing_page: list[str] = Field(default_factory=list, alias="dcat:landingPage")
    documentation: list[str] = Field(default_factory=list, alias="foaf:page")
    publisher: list[Agent] = Field(default_factory=list, alias="dct:publisher")
    contacts: list[ContactPoint] = Field(
        default_factory=list, alias="dcat:contactPoint"
    )
    themes: list[Concept] = Field(default_factory=list, alias="dcat:theme")
    keywords: LanguageMap = Field(default_factory=dict, alias="dcat:keyword")
    notes: LanguageMap = Field(default_factory=dict, alias="skos:note")
    dimensions: list[Dimension | Reference] = Field(
        default_factory=list, alias="statdcatap:dimension"
    )
    measures: list[Measure] = Field(default_factory=list, alias="statdcatap:measure")
    attributes: list[Attribute] = Field(
        default_factory=list, alias="statdcatap:attribute"
    )
    structure: Reference | DataStructureDefinition | None = Field(
        default=None, alias="statdcatap:dataStructureDefinition"
    )
    distributions: list[Distribution] = Field(
        default_factory=list, alias="dcat:distribution"
    )
    official_statistics: StrictBool | None = Field(
        default=None, alias="ni:officialStatistics"
    )
    discontinued: StrictBool | None = Field(default=None, alias="ni:discontinued")
    next_release: str | None = Field(default=None, alias="ni:nextRelease")
    source_evidence: dict[str, JsonValue] = Field(
        default_factory=dict, alias="ni:sourceEvidence"
    )
    match_evidence: list[dict[str, JsonValue]] = Field(
        default_factory=list, alias="ni:matchEvidence"
    )

    @model_validator(mode="after")
    def unique_distributions(self):
        if len({d.id for d in self.distributions}) != len(self.distributions):
            raise ValueError("Duplicate distribution identity")
        return self


class HarvestContribution(ExtensibleModel):
    source: SourceIdentity
    dataset: Dataset
    codelists: list[CodeList] = Field(default_factory=list)
    attribute_mapping: dict[str, str] = Field(default_factory=dict)
