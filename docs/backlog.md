# Backlog

Open work only: bugs, fixes, features and investigations not yet done.

## How to use this file

- **One line per item**: `- [ ] Short imperative title — why it matters (link).`
  Link the issue, run, file or doc that holds the detail; keep detail out of here.
- **Sections are priority**: *Now* is being worked on or next up, *Next* is
  agreed and ready, *Later* is wanted but unscheduled, *Ideas* is unvetted.
- **Keep *Now* short**: at most five items; move an item up or down instead of
  reordering within a section.
- **Done means deleted**: remove the item in the commit or PR that completes it;
  Git history is the record. Never keep a "Done" section.
- **Dropped means deleted too**: say why in the commit message.
- **Tag the owning area** first, matching `apps/` or the adapter name, e.g.
  `**kolada**`, `**catalog**`, `**ci**`, `**retrieval**`.
- **Rules live elsewhere**: behaviour and decisions belong in their owning guide
  (see [Documentation](README.md)); this file only says what is still to do.

## Now

## Next

- [ ] **kolada** Review and optimize the refresh workflow — primarily through
  better refresh logic, possibly through parallel requests
  ([workflow.py](../apps/harvest/metadata_adapters/kolada/workflow.py)).
- [ ] **harvest** Re-evaluate tables previously dismissed as incompatible with
  JSON-stat2 — after the switch to StatDCAT they may now qualify for inclusion.
- [ ] **retrieval** Support PXWeb v1 APIs without JSON-stat2 output — wrap the
  retrieval function to recognise those organisations, request JSON-stat (v1,
  always supported) and convert it to JSON-stat2 so the normal workflow is
  unaware of the switch.
- [ ] **pxweb_v1** Fix tables found at multiple paths with different variables
  (e.g. Energimyndigheten) — record multiple paths only when variables match;
  otherwise keep the most recently published/updated instance as canonical and
  ignore the others with the same code.
- [ ] **kolada** Consider separating municipality and OU tables into distinct
  datasets again instead of one combined dataset — better discoverability.
- [ ] **catalog** Speed up the catalog build and make it report progress — it
  runs for 28+ minutes with no logs; idea: rebuild only the files that changed
  since the last build ([catalog.yml](../.github/workflows/catalog.yml)).

- [ ] **repo** Replace gzip with zstd everywhere in the codebase — some gzip
  files are approaching the 100 MB file-size limit.

- [ ] **model** Add a helper to the StatDCAT pydantic models that converts a
  StatDCAT object directly into a JSON-stat2 dataset — return a plain
  dict/JSON, not another custom model; wanted as a core capability
  ([apps/model/](../apps/model/)).

## Later

## Ideas

- [ ] **model** Investigate switching from `jsonschema` to `fast-jsonschema` —
  faster validation, if it supports the drafts and features we rely on
  ([validation.py](../apps/model/validation.py)).
