"""Försäkringskassan metadata and retrieval; harvesting imports stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    from harvesting.adapters.forsakringskassan.workflow import (
        discover_provider as discover,
    )

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    from harvesting.accepted import as_contributions
    from harvesting.adapters.forsakringskassan.workflow import (
        fetch_metadata as fetch,
    )

    return as_contributions(
        await fetch(record, client=client), adapter="forsakringskassan"
    )


async def retrieve(session, **kwargs):
    from harvesting.adapters.forsakringskassan.retrieval import retrieve as fetch

    return await fetch(session, **kwargs)
