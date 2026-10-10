"""PXWeb v1 discovery and metadata; legacy web discovery is retained."""


async def fetch_metadata(record, *, provider_code=None):
    """Build validated source contributions; preserve native task identities."""
    from apps.harvest.accepted import as_contributions
    from apps.harvest.metadata_adapters.pxweb_v1.metadata import get_pxweb_metadata

    document = await get_pxweb_metadata(
        record, provider_code=provider_code or record["provider_code"]
    )
    return as_contributions(document, adapter="pxweb_v1")
