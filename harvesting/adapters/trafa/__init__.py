"""Trafikanalys metadata harvesting; harvesting imports stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    from harvesting.adapters.trafa.workflow import discover_provider as discover

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    from harvesting.accepted import as_contributions
    from harvesting.adapters.trafa.workflow import fetch_metadata as fetch

    return as_contributions(await fetch(record, client=client), adapter="trafa")
