"""EntryScape RowStore metadata and retrieval; harvesting imports stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    from metadata_harvest.adapters.entrysoft.workflow import (
        discover_provider as discover,
    )

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    from metadata_harvest.accepted import as_contributions
    from metadata_harvest.adapters.entrysoft.workflow import fetch_metadata as fetch

    return as_contributions(await fetch(record, client=client), adapter="entrysoft")


async def retrieve(session, **kwargs):
    from metadata_harvest.adapters.entrysoft.retrieval import retrieve as fetch

    return await fetch(session, **kwargs)
