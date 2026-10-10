"""Repository compatibility for typed distributions and shared retrieval helpers."""

from harvesting.models import Distribution
from retrieval._selection import retrieve_selection, split_selection
from retrieval.distribution import retrieve_distribution as _retrieve

__all__ = ["retrieve_distribution", "retrieve_selection", "split_selection"]


async def retrieve_distribution(session, distribution: Distribution, *, selection):
    """Serialize the repository model and delegate to the standalone library."""
    if not isinstance(distribution, Distribution):
        raise TypeError("A validated Distribution model is required")
    return await _retrieve(session, distribution.to_jsonld(), selection=selection)
