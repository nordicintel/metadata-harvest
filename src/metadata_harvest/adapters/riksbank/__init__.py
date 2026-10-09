"""Riksbank (SWEA, forecasts, SWESTR) metadata and retrieval; harvesting imports
stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    from metadata_harvest.adapters.riksbank.workflow import (
        discover_provider as discover,
    )

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    from metadata_harvest.adapters.riksbank.workflow import fetch_metadata as fetch

    return await fetch(record, client=client)


async def retrieve(session, **kwargs):
    from metadata_harvest.adapters.riksbank.retrieval import retrieve as fetch

    return await fetch(session, **kwargs)
