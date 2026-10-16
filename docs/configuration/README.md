# Source configuration and StatDCAT vocabulary

Edit these three authoritative files together:

| File | Definition |
| --- | --- |
| [adapters.json](../../config/adapters.json) | Twelve adapter definitions, supported construction parameters and operational defaults |
| [organisations.json](../../config/organisations.json) | Organisation identities, multilingual names, identifiers and organisation metadata |
| [catalogs.json](../../config/catalogs.json) | Catalogs and concrete service bindings, referencing organisations and adapters |

An organisation is an entity. Publisher is its role with respect to a catalog,
dataset or service. Those roles are independent: a catalog publisher is not
automatically the publisher of every dataset, or the operator of a hosting service.
Organisation models use the W3C Organization class, an Agent subtype.

Catalogs list services through `dcat:service` and datasets through `dcat:dataset`
(the dataset's inline `catalogs` context supplies the reverse relationship).
Distributions reference concrete services through `dcat:accessService`. A service
has its own stable identity, `dcat:endpointURL`, applicable `dct:publisher` and
`dcat:servesDataset` relationships, plus the typed `ni:adapter` connection.

For example, SCB's PXWeb v2 and HVD v1 services reference `scb` and different adapter
definitions. Göteborg's EntryScape and PXWeb catalogs reference `goteborg` and
separate concrete services. One service binding may appear in multiple catalogs.

## Loading and changing configuration

`load_configuration(directory)` validates all three files and their references.
Without a directory it reads `config/` relative to this checkout. Installed retrieval
does not load source configuration. `--config DIRECTORY` selects another complete
configuration; `--providers FILE` explicitly imports an existing legacy list.
The two command options are mutually exclusive.

`providers_for_adapter(adapter, configuration=...)` supplies existing workers
with their established dictionaries. Adapter defaults are overridden by explicit
binding options, including `0`, `False` and empty lists. Source ordering is explicit.
Unknown JSON-compatible options remain available to adapters. Validators do not
perform discovery, network requests or persistence.

Use `source_code` for routing aliases, `organisation` for entities, `publisher`
for a resource-specific relationship, and catalog/service identifiers for those
resources. Existing `provider_code` selectors, checkpoint keys and retrieval entry
points remain compatibility aliases. `SourceIdentity.provider_code` exposes the
same value as `source_code`; profile 2 serializes `source_code`.

When adding a source, reference an existing organisation or add its verified
identity, select an adapter and supply its endpoint/options in a service binding,
then reference that binding from the actual catalog. Do not derive organisations
from hostnames. The `identity_namespace` preserves minted dataset/distribution IDs;
it is not an attribution statement.

## EntryScape provenance and scope

Catalog scopes retain EntryStore context URLs, known dataset codes and discovery
origins. Digidel and Klimatbyrån share `free.entryscape.com` but have distinct
catalogs and organisation references. Their original discovery origin was
`dataportalse`; this does not make the aggregator their dataset publisher.

Discovery filters shared-host results by those scopes. A complete scoped discovery
cannot delete accepted tables outside that scope. Previously accepted records
without sufficient attribution are retained with findings. Their host never
becomes an invented organisation or an unowned-catalog configuration category.
The original [host evidence](../reference/entryscape-origins/rowstore_hosts.csv) and
[dataset publisher evidence](../reference/entryscape-origins/data_eu_rowstore_datasets.csv)
are bundled for audit, not runtime input.

## Generated artifacts and profile compatibility

```powershell
uv run python scripts/generate/generate_configuration.py
uv run python scripts/generate/generate_schema_reference.py
uv run python scripts/generate/generate_configuration.py --check
uv run python scripts/generate/generate_schema_reference.py --check
```

These commands generate JSON Schema, the source inventory and model documentation.
CI checks for drift. Edit the files in `config/` and the Pydantic models. No packaged
configuration copies are generated. The old provider schema is only a routing compatibility
envelope; it is not an organisation contract.

The generated [configuration field reference](reference.md) defines
construction fields; the [metadata reference](../model/reference.md)
lists standard vocabulary aliases and NordicIntel (`ni:`) extensions.

Worker artifacts are plain JSON; nothing rewrites accepted production files.

The supplied [StatDCAT-AP 3.0.0 specification](<../reference/StatDCAT-AP 3.0.0.html>) is
stored verbatim with a [hash and provenance](<../reference/StatDCAT-AP 3.0.0.provenance.json>).
Its referenced images/styles were not bundled in the supplied HTML. The upstream
revision pinned in [profile.json](../../config/profile.json) remains
independent of this reference snapshot. Upstream descriptive requirements are
quality findings; identities, retrieval structures and information preservation
remain publication gates.
