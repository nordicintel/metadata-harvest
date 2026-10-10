# Portal snapshots

`apps.portal.snapshot` owns import, refresh and accepted snapshot verification.
Matching and enrichment live in catalog code and run offline.

```sh
uv run python -m apps.operations.portal import --source snapshot.jsonl --output data/data_eu
uv run python -m apps.operations.portal refresh --output data/data_eu
```

These commands accept only complete oppnadata snapshots. Failed pagination, invalid
scope or malformed records leave the accepted snapshot intact. Git publication is
separate: `uv run python -m apps.operations.portal publish`.
