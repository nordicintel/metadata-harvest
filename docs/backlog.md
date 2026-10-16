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

- [ ] **harvest** Separate harvesting from parsing and catalog building — top
  priority. Harvests only retrieve source data, with no more interpretation than
  refresh logic needs: no StatDCAT conversion and no combining with data.europa.eu
  or Dataportalen DCAT-AP records. StatDCAT parsing and catalog building become a
  separate module that is not scheduled, run only for large-scale testing or
  production, which is a long way off. This was missed when StatDCAT was
  implemented and again in the codebase restructure
  ([architecture](architecture.md)).
- [ ] **harvest** Store harvests as one file (or more when needed) per
  dataset/table instead of combined collections, implemented together with the
  harvest/parsing separation — deterministic names and locations make it simple
  to find and replace only what a refresh changes; combining waits for catalog
  building. Add a small JSON manifest per adapter listing each identifier, its
  file location(s) and last update date for refresh checks.

## Next

- [ ] **vardenisiffror** Add an adapter for Vården i siffror's new API — 1000+
  indicators/datasets that are not in data.europa.eu or Dataportalen; next after
  the harvest/parsing separation
  ([API documentation](https://vardenisiffror.se/dataexport/api-dokumentation)).

- [ ] **harvest** Re-evaluate tables previously dismissed as incompatible with
  JSON-stat2 — after the switch to StatDCAT they may now qualify for inclusion.
- [ ] **kolada** Consider separating municipality and OU tables into distinct
  datasets again instead of one combined dataset — better discoverability.
- [ ] **catalog** Speed up the catalog build — runs take ~14 minutes, ~12 of
  them rebuilding the whole SCB group (5,000 datasets) when one of its
  collections changes; unchanged groups are already reused
  ([catalog.yml](../.github/workflows/catalog.yml)).

- [ ] **repo** Replace gzip with zstd everywhere in the codebase — some gzip
  files are approaching the 100 MB file-size limit (Kolada RKA at 91 MB,
  SCB at 80 MB); publication archives already use zstd.

- [ ] **model** Add a helper to the StatDCAT pydantic models that converts a
  StatDCAT object directly into a JSON-stat2 dataset — return a plain
  dict/JSON, not another custom model; wanted as a core capability
  ([apps/model/](../apps/model/)).

## Later

## Ideas

- [ ] **model** Investigate switching from `jsonschema` to `fast-jsonschema` —
  faster validation, if it supports the drafts and features we rely on
  ([validation.py](../apps/model/validation.py)).
