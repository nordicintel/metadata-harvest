"""Prepared sdb integration; no harvesting or retrieval implemented here."""

AVAILABLE = False


def _unavailable(*args, **kwargs):
    raise NotImplementedError("sdb is not implemented here; see docs/adapters/sdb.md")


discover_provider = _unavailable
fetch_metadata = _unavailable
retrieve = _unavailable
