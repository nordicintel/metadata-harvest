"""Future deterministic roles, periods and other semantic assignments. No processor is configured yet."""

CONFIGURED = False


def process(dataset: dict):
    """Return explicit inactive status without reading or changing source content."""
    from metadata_harvest.processing import StageResult

    return StageResult("not_configured")
