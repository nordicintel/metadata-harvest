"""Apply publication-only distribution filters to copied dataset models."""

from __future__ import annotations

from urllib.parse import urlsplit

from apps.model.models import (
    Dataset,
    Reference,
)


def is_scb_v1_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in {
        "api.scb.se",
        "www.statistikdatabasen.scb.se",
        "statistikdatabasen.scb.se",
    } and ("/OV0104/v1/" in parsed.path or "/api/v1/" in parsed.path.lower())


def publication_view(dataset: Dataset, *, include_scb_pxweb_v1=False) -> Dataset:
    """Filter a copy; imported/accepted descriptions remain available for rebuilding."""
    result = dataset.model_copy(deep=True)
    if not include_scb_pxweb_v1:
        result.distributions = [
            d
            for d in result.distributions
            if not any(is_scb_v1_url(u) for u in d.access_url + d.download_url)
        ]
        structures = {
            d.structure.id: d.structure for d in result.distributions if d.structure
        }
        if (
            isinstance(result.structure, Reference)
            and result.structure.id not in structures
        ):
            result.structure = None
        if result.structure is not None and not isinstance(result.structure, Reference):
            structures[result.structure.id] = result.structure
        dimensions = {c.dimension.id for s in structures.values() for c in s.components}
        result.dimensions = [
            d
            for d in result.dimensions
            if not isinstance(d, Reference) or d.id in dimensions
        ]
        used_services = {s.id for d in result.distributions for s in d.services}
        for catalog in result.catalogs:
            catalog.services = [s for s in catalog.services if s.id in used_services]
    return result
