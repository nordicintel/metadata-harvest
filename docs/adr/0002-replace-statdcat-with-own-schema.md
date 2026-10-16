# ADR 0002: Replace StatDCAT with a self-defined JSON schema

Status: proposed, intent only (2026-10-16)

## In one paragraph

The accepted metadata model will stop being StatDCAT. It will be one common schema
of our own: similar to DCAT-AP in vocabulary, fully JSON, multilingual by
construction, with typed subtypes for each kind of data source. The schema is not
designed yet. This ADR records the direction so that no further work deepens the
StatDCAT-specific code, and so the harvest-to-parse seam is kept as the place where
the new model will plug in.

## Why

StatDCAT is an RDF profile forced into JSON-LD. It costs the repository a large
conversion module (`apps/parsing/contributions.py`), a profile reader with upgrade
paths, JSON-LD vocabulary handling, and catalog assembly that reasons about graphs.
None of that serves the actual consumers, which want tables with dimensions,
categories and codelists in more than one language.

## What the new model will be

Decided in intent, to be designed in a later session:

- **One schema**, JSON, DCAT-AP-like naming, multilingual text as a first-class
  type rather than language-tagged literals.
- **Subtypes** per kind of source so each adapter describes exactly what its
  provider offers.
- **A common tabular model** close to how PXWeb v2 models a table: the listing
  entry, the JSON-stat2 style metadata (dimensions, categories, units, time), and the
  codelists, all multilingual, with normalisation of dimensions and categories built
  in. Nearly every adapter fits this.
- **A more flexible tabular form** for the sources that do not fit, such as some
  Trafikanalys entries and the Dataanalys entries not supported today. SDMX is the
  candidate to evaluate.

## Consequences now

- Do not add depth to `apps/model/` StatDCAT models, `convert_dataset`,
  `legacy_document`, or the JSON-LD vocabulary. Delete where a refactor touches them.
- Keep the seam from ADR 0001 intact: harvesting stores provider responses as files;
  parsing turns files into the model. The new model replaces only the right-hand
  side of parsing.
- Identity (owner, service, catalog, stable IDs) is model-independent and is being
  moved into a `registry/` module first. The new schema will reference registry
  identities, not mint its own.
- Accepted collections are regenerated from source files when the model changes.
  There is no upgrade path and none is wanted.

## Open, for the schema session

- Exact field set and naming.
- How subtypes are declared and validated.
- Whether SDMX structures are embedded, referenced, or translated.
- What the catalog build becomes once datasets are plain JSON records.
