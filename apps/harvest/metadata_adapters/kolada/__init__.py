"""Kolada metadata harvesting; harvesting imports stay lazy."""

AVAILABLE = True


async def discover_provider(provider, *, client=None):
    from apps.harvest.metadata_adapters.kolada.workflow import (
        discover_provider as discover,
    )

    return await discover(provider, client=client)


async def fetch_metadata(record, *, client):
    from apps.harvest.metadata_adapters.kolada.metadata import fetch_metadata as fetch
    from apps.parsing.accepted import as_contributions

    return as_contributions(await fetch(record, client=client), adapter="kolada")
