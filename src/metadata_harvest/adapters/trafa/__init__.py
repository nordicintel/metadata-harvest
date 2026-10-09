"""Prepared trafa integration; no harvesting or retrieval implemented here."""

AVAILABLE = False


def _unavailable(*args, **kwargs):
    raise NotImplementedError(
        "trafa is not implemented here; see docs/adapters/trafa.md"
    )


discover_provider = _unavailable
fetch_metadata = _unavailable
retrieve = _unavailable
