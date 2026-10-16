# Implementing a metadata adapter

Start with `apps/harvest/registry.py` and the closest implemented adapter under
`apps/harvest/metadata_adapters/`. Registry entries are explicit; each adapter owns
its clients, discovery, parsing, coverage probes and operational policy.

## Boundaries

1. Configure verified organisations, concrete services and catalogs through the
   [three inventories](../configuration/README.md). Preserve routing aliases and
   dataset/distribution identities independently of attribution.
2. Discover native records with explicit scope completeness and individual errors.
   `workflow.run` stores responses as received through `apps.harvest.source_store`
   (safety nets: successful response, non-empty, JSON-serialisable) and never calls
   `mapping`; `parse.py` provides `documents(store, state, providers, *,
   portal=None)` yielding `(key, document | exception)` and `withheld_keys(state)`.
   Keep anything derived from observations under the envelope's `derived` key.
   The public `fetch_metadata` builds in memory and returns
   `list[HarvestContribution]` for live checks only.
3. Preserve source change signals and unfamiliar attributes. Use standard properties
   where they fit; see the [StatDCAT field mapping](../model/output-contract.md).
4. Keep metadata inference and observation probes here. Put consumer request/response
   implementation in `retrieval/data_adapters/`; harvesting must not call the
   consumer dispatcher. Only genuinely identical pure helpers may be shared.

## Active worker representation

Some native parsers use the [worker schema](../../schemas/legacy-dataset-metadata.schema.json)
before conversion in `apps.parsing.contributions`. This is an internal JSON-stat
representation, not the published metadata contract: `id` orders dimensions, `size`
gives category counts, and `value: []` means metadata only. Original routing identity
and URLs live in `extension.nordicintel`; adapter evidence uses its own namespace.
Category extras belong to the enclosing dimension's namespace. Preserve codes,
labels, units, notes, hierarchy, role and elimination settings. Conversion must
preserve source values without duplicating successfully mapped fields.

## Failure and persistence

Isolate failure per resource or discovery scope; process valid siblings after a bad
row. Incomplete pagination, malformed rows and conflicting identities block absence
inference. Scope keys and caches by source, language and complete native path.
Keep previous accepted contributions on replacement failure; retry failures even
when the listing timestamp is unchanged. Codelist failure must not reject valid
table metadata. Propagate cancellation and never turn a failed request into empty
successful output.

Use shared atomic persistence and artifact verification. Keep retry/pacing policy
inside the adapter. Publish successes with explicit unresolved errors; reject stale
artifacts, unsafe paths, identity mismatches and hash failures. Report phase, counters
and structured errors through `Progress`, outside accepted metadata.

## Verification and integration

Fixtures should cover malformed rows before valid siblings, incomplete scopes,
failed-update retention, retry, language/source isolation, cancellation and independent
codelist errors. Retrieval fixtures should execute actual adapter requests and
compare dispatcher behavior. Run affected checks; use bounded live checks only when
source compatibility needs verification, and label them separately.

Register workflows and central schedules together. See [harvesting operations](README.md)
for continuation, monitoring and collection.
