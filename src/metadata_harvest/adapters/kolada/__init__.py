"""Prepared kolada integration; no harvesting or retrieval implemented here."""

AVAILABLE = False


def _unavailable(*args, **kwargs):
    raise NotImplementedError(
        "kolada is not implemented here; see docs/adapters/kolada.md"
    )


discover_provider = _unavailable
fetch_metadata = _unavailable
retrieve = _unavailable
