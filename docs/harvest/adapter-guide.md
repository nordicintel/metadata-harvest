# Implementing a metadata adapter

A new adapter is written by copying the shape, not another adapter. One folder:

```text
apps/harvest/metadata_adapters/<name>/
├── __init__.py      empty
├── client.py        HTTP calls to the provider: URLs, parameters, decoding
├── harvest.py       ADAPTER: how to discover what exists and fetch one item
├── parse.py         documents() and withheld_keys(): source files -> documents
└── mapping.py       optional: pure functions shared by harvest.py and parse.py
```

Adapters may add private modules for their own use. Nothing outside the folder
imports them, except the live check scripts, which may also import `client.py` and
`ADAPTER`. Add the name to `ADAPTERS` in `apps/harvest/metadata_adapters/__init__.py`,
a definition to `config/adapters.json`, a service binding to `config/catalogs.json`,
a schedule and a `harvest-<name>.yml` workflow.

## The adapter object

`harvest.py` exposes one `harvest.lifecycle.Adapter` named `ADAPTER` with:

- `name`, `provider_code`, `workflow_file`, the state `collection` name, extra
  `counts`, the CLI `options` it accepts (flag to selection name, for example
  `{"subject": "subjects"}`) and its `languages`;
- `client(binding)`, a factory for the provider client (`client.py`);
- `discover(run)`, returning scopes (`Scope(name, load)`) whose `load()` lists the
  scope's tasks (`Task(key, item)`), storing shared source files through `run.store`;
- `fetch(run, task)`, returning `Stored(responses, derived, updated, files, info)`,
  `Kept()` when the adapter's own change signal proves the stored entry current, or
  `Dropped(count)` when the item must go;
- optionally `forget(run, key)` for companion files, `scope_of(key)` for the scope a
  known key belongs to, and `scope_failures(report)` for the outcome check.

A key is `<language>/<code>`. Raising `Unreachable` (the client's subclass of
`harvest.lifecycle.Unreachable`) is an endpoint failure, retried once after the
cooldown and then withheld; any other exception is a metadata failure that keeps
the stored entry published while it is fresh. The lifecycle owns everything else.
An adapter whose provider does not fit the loop sets `run` (and `collect`) on the
object instead and uses `lifecycle.prepare` and `lifecycle.finish`.

## Parsing

`parse.py` provides `documents(store, state, bindings, *, portal=None)` yielding
`(key, document | exception)` for every source entry, and `withheld_keys(state)`.
Documents are worker documents validated by the
[tabular schema](../../schema/tabular.schema.json): `id` orders dimensions, `size`
gives category counts, `value: []` means metadata only, routing identity and URLs
live in `extension.nordicintel`, adapter evidence under its own namespace. Preserve
codes, labels, units, notes, hierarchy, role and elimination settings.

Keep metadata inference and observation probes in the adapter. Consumer
request/response code goes in `retrieval/data_adapters/`; harvesting must not call
the consumer dispatcher.

## Tests

Each adapter has `tests/harvest/<name>/` with `test_harvest.py` (a discover test, a
fetch test and one end-to-end run that collects, parses and compares the golden
under `tests/fixtures/<name>/accepted/`), `test_parse.py` and `test_mapping.py`.
Recorded responses and fixtures live in `tests/support/<name>.py`. No test asserts
production inventory counts. The lifecycle itself is tested once, with a toy
adapter, in `tests/harvest/test_lifecycle.py`.
