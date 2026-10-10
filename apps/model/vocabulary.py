"""Pinned NordicIntel profile identity and inline JSON-LD vocabulary."""

from __future__ import annotations

PROFILE_VERSION = "2.0.0"


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
            "foaf:homepage",
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


PROFILE1_CONTEXT = {
    key: value for key, value in CONTEXT.items() if key != "foaf:homepage"
}
