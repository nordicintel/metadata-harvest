"""Tillväxtverket open data metadata and retrieval; harvesting imports stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    """The registered datasets; Tillväxtverket has no listing endpoint."""
    from metadata_harvest.adapters.tillvaxtverket.workflow import (
        discover_provider as discover,
    )

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    """Build one registered dataset's document; ``record`` is a ``source.Spec``."""
    from metadata_harvest.accepted import as_contributions
    from metadata_harvest.adapters.tillvaxtverket.workflow import (
        fetch_metadata as fetch,
    )

    return as_contributions(
        await fetch(record, client=client), adapter="tillvaxtverket"
    )


async def retrieve(session, **kwargs):
    from metadata_harvest.adapters.tillvaxtverket.retrieval import retrieve as fetch

    return await fetch(session, **kwargs)
