# Catalog assembly and publication

The builder runs offline: accepted contributions → grouping → portal matching → enrichment → publication filtering → validation → deterministic JSON-LD. It never starts a harvest or downloads portal metadata.

## Inputs and matching

The repository owns `data/data_eu/oppnadata.jsonl.gz` and its hash/count manifest. Import an existing export with `python -m harvesting.portal import --source <export.jsonl>` or download a candidate with `python -m harvesting.portal refresh`. Scope is always the `oppnadata` catalog. Interrupted pagination, invalid scope and malformed/duplicate identities fail before accepting the candidate. The separate `refresh-portal.yml` workflow runs weekly through [schedules.toml](../schedules.toml).

Matching order is explicit [overrides](../catalog-matches.json), provider-scoped native identifiers, then exact dataset-specific access/metadata/landing-page URLs. Titles and shared endpoints are not identity evidence. Ambiguous matches are reported. Overrides may identify an equivalent dataset or a broader related entry; broader entries are not flattened into the dataset. Unmatched harvested datasets remain; portal-only datasets are not added.

Identifier comparison keys normalize URL encoding, lowercase, and remove whitespace and punctuation, as requested. Original identifiers are never rewritten. Native PxWeb table IDs can also be extracted from API URLs beneath a configured provider's endpoint; this handles default ports, HTTP/HTTPS and framework-prefix spelling differences without guessing from titles. Collisions retain all candidates and are reported as ambiguous. Exact URL fallback retains meaningful fragments and query strings.

Harvested titles, structure, retrieval and coverage remain authoritative. Portal descriptions, publisher information, themes, keywords, contacts, licences and additional distributions enrich the dataset. Conflicts remain distinct source assertions. Portal distribution licences do not spill onto harvested distributions. Catalog-record modification is not treated as source data modification.

The [publication switch](../catalog.toml) `include_scb_pxweb_v1 = false` removes distributions of SCB's main v1 API just before serialization. SCB HVD and every other provider's v1 API remain. Switching it on rebuilds from retained accepted descriptions and the local portal snapshot; no harvest is required.

## Build and publish

Zstandard is included in the Python dependencies. 7-Zip is needed only when reading the existing legacy `.7z` publication.

```powershell
uv run python -m harvesting.catalog build
# Explicit publication commits/pushes only data/catalog/.
uv run python -m harvesting.catalog publish
```

New production builds produce `catalog.jsonl.zst` and `codelists.jsonl.zst`, each a compressed JSONL stream. Models embed a bundled inline context. No release assets or external storage are required. Compression uses Zstandard level 6 with a content checksum and no timestamps. Consumers can stream JSONL without extracting a temporary file. All files retain the existing 100 MiB gate.

## Incremental assembly and retention

State records accepted input hashes and committed Git blob identities, per-owner output byte ranges/checksums, profile/rule/configuration fingerprints, and relevant portal matching inputs. Unchanged groups reuse verified output. An unrelated portal record change does not invalidate their enrichment. Equivalent content reuses the existing archive bytes.

Every changed input is fully parsed and validated before acceptance. A failed current input is rolled back independently and reconstructed from its last accepted committed blob when available; other valid inputs can advance. Without recoverable source bytes, the previous verified owner output is retained, and changing publication rules is refused until recovery is possible. A missing collection is not deletion evidence. A confirmed removal must be represented by a valid accepted replacement, including an empty collection where appropriate.

Previous archives must match their recorded hashes. Candidates carry source, dependency, prior-catalog and output fingerprints; any drift blocks promotion. Publication remains scoped, serialized and non-force-pushed. Reports distinguish matching, ambiguity, filtering, source failures and retained inputs; upstream harvest reports remain visible.

`build_legacy` is an explicit recovery/test path for the previous concatenated contract. It is not the default publication builder.

## Streaming reads

```python
import io
import json
from harvesting.catalog import open_catalog

with open_catalog("catalog.jsonl.zst") as raw:
    with io.TextIOWrapper(raw, encoding="utf-8") as stream:
        for line in stream:
            dataset = json.loads(line)
```

This is sequential access. Seeking directly to an arbitrary dataset would need a
separate index and independently compressed frames. `open_catalog` also reads
legacy `.7z` files through 7-Zip without extracting files onto disk. The internal
catalog assembler may still use temporary decompressed files for its byte-range
reuse; consumer streaming does not require those files.

The existing production `.7z` files remain unchanged until a production publication.
Development trials do not promote or publish catalog assets.

## Level 6 trial, 2026-10-10

The [trial measurements](compression-trial.json) used the existing complete publication,
without metadata reconstruction, source requests or publication. Both decompressed
outputs matched the existing state content hashes exactly.

| Stream | Zstandard size | Time including legacy decode and verification |
| --- | ---: | ---: |
| Catalog | 78.85 MiB | 18.27 s |
| Codelists | 55.14 MiB | 14.47 s |

These are single-run measurements on the development machine, not compression-only
benchmarks. They establish the size of this corpus, not future profile output;
the production size gate still applies.
