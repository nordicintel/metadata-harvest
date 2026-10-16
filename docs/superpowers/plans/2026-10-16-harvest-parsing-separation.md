# Harvest/Parsing Separation and Per-Dataset Source Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> At execution start, copy this file to `docs/superpowers/plans/2026-10-16-harvest-parsing-separation.md` and track progress there.

**Goal:** Harvest workflows store only native source responses, one envelope file per dataset/table plus a per-adapter manifest; a new unscheduled `apps/parsing` module turns those files into today's accepted StatDCAT collections, byte for byte.

**Architecture:** A shared `SourceStore` (`data/<adapter>/source/`) replaces `write_metadata` as the harvest's persistence target. Each adapter keeps its `mapping.py` but `workflow.run` never calls it; a new `parse.py` per adapter rebuilds worker documents from the store, and `apps/parsing/build.py` feeds them through the existing `write_metadata` path so `apps/catalog` keeps reading `data/<adapter>/metadata/*.jsonl.gz` unchanged. StatDCAT conversion code moves from `apps/harvest` to `apps/parsing` in the last code task; harvest workflows stop dispatching the catalog.

**Tech Stack:** Python 3.13, uv, pytest, ruff, aiohttp, Pydantic (`apps/model`), existing `apps.harvest.storage.write` atomic JSON writer.

**Spec:** `docs/backlog.md` lines 23-36 (the two **harvest** items under *Now*), plus the decisions recorded in Context below.

## Context

Both *Now* items in `docs/backlog.md` are one change. Today every adapter's `state.save()` (or `JsonlStore.save()` on a `metadata/` directory) calls `apps.harvest.accepted.write_metadata`, which converts JSON-stat worker documents to StatDCAT (`apps/harvest/contributions.py:convert_dataset`), assembles languages and writes one gzipped collection per owner. On the next run the adapter reads those collections back through `legacy_document` to decide what to refetch. The harvest therefore owns parsing, the catalog's input unit is a whole owner collection (one changed SCB table rebuilds ~5,000 datasets), and nothing records where one dataset's source data lives or when it was last updated. Tillväxtverket additionally fetches data.europa.eu records inside its harvest; the catalog already merges the same portal data offline.

Decisions taken with the user (2026-10-16):

- **Raw level:** store native responses as received. Harvest may parse in memory to decide what to request next, but persists only responses, manifest and operational state. `mapping.py` is called only from parsing.
- **No in-memory acceptance gate.** The harvest stores whatever came back, guarded by simple safety nets: only bodies the client returned successfully (2xx), non-empty, JSON-serialisable (`allow_nan=False`), and a cheap identity check where the body carries the requested identifier. A source regression therefore replaces the stored raw file and parse reports the failure; today's "a mapping failure keeps the last good document" rule is retired for mapping errors and kept only for transport errors.
- **Declared derived data** is allowed only where the per-dataset source data are observations: Kolada per-scope `coverage`, EntryScape trimmed `structure`, PXWeb v1 samples stripped of `value`/`status`, UKÄ per-period scan summaries (no CSV exports in Git). The envelope keeps these under `derived`, never under `responses`.
- **Parse output:** today's collections, same path, same bytes. Catalog untouched. Parsing is a manual command; no workflow, no schedule. Existing `data/<adapter>/metadata/*.jsonl.gz` are never deleted by harvest code; `parsing apply` replaces them.
- **File format:** plain JSON, `indent=2`, `sort_keys=True`, `ensure_ascii=False`, trailing newline, atomic writes.
- **Scope:** all twelve adapters in this plan, single cutover per adapter, commits straight to `main` (user instruction: merge direct requests myself).
- **Tillväxtverket:** drop the per-dataset data.europa.eu fetch; parse feeds `apply_dcat` from the offline portal snapshot `data/data_eu/oppnadata.jsonl.gz` (verified identical for all six accepted documents).

## Global Constraints

- Python 3.13+, `uv run`; every task ends with `uv run ruff check .`, `uv run ruff format --check .` and the owning tests green.
- Preserve accepted data and production artifacts: never delete or rewrite `data/<adapter>/metadata/*.jsonl.gz`, `data/catalog/*`, `data/data_eu/*` from harvest code or tests against the real `data/` tree. Tests use `tmp_path`.
- Preserve opaque identifiers, ordering, meaningful empty values, zeroes and `False` (AGENTS.md). Manifest `updated` is stored verbatim as the JSON value the source gave (string, number or null), never stringified.
- Every published file stays below 100 MiB (`apps.publication.artifacts.check_collection_sizes` keeps running on worker output).
- `apps/harvest` must not import `apps/parsing`; `apps/parsing` must not import `apps/catalog`; `apps/model` stays pure. `retrieval/` is untouched.
- Dataset codes may contain `/` and `|` (forsakringskassan `<report>/<Table>`, tillvaxtverket `inkvartering/<Name>`, kolada `<scope>/<kpi>`, dataanalys `<AREA>|<code>`, trafa `<product>|...`). Keys split as `provider, rest = key.split("/", 1); code, lang = rest.rsplit("/", 1)`.
- Commit subjects: short imperative, e.g. "Store Trafikanalys harvests as per-dataset source files". End commit messages with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- After model/configuration changes run both `scripts/generate/*.py --check`; after workflow edits run `uv run python -m apps.operations.scheduling sync --check`.

## Review Focus

Inputs the spec implies but no existing test pins; each line's test is added to the owning task.

1. **A source returns 200 with an empty or non-JSON body** for one table: the envelope must not be written and the previous envelope must survive (Task 1 `test_put_rejects_empty_and_unserialisable_bodies`; Task 3 `test_empty_body_keeps_previous_envelope`).
2. **A restricted run** (one language, one product, one API, one area) must not garbage-collect shared files or entries outside its scope (Task 1 `test_save_keeps_shared_files_and_untouched_entries`; Task 5 riksbank `test_one_failing_api_keeps_the_others` extended).
3. **Parse with a mapping failure in one dataset** must still write the siblings and report the failed key (Task 2 `test_build_reports_failed_documents_and_writes_siblings`).
4. **A dataset code longer than the filename budget or containing `/`, `|`, `:`** must map to a unique, stable, Windows-safe filename (Task 1 `test_source_filename_is_stable_safe_and_unique`).
5. **The first harvest after cutover** finds an empty manifest next to existing collections: every incremental adapter must fetch everything rather than treat the absence as "unchanged" (kolada/entrysoft/pxweb_v2/pxweb_v1 tasks each add `test_missing_manifest_entry_is_fetched`).

---

## File Structure

New:

- `apps/harvest/source_store.py` — manifest, envelopes, atomic staged save, safety nets.
- `apps/harvest/metadata_adapters/<adapter>/parse.py` ×12 — `documents(store, state, provider, *, portal=None) -> list[dict]`.
- `apps/parsing/__init__.py`, `apps/parsing/build.py` — offline build/apply of accepted collections.
- `apps/parsing/contributions.py`, `apps/parsing/accepted.py` (moved from `apps/harvest/` in Task 9).
- `apps/operations/parsing.py` — CLI `build | apply | publish`, `--adapter | --all`.
- `tests/harvest/test_source_store.py`, `tests/parsing/test_build.py`, `tests/support/parsing.py`, `tests/harvest/<adapter>/test_parse.py`, golden fixtures `tests/fixtures/<adapter>/accepted/*.jsonl.gz`.
- `docs/parsing/README.md`.

Modified: every adapter's `workflow.py` and `state.py`; `apps/operations/harvest.py`; `apps/harvest/storage.py`; `apps/catalog/{assembly,enrichment,dependencies}.py` (imports only); `apps/migrations/*` (imports); `.github/workflows/harvest*.yml`, `refresh-portal.yml`; `docs/architecture.md`, `docs/harvest/README.md`, `docs/harvest/adapter-guide.md`, `docs/catalog/README.md`, `docs/backlog.md`; `tests/integration/test_architecture.py`.

Source tree layout (per adapter):

```text
data/<adapter>/source/manifest.json
data/<adapter>/source/<provider_code>/<quoted dataset code>.<lang>.json        envelope
data/<adapter>/source/<provider_code>/<quoted dataset code>.<lang>.<part>.<ext> extra file (text)
data/<adapter>/source/<provider_code>/_shared/<name>.json                       listings, configs, probes shared by several datasets
```

Manifest:

```json
{"adapter": "trafa", "version": 1,
 "entries": {"trafa/t0801|ar|...|sv": {"files": ["trafa/t0801%7Car%7C....sv.json"], "updated": null, "retrieved_at": "2026-10-16T05:00:00+00:00"}},
 "shared": {"trafa/_shared/structure.T0801.sv.json": {"retrieved_at": "2026-10-16T05:00:00+00:00"}}}
```

Envelope:

```json
{"key": "trafa/<code>/sv", "identifier": "<dataset code>", "language": "sv", "updated": null,
 "retrieved_at": "...", "responses": {"probe": {...}}, "derived": {}}
```

---

### Task 1: SourceStore

**Files:**
- Create: `apps/harvest/source_store.py`
- Test: `tests/harvest/test_source_store.py`

**Interfaces:**
- Consumes: `apps.harvest.storage.write` (atomic JSON), `apps.publication.artifacts.manifest`.
- Produces:
  - `source_key(provider_code: str, dataset_code: str, language: str) -> str` → `f"{provider_code}/{dataset_code}/{language}"`.
  - `split_key(key: str) -> tuple[str, str, str]`.
  - `source_filename(dataset_code: str, language: str, part: str | None = None, suffix: str = ".json") -> str` → `quote(code, safe="")[:65]` + (`"__" + sha256(code)[:12]` only when the quoted code exceeds 65 chars) + `f".{language}"` + (`f".{part}"` if part) + suffix.
  - `class Rejected(ValueError)`.
  - `class SourceStore(root: Path, adapter: str)` with attributes `root`, `adapter`, `manifest: dict` and methods:
    - `entries() -> dict[str, dict]` (manifest entries, read-only view),
    - `get(key) -> dict | None` (full envelope loaded from disk),
    - `put(key, *, responses: dict, updated, retrieved_at: str, derived: dict | None = None, files: dict[str, str] | None = None) -> None` where `files` maps `part` → text body stored as `<name>.<lang>.<part>.<ext>` (`ext` taken from the part name, e.g. `labels.csv`); raises `Rejected` when `responses` is empty, any response is `None`/`""`/`[]`/`{}`, or `json.dumps(..., allow_nan=False)` fails. Rejection leaves the previous entry intact.
    - `remove(key) -> None`,
    - `get_shared(provider_code, name) -> object | None`, `put_shared(provider_code, name, body, *, retrieved_at) -> None` (same rejection rules; name includes its extension, e.g. `listing.sv.json`, `documentation.T0801.html` for `str` bodies),
    - `save() -> None`: stage every changed file to `.<uuid>.tmp`, verify re-read equality, replace only files whose bytes differ, write `manifest.json` last, then delete files under `root` not referenced by `entries[*].files` or `shared` (never touches anything outside `root`). Byte-identical reruns leave mtimes alone.
    - `harvest_manifest(data: Path) -> dict`: `apps.publication.artifacts.manifest(data)` without keys starting with `metadata/` (the harvest's baseline; parsing owns `metadata/`).
  - Envelope dict shape as in File Structure; `updated` stored verbatim.

- [ ] **Step 1: Write the failing tests**

```python
def test_source_filename_is_stable_safe_and_unique():
    assert source_filename("ABAKD11.px", "sv") == "ABAKD11.px.sv.json"
    assert source_filename("inkvartering/Gäster", "sv") == "inkvartering%2FG%C3%A4ster.sv.json"
    a, b = (source_filename("x" * 70 + s, "en") for s in ("1", "2"))
    assert a != b and len(a) <= 65 + 15 + len(".en.json") and "/" not in a
    assert source_filename("t", "sv", part="labels.csv") == "t.sv.labels.csv"

def test_split_key_round_trips_codes_with_slashes():
    assert split_key(source_key("fk", "syn-kommun/Tabell 1", "sv")) == ("fk", "syn-kommun/Tabell 1", "sv")

def test_put_get_save_round_trip_is_deterministic(tmp_path): ...  # bytes equal after second save, manifest sorted
def test_put_rejects_empty_and_unserialisable_bodies(tmp_path):
    # Rejected for {}, None, "", float("nan"); previous envelope and manifest entry unchanged on disk
def test_save_keeps_shared_files_and_untouched_entries(tmp_path):
    # put_shared(...)+put(a)+save(); new store put(b)+save(); shared file and a's file still exist
def test_save_deletes_files_of_removed_entries_only(tmp_path): ...
def test_updated_is_stored_verbatim(tmp_path):
    # int 1730000000000 and None survive round trip with their JSON types
def test_harvest_manifest_excludes_metadata(tmp_path): ...
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/harvest/test_source_store.py -v` → ImportError.
- [ ] **Step 3: Implement `apps/harvest/source_store.py`** per Interfaces. Reuse `storage.write` for each file and the manifest; sort `entries`, `files` and `shared` on save.
- [ ] **Step 4: Run tests** → PASS. `uv run ruff check . && uv run ruff format --check .`
- [ ] **Step 5: Commit** — `git add apps/harvest/source_store.py tests/harvest/test_source_store.py && git commit -m "Add per-dataset source store with adapter manifest"`.

---

### Task 2: `apps/parsing` build, apply and CLI

**Files:**
- Create: `apps/parsing/__init__.py`, `apps/parsing/build.py`, `apps/operations/parsing.py`, `tests/parsing/test_build.py`, `tests/support/parsing.py`, `docs/parsing/README.md`
- Modify: `docs/architecture.md` (ownership table row `apps/parsing/`, data-flow block, command routing row `parsing`)

**Interfaces:**
- Consumes: `SourceStore` (Task 1); `apps.harvest.accepted.write_metadata(root, documents, *, adapter, empty_providers)`; `apps.harvest.validation.validate_metadata(document, adapter=)`; `apps.harvest.artifacts.report_status`; `apps.configuration.providers_for_adapter`; `apps.publication.git.publish`; `apps.portal.snapshot.accepted_records`.
- Produces:
  - Adapter contract: module `apps.harvest.metadata_adapters.<adapter>.parse` with
    - `documents(store: SourceStore, state: dict, provider: dict, *, portal: Path | None = None) -> Iterator[tuple[str, dict | Exception]]` yielding one `(key, worker_document)` pair per manifest entry, or `(key, exception)` when mapping that entry fails, so one bad entry never stops its siblings;
    - `withheld_keys(state: dict) -> set[str]` returning the keys `build` must skip.
    Every per-adapter `parse.py` in Tasks 3-8 implements exactly these two functions; `tests/support/parsing.documents` and `apps/parsing/build.build` are their only callers.
  - `apps/parsing/build.py`:
    - `build(adapter: str, data: Path, output: Path, *, portal: Path | None = None) -> dict` → reads `data/<adapter>/source` and `data/<adapter>/state`, calls the adapter's `documents`, validates each with `validate_metadata`, writes `output/metadata/*.jsonl.gz` through `write_metadata(output, docs, adapter=adapter, empty_providers=<adapter's provider codes>)`, writes `output/reports/parse.json` `{"adapter","status","counts":{"parsed","failed","withheld"},"errors":[{"key","error"}],"collections":[...]}`, and `output/result.json` with `base_manifest` (sha256 map of `data/<adapter>/metadata`) and `manifest` (of `output`); returns the report.
    - `apply(adapter: str, data: Path, output: Path) -> dict` → refuses if `data/<adapter>/metadata` changed since `base_manifest`, then replaces `metadata/*.jsonl.gz` atomically (same staged pattern as `write_metadata` lines 171-179) and copies `reports/parse.json`.
  - CLI `python -m apps.operations.parsing {build,apply,publish} (--adapter NAME | --all) [--data data] [--output output/parsing] [--portal data/data_eu]`; `publish` reuses `publish("Parse <adapter>: regenerate accepted collections", data=data/adapter)`.
  - `tests/support/parsing.py: documents(adapter: str, data: Path, provider: dict | None = None, *, portal=None) -> dict[str, dict]` keyed by `"<language>/<dataset_code>"` — the helper every adapter test uses instead of `load_documents`.

- [ ] **Step 1: Write failing tests** in `tests/parsing/test_build.py` using a fake adapter module registered via `monkeypatch.setitem(sys.modules, "apps.harvest.metadata_adapters.fake.parse", module)` and a legacy document from `tests/model/test_statdcat.py:legacy`:

```python
def test_build_writes_collection_and_report(tmp_path): ...  # metadata/<owner>__multilingual__*.jsonl.gz exists, counts.parsed == 2, status == "success"
def test_build_reports_failed_documents_and_writes_siblings(tmp_path): ...  # one entry raises ValueError → errors has its key, status "completed_with_errors", sibling present
def test_build_skips_withheld_entries(tmp_path): ...
def test_apply_refuses_changed_baseline_then_replaces(tmp_path): ...
def test_cli_build_all_iterates_available_adapters(tmp_path, monkeypatch): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/parsing -v` → fails (module missing).
- [ ] **Step 3: Implement** `apps/parsing/build.py`, `apps/operations/parsing.py`, `tests/support/parsing.py`. `build` imports the adapter's `parse` with `importlib.import_module`; `withheld` = keys whose state status is `withheld` (state shape per adapter is read through `parse.withheld_keys(state) -> set[str]`, a second required function in each `parse.py`, default empty).
- [ ] **Step 4: Run tests** → PASS; ruff clean.
- [ ] **Step 5: Docs** — write `docs/parsing/README.md` (commands, what is raw vs `derived`, that parsing is manual and unscheduled, that PXWeb v1 samples are stripped of observations); add the `apps/parsing/` row and `parsing` command row to `docs/architecture.md`; extend the data-flow block: `harvest -> source files + manifest`, `source files -> parsing -> accepted contributions`.
- [ ] **Step 6: Commit** — "Add offline parsing module that builds accepted collections from source files".

---

### Task 3: Trafikanalys pilot

**Files:**
- Modify: `apps/harvest/metadata_adapters/trafa/workflow.py`, `trafa/state.py`
- Create: `trafa/parse.py`, `tests/harvest/trafa/test_parse.py`, `tests/fixtures/trafa/accepted/<collection>.jsonl.gz` (golden)
- Test: `tests/harvest/trafa/test_workflow.py`

**Interfaces:**
- Consumes: Task 1 `SourceStore`, `source_key`, `harvest_manifest`; Task 2 adapter contract and `tests/support/parsing.documents`.
- Produces (the pattern Task 4 copies):
  - Keys `trafa/<combination.dataset_code>/<lang>`; envelope `responses = {"probe": <DATA_URL body>}`; shared `trafa/_shared/products.json` (`STRUCTURE_URL` listing), `trafa/_shared/structure.<unique_id>.<lang>.json`, `trafa/_shared/documentation.<unique_id>.html`; `updated = None`.
  - `state["datasets"][key]` keeps `checked_at`, `verified_at`, `mapping_version`, `error`, `status`, `withheld_reason`; `state["withheld"]` is removed (withheld = status only).
  - `state.py`: `load_state(root)` reads `state/current.jsonl.gz` directly (no `records()`/`contribution_documents`), `save(root, state)` writes only state; `load_documents` and `collection()` are deleted; `collect()` allowlist becomes `{STATE, REPORT} | {p for p in manifest if p.startswith("source/")}` and both run/collect compare `harvest_manifest(data)` (metadata/ excluded) to `base_manifest`.
  - `parse.py`: `documents(store, state, provider, *, portal=None)` rebuilds `parse_product`, `parse_combinations`, `select_combinations`, `parse_probe` from shared files and calls `mapping.build_dataset` exactly as `_Product.build` does today (including the English `untranslated_items` skip and `reference`/`reference_probe`); `withheld_keys(state)`.
  - `currency.settle` is called with `published`/`withheld` dicts whose values are `True` placeholders instead of documents (signature unchanged; "document is None" now means "no manifest entry").

- [ ] **Step 1: Golden fixture (before any change).** Add `tests/harvest/trafa/test_parse.py::test_parse_reproduces_previous_collection_bytes` skeleton and a one-off generator under `tests/support/trafa.py: write_golden(tmp)` that runs today's `run()`+`collect()` on `RecordedClient` and copies `metadata/*.jsonl.gz` to `tests/fixtures/trafa/accepted/`. Run it once, commit the fixture, delete the generator call (keep the function for regeneration). Commit: "Record Trafikanalys accepted collection as parse golden".
- [ ] **Step 2: Write the failing tests.** In `test_workflow.py` replace `published()` with `tests.support.parsing.documents("trafa", data)`; change `test_rerun_is_byte_identical_and_unchanged` to compare `harvest_manifest(data)` and `source/manifest.json` bytes; withheld tests assert `state["datasets"][k]["status"] == "withheld"`, the envelope file still exists, and the key is absent from `documents()`; add `test_empty_body_keeps_previous_envelope` (RecordedClient returns `{}` for one probe → previous envelope bytes unchanged, error recorded, status stays published if `verified_at` fresh); add `test_restricted_product_run_keeps_other_products_files`. In `test_parse.py` add the bytes equality test: `build("trafa", data, out)` then `(out/"metadata"/name).read_bytes() == golden.read_bytes()`.
- [ ] **Step 3: Run** `uv run pytest tests/harvest/trafa -v` → failures.
- [ ] **Step 4: Implement** `workflow.run`: copy `data/source/**`, `state`, `reports` into `output` (not `metadata/`); open `SourceStore(output/"source", "trafa")`; per product `put_shared` structure(s) and documentation; per dataset `put(key, responses={"probe": body}, updated=None, retrieved_at=stamp)` wrapped in `try/except Rejected` → `fail("metadata", key, error)`; drop `build_dataset`/`validate_metadata` imports; `counts["unchanged"]` = stored envelope responses equal; call `store.save()` before `save(output, state)`. Implement `parse.py` and simplify `state.py`.
- [ ] **Step 5: Run** `uv run pytest tests/harvest/trafa tests/parsing tests/harvest/test_source_store.py -v` → PASS; ruff clean.
- [ ] **Step 6: Commit** — "Store Trafikanalys harvests as per-dataset source files".

---

### Task 4: Remaining single-worker adapters (one commit each)

Apply the Task 3 pattern to sdb, uka, smhi_emissions, forsakringskassan, riksbank, dataanalys, tillvaxtverket, in that order. Each sub-task: golden fixture commit first (`tests/fixtures/<adapter>/accepted/`), then tests, then implementation, then commit "Store <Adapter> harvests as per-dataset source files". Shared steps per adapter:

- [ ] Golden fixture from the pre-change `run()` using the adapter's existing fake (`tests/support/<adapter>.py`).
- [ ] `test_workflow.py`: `published()` → `tests.support.parsing.documents`; byte-identical rerun → `harvest_manifest` + manifest bytes; withheld/restore → status assertions; "mapping failure keeps document" tests become "raw stored, parse reports error for that key, siblings parsed" in `test_parse.py`; add `test_parse_reproduces_previous_collection_bytes`.
- [ ] `workflow.run` writes `SourceStore`; `state.py` loses `records()`/`load_documents`/`write_metadata`; `collect()` allowlist and baseline per Task 3.
- [ ] `parse.py` with `documents` and `withheld_keys`.
- [ ] `uv run pytest tests/harvest/<adapter> -v` green; ruff clean; commit.

Adapter specifics (what to store, the `updated` value, and the refresh comparison to rewrite):

| Adapter | Shared files (`_shared/`) | Per-dataset `responses` (+`derived`) | `updated` | Refresh rewrite |
| --- | --- | --- | --- | --- |
| **sdb** | `listing.<lang>.json`; `probe/<quote(namn)>.json` = `{"language", "first": body, "years": {year: body\|null}}` (probe done once per subject, shared across languages) | `variables`, `categories` (variable → body), `resultat`, `listing_row` | `null` | none; split `probe.probe_results` into fetch + pure `reconcile(first, years, variables, categories) -> ResultProbe` (`probe.py:72-171`), recompute `added` from stored `years` so `datetime.now().year` candidates reproduce. Parse reads `time_unit` from `listing.sv.json`. |
| **uka** | `indicators.json` | `totals`, `links` (period → link body), `exports` (period → `{"url","last_modified"}` or `null` for 404); `derived.scan` = per-period `{layout, paths, markers, conflicts, rows}` exactly what `mapping.Scan.add` consumes (`mapping.py:70-113`) | `null` | `workflow.py:429` `accepted is not None` → `store.get(key) is not None`; `state["indicators"][code]` (`files`, `source_digest`, `mapping_version`, `skipped_periods`) stays. Add `Scan.from_summary(derived)` so parse rebuilds the scan without CSVs. Update the "never stored" sentences in `workflow.py:5-7`, `client.py:1`, `docs/harvest/adapters/uka.md`. |
| **smhi_emissions** | `filteroptions.json` | `getdata` | `body["primary_submission"]` verbatim | none (always rebuilt); quarantined substances have state only. |
| **forsakringskassan** | `reports.json`, `texts.json`, `meta/<quote(report)>.json` | `probe` (only for undeclared-dimension tables) | `null` | `workflow.py:538-543` `all(key in published or withheld)` → `store.get(key) is not None`; `:635` previous document → envelope; `known()` `:343-350` unions state and manifest keys. Parse recomputes `meta_sha256`/`texts_sha256` with `source.fingerprint`. Excluded tables get no entry. |
| **riksbank** | `swea_groups.json`, `swea_series.json`, `mpd_series_ids.json`, `mpd_policy_rounds.json`, `swestr_latest.json` | swea `latest`; mpd `vintage`; swestr none (entry with `responses={"latest": shared body}`) | swea max row `date`; mpd last policy round; swestr `latest["date"]` | `_known_selected` `:335-346` reads series aliases from stored `swea_series.json` rows, not from documents. Extend `test_one_failing_api_keeps_the_others` to assert the failed API's shared files survive. |
| **dataanalys** | `charts.json`, `dashboards.json` (lists of page bodies), `catalogue/<AREA>.json` = `{"columns", "body"}`, `columns/<AREA>.json` = `{"columns", "body"}` | `cube` | catalogue `DataGenerationDate` verbatim (number) | none; split `discover` into `_list` fetch + pure `interpret(chart_rows, dashboard_rows)`; `_Harvester.catalogue/columns` keep the HTTP-400-driven column decisions and store the chosen `columns` beside the body. |
| **tillvaxtverket** | none | `columns`, `load_dates`, `regions`, `dimensions` (id → body), `periods`; `files={"labels.csv": text}` | `Probe.loaded` (latest `LOAD_DATE`) | `:386-392` `source_record == probe.signal()` → `parse_probe(spec, stored["columns"], stored["load_dates"]).signal()`; reuse path re-puts the unrefetched stored responses; `previous_labels` reads the stored CSV. **Delete** `DCAT_API` use (`api.py:33,57-58`, `workflow.py:119-120, 360-374`, `mapping.previous_dcat`); `parse.documents(..., portal=)` loads `accepted_records(portal)` once, indexes by `id`, and calls `apply_dcat(cube, spec, parse_dcat({"result": record}) if record else None)`. Move the two DCAT tests to `test_parse.py` using a tmp snapshot built with `apps.portal.snapshot.import_snapshot` from `RECORDED[dcat_key(name)]["result"]`. |

---

### Task 5: Kolada

**Files:**
- Modify: `kolada/workflow.py`, `kolada/state.py`, `tests/support/kolada_fixture.py`, `tests/harvest/test_kolada.py`, `tests/harvest/test_kolada_workflow.py`
- Create: `kolada/parse.py`, `tests/harvest/kolada/test_parse.py` (or extend `test_kolada.py`), golden `tests/fixtures/kolada/accepted/rka__multilingual__2e4e3dbc2836.jsonl.gz`

**Interfaces:**
- Shared: `kolada/_shared/kpi.json`, `ou.json`, `municipality_groups.json`, `kpi_groups.json` (concatenated `values` rows).
- Entry key `kolada/<scope>/<kpi_id>/sv`; `responses = {"kpi": <kpi row>}`; `derived = {"coverage": {...}, "empty": bool, "harvested_on": "YYYY-MM-DD"}`; `updated = kpi.get("publication_date")`.
- `state`: keep `kpis` (truncated-catalogue guard), `scopes[code] = {references_digest}` (new field, replaces reading `references_digest` from documents), `pending`, `refreshed`; drop `scopes[*].coverage/empty` (now in `derived`) and `references` (recomputed from shared files at load via existing `reference_rows`).
- `parse.documents` builds `references` from shared files, calls `metadata.build_dataset(kpi, scope, coverage, references, updated=updated_for(kpi, years[-1], today=harvested_on, portal_modified=None))`. Note `portal_modified` is never populated today; drop the parameter.

- [ ] Golden fixture from `tests/support/kolada_fixture.initial_state` + today's `run()`.
- [ ] Tests: `test_kolada_workflow.py:63,85` → manifest bytes; `kolada_fixture.initial_state` seeds `SourceStore` entries + state; `test_kolada.py:239-285` → `documents()`; rename `test_missing_document_rebuilds_from_saved_coverage` → `test_missing_manifest_entry_is_fetched_or_rebuilt_from_coverage`; keep `test_metadata_only_change_does_not_fetch_values` and `test_unchanged_reuses_seed_and_advances_refresh`; add `test_budget_expiry_keeps_partial_source_entries`; add equality test.
- [ ] Implement: `workflow.py:259-271` reuse test → `entry = store.get(key)`; `entry is None → pending rebuild|rescan`; `entry["responses"]["kpi"] != kpi or state["scopes"][code].get("references_digest") != context_digest(context)` → rebuild; `BudgetExpired` path saves the store with the entries written so far.
- [ ] `uv run pytest tests/harvest/test_kolada.py tests/harvest/test_kolada_workflow.py -v` green; commit "Store Kolada harvests as per-dataset source files".

---

### Task 6: PXWeb v2

**Files:**
- Modify: `pxweb_v2/workflow.py` (`load_stores:56-61`, `_write_candidate:81-147`, plan `:290-344`, accept `:389-395`, removal `:416-419`, `_validate_reconciliation:550-615`, `collect:618-700`), `tests/harvest/test_pxweb_v2_workflow.py`
- Create: `pxweb_v2/parse.py`, `tests/harvest/pxweb_v2/test_parse.py`, golden fixture

**Interfaces:**
- Shared `scb/_shared/config.json` (`max_cells`); entry key `scb/<table id>/<lang>`, `responses = {"metadata": body}`, `updated = row["catalogue"]["updated"]`.
- **Keep** `data/pxweb_v2/codelists/*.jsonl.gz` (already raw, a catalog input via `apps/catalog/inputs.py:14,30`) and `table-records/` (listing rows, the `updated` source) exactly as today.
- Reuse test: `store.get(key)` exists, `entry["updated"] == row["catalogue"]["updated"]`, `listings.records.get(name) == row`, and `validate_metadata(stored["responses"]["metadata"], row)` native branch passes (`validation.py:54-106`); codelist checks use `codelist_ids` on the native body.
- `parse.documents` = `pxweb_v1.mapping.map_metadata(body, table_record(provider, lang, listing_row), "pxweb_v2", max_cells=config)` per entry.
- `collect` validates each artifact envelope with the native branch against `listings.records[name]`, applies via `SourceStore.save()`; allowlist adds `source/manifest.json` and `source/scb/*.json`.

- [ ] Golden fixture from `tests/harvest/test_pxweb_v2.py` helpers (`metadata()`, `table()`, `codelist()`, `CONFIG`).
- [ ] Tests: `:129-134,148,172` → source entries + `documents()`; `metadata_bytes()` → source bytes; keep "unchanged run makes no `/metadata` request"; add `test_missing_manifest_entry_is_fetched`; add equality test (golden `max_cells` from `CONFIG["maxDataCells"]`).
- [ ] Implement; `uv run pytest tests/harvest/test_pxweb_v2*.py -v` green; commit "Store PXWeb v2 harvests as per-table source files".

---

### Task 7: EntryScape (entrysoft)

**Files:**
- Modify: `entrysoft/workflow.py` (`rebuild:113-152`, `publish:155-170`, `settled:484-488`, change test `:494-530`, error branch `:546-556`, `collect:660-680`), `entrysoft/state.py`, `entrysoft/datasets.py` (add `trim_structure`), `tests/harvest/entrysoft/test_workflow.py`
- Create: `entrysoft/parse.py`, `tests/harvest/entrysoft/test_parse.py`, golden fixtures (two providers → two collections)

**Interfaces:**
- Entry key `<provider>/<entry uuid>/sv`; `responses = {"discovery": record}` (EntryStore search hit); `derived = {"structure": trim_structure(infer_structure(...))}` where `trim_structure` keeps, per column with more than 1,000 values, `categories[:200]`, `distinct` and `range` — exactly what `datasets.trim` (`:160-171`) keeps, so `trim(table_document(record, structure))` is byte-identical; `updated = record["published"]`.
- `state/tables.jsonl.gz` rows keep `key, status, markers, rowcount, strategy, requests, reason, paging_requests, harvested_at, error`; drop `record` and `document`.
- `settled` additionally requires `store.get(key) is not None` for `status == "ok"`.
- `parse.documents` runs today's `rebuild()` grouping (`build_datasets` + `to_document`) from store entries; `harvested_documents` and `within_catalog_scope` read the store.
- `collect` stops pruning `metadata/` collections (`:673-680`); allowlist `{TABLES, CURRENT, REPORT} ∪ source/**`.

- [ ] Golden fixtures from `FakeEntryScape`.
- [ ] Tests: every `collection_store(data).records` assertion → `documents()`; `load_tables(data)[key]` has no `document`/`record`; `store.get("alpha/<uuid>/sv")["responses"]["discovery"]["title"]` set; `test_parse.py::test_regrouping_needs_no_requests` (changed `MAPPING_VERSION`, no client); `test_missing_manifest_entry_is_fetched`; equality test.
- [ ] Implement; `uv run pytest tests/harvest/entrysoft -v` green; commit "Store EntryScape harvests as per-table source files".

---

### Task 8: PXWeb v1

**Files:**
- Create: `pxweb_v1/assemble.py` (pure: `_assemble`, `_project`, `_merge`, `_variables(complete=True)`, `_categories` moved from `metadata.py:268-509`), `pxweb_v1/parse.py`, `tests/harvest/pxweb_v1/test_parse.py`, golden fixture
- Modify: `pxweb_v1/metadata.py` (split `_generate:512-654` into `fetch_raw(requests, table) -> {"config","metadata","samples","queries","table"}` and nothing else), `pxweb_v1/workflow.py` (`prepare:33-89`, `collect_refresh:254-527`, `build_provider:530-681`, `collect_metadata:684-831`), `apps/harvest/storage.py` (`validate_dataset`, `refresh_identity` move to parse side; `group_records` stays), `tests/harvest/test_workflow.py`, `tests/harvest/test_metadata.py`

**Interfaces:**
- Shared `<provider>/_shared/config.<quote(api root)>.json`; entry key `<provider>/<dataset_code>/<lang>`; `responses = {"metadata": GET body, "table": listing row at harvest time}`; `derived = {"samples": [<_read_sample output>...], "queries": [payload...]}` (POST samples stripped of `value`/`status`/`index` — observations never stored); `updated = record["published"]`.
- `build_provider` writes a per-task `SourceStore(output, "pxweb_v1")`; `collect_metadata` validates each artifact envelope with new `validate_raw(envelope, record)` (identity, `_variables(metadata)`, `envelope["responses"]["table"]["published"] == record["published"]`) and `put`s into `data/pxweb_v1/source`, `save()` at today's `:804`.
- `collect_refresh` reuse test (`:400-421`): `entry = store.get(key)`; reusable iff `entry is not None and not full and name not in previous_failures and entry["updated"] == new["published"]`; drop the `refresh_identity` block at `:383-390` (applied at parse time); `:429-442` invalid-saved branch → `validate_raw` failure.
- `parse.documents`: `assemble(...)` + `map_metadata(result, record, "pxweb_v1", max_cells=config.get("maxCells", _CELL_LIMIT))` then `refresh_identity(doc, current_listing)` when `current["published"] == stored table["published"]`.
- Checkpoint compatibility: write `"layout": 2` into `work/current.json` (`:501-519`); `prepare` (`:35-41`) refuses a checkpoint without it with "Checkpoint predates per-dataset source layout; finish or discard the run before upgrading". **Cutover must land between pxweb_v1 runs** (live checkpoint is at wave 1 with ~7,372 pending tasks); the implementer checks `data/pxweb_v1/work/current.json` is absent on `main` before committing this task, otherwise waits.

- [ ] Golden fixture from `tests/harvest/test_workflow.py:seed/dataset` helpers.
- [ ] Tests: `seed()` `:104-112` seeds envelopes (`{"metadata": native without extension, "samples": [], "table": row}`); `:125` queue test; `:270-291` → store entry + `documents()`; `:294-312` moves to `test_parse.py` (`refresh_identity` semantics); `:315` patches `fetch_raw`; `tests/harvest/test_metadata.py` becomes `fetch_raw` + parse-side assembly equality against today's `get_pxweb_metadata` output (local `website` fixture); add `test_prepare_refuses_pre_layout_checkpoint`; add `test_missing_manifest_entry_is_fetched`; add equality test.
- [ ] Implement; `uv run pytest tests/harvest/test_workflow.py tests/harvest/test_metadata.py tests/harvest/pxweb_v1 -v` green (slow marks apply); commit "Store PXWeb v1 harvests as per-table source files".

---

### Task 9: Move StatDCAT conversion out of `apps/harvest`

**Files:**
- Move: `apps/harvest/contributions.py` → `apps/parsing/contributions.py`; `apps/harvest/accepted.py` → `apps/parsing/accepted.py`; the StatDCAT (`dcat:Dataset`) branch of `apps/harvest/validation.py` → `apps/parsing/validation.py` (harvest keeps the native/worker-document branch only).
- Modify imports: `apps/catalog/assembly.py:24-28`, `apps/catalog/enrichment.py:12`, `apps/catalog/dependencies.py:48-59` (code-hash list now names the parsing modules; this intentionally forces one full catalog rebuild), `apps/harvest/storage.py` (drop `contribution_documents`/`write_metadata`; `JsonlStore.__init__` JSON-LD branch `:219-227` and `save` metadata branch `:249-260` deleted; `migrate_storage` iterates `table-records` only), `apps/migrations/{collections,verification,audit,profile2}.py`, `scripts/checks/check_statdcat_upstream.py`, `scripts/live/check_statdcat_live.py`, `apps/harvest/metadata_adapters/*/__init__.py` (`fetch_metadata` wrappers import `as_contributions` from `apps.parsing.accepted`), tests listed by grep (`tests/support/kolada_fixture.py`, `tests/configuration/test_configuration.py`, `tests/catalog/*`, `tests/publication/test_candidates.py`, `tests/harvest/test_accepted.py` → `tests/parsing/test_accepted.py`, `tests/model/test_statdcat.py`, `tests/retrieval/test_retrieval_distribution.py`, `tests/harvest/test_storage.py:135`).
- Test: `tests/integration/test_architecture.py` — add `test_harvest_does_not_import_parsing` (AST walk of `apps/harvest/**`) and `test_parsing_does_not_import_catalog`.

- [ ] Write the two architecture tests; run → fail (harvest still imports `accepted` via adapters' `__init__`).
- [ ] `git mv` the modules; update imports with Grep for `apps.harvest.contributions|apps.harvest.accepted`; delete dead code in `storage.py`.
- [ ] `uv run pytest -m "not slow"` green; `uv run python scripts/generate/generate_schema_reference.py --check` and the other generator `--check`; `uv run python scripts/checks/check_distribution.py`; ruff clean.
- [ ] Commit "Move StatDCAT conversion from harvesting to parsing".

---

### Task 10: Workflows, docs, backlog

**Files:**
- Modify: `.github/workflows/harvest.yml`, `harvest-*.yml` (11), `refresh-portal.yml` — delete the `gh workflow run catalog.yml` step (and its `if` guard that checks for a queued catalog run); `catalog.yml` stays `workflow_dispatch`.
- Modify: `docs/architecture.md` (Workflows paragraph: harvests no longer dispatch the catalog; "accepted contributions" produced by parsing), `docs/harvest/README.md:24-27` (accepted-path paragraph → source layout + manifest; `:72-74` dispatch sentence), `docs/harvest/adapter-guide.md` (boundaries: `parse.py` contract, raw vs derived, safety nets), `docs/catalog/README.md` (inputs come from `parsing apply`), `docs/harvest/adapters/uka.md`, `tillvaxtverket.md`, `kolada.md`, `entrysoft.md`, `pxweb-v1.md`, `pxweb-v2.md` (per-adapter storage notes), `docs/backlog.md` (delete both *Now* items; move nothing else).

- [ ] Edit workflows; run `uv run python -m apps.operations.scheduling sync --check` → unchanged (it manages only schedule/enablement blocks).
- [ ] Update docs; run both `scripts/generate/*.py --check`.
- [ ] `uv run pytest -m "not slow"`; ruff clean.
- [ ] Commit "Stop dispatching the catalog from harvests and document the parsing split".

---

## Verification (end to end)

1. `uv run pytest` (full offline suite, including slow workflow modules) green on `main`.
2. Local dry run with the real tree on a scratch copy, no network for parsing:
   ```sh
   uv run python -m apps.operations.harvest run --adapter trafa --data data/trafa --output output/trafa   # live, ~400 requests
   uv run python -m apps.operations.harvest collect --adapter trafa --data data/trafa --output output/trafa
   uv run python -m apps.operations.parsing build --adapter trafa --data data --output output/parsing/trafa
   cmp output/parsing/trafa/metadata/trafa__multilingual__*.jsonl.gz data/trafa/metadata/trafa__multilingual__*.jsonl.gz
   ```
   Expected: `data/trafa/source/manifest.json` lists every Trafikanalys dataset with files and `retrieved_at`; the parsed collection is byte-identical to the committed one (Trafikanalys publishes no update signal, so content should match unless the source changed that day; a diff is inspected, not assumed wrong).
3. `uv run python -m apps.operations.catalog build --data data --output output/catalog` still succeeds against unchanged `metadata/` inputs.
4. `uv run python scripts/checks/check_distribution.py` (retrieval install unaffected).
5. First scheduled runs after merge: confirm each adapter's collect job publishes `source/**` and `state/`, never `metadata/`, and that `kolada`, `entrysoft`, `pxweb_v2` and `pxweb_v1` perform a full refetch on their first run (manifest empty).
