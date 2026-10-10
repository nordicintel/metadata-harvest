# Generated profile schemas

`dataset-metadata.schema.json`, `codelist.schema.json` and `harvest-contribution.schema.json` are generated from local Pydantic v2 models. Run `scripts/generate/generate_schema_reference.py` from the repository root. `legacy-dataset-metadata.schema.json` supports native parsing and explicit migration only.

`statdcatap-3.0.0-diagnostic.ttl` is the upstream diagnostic shape set pinned by URL, commit and checksum in `../config/profile.json`. It is used offline by `scripts/checks/check_statdcat_upstream.py`; upstream conformance is not a publication gate.

## Legacy schema provenance

# Shared output contracts

Authoritative schemas owned by metadata-harvest. Imported from nordicintel/nordicintel-schemas commit 77a7a4665fea6f3c2be8f286d560b79317f08dd7 (prepared, unpublished 2.0.0), under the accompanying Apache-2.0 licence. Schema identifiers now name this repository; consumers pin a repository commit or installed distribution and validate offline. This does not create metadata snapshots or a runtime model dependency.
