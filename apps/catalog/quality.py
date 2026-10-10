"""Publication integrity gates and non-blocking descriptive quality findings."""

from apps.model.models import Dataset, Dimension, Reference


def validate_publication(dataset: Dataset) -> list[dict]:
    """Validate internal structural references; describe gaps without dropping records."""
    structures = {
        distribution.structure.id: distribution.structure
        for distribution in dataset.distributions
        if distribution.structure is not None
    }
    if dataset.structure is not None and not isinstance(dataset.structure, Reference):
        structures[dataset.structure.id] = dataset.structure
    if (
        isinstance(dataset.structure, Reference)
        and dataset.structure.id not in structures
    ):
        raise ValueError("Unresolved dataset structure reference")
    dimensions = {
        component.dimension.id
        for structure in structures.values()
        for component in structure.components
    }
    dimensions.update(
        dimension.id
        for dimension in dataset.dimensions
        if isinstance(dimension, Dimension)
    )
    if any(
        isinstance(dimension, Reference) and dimension.id not in dimensions
        for dimension in dataset.dimensions
    ):
        raise ValueError("Unresolved dataset dimension reference")
    findings = []
    findings.extend(dataset.source_evidence.get("attribution_findings", []))
    for field in ("title", "description", "publisher", "contacts"):
        if not getattr(dataset, field):
            findings.append(
                {
                    "entity": dataset.id,
                    "code": "missing_" + field,
                    "severity": "quality",
                }
            )
    for distribution in dataset.distributions:
        for service in distribution.services:
            if not service.endpoint_url:
                findings.append(
                    {
                        "entity": service.id,
                        "code": "missing_endpoint",
                        "severity": "quality",
                    }
                )
        for message in distribution.source_evidence.get("diagnostics", []):
            findings.append(
                {
                    "entity": distribution.id,
                    "code": "source_attribute",
                    "message": message,
                    "severity": "quality",
                }
            )
    return findings
