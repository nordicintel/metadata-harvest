# Explicit production publication

`apps.publication.archives` reads historical 7-Zip archives and streams current
Zstandard level 6 output with checksums. `candidates` verifies fingerprints and
promotes an accepted candidate; `git` limits commits to the selected production scope.

```sh
uv run python -m apps.operations.catalog build --data data --output output/catalog --config config
uv run python -m apps.operations.catalog publish --data data --output output/catalog --config config
```

Building is offline and writes only the candidate directory. Publishing is a
separate production action. It rejects stale source/configuration fingerprints,
tampered candidates and unrelated staged changes. Every file remains below the
existing 100 MiB gate. Development changes do not require production publication.

The two outputs are `catalog.jsonl.zst` and `codelists.jsonl.zst`, accompanied by
state and reports. Existing production bytes remain until an explicit publication.
