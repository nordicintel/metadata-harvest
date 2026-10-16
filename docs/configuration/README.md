# Source configuration

Edit these three files together; `registry.load()` reads them once, validates each
against [source-configuration.schema.json](../../schema/source-configuration.schema.json)
and checks the cross-references.

| File | Definition |
| --- | --- |
| [adapters.json](../../config/adapters.json) | Twelve adapter definitions: title, description, option parameters and defaults |
| [organisations.json](../../config/organisations.json) | Organisation codes with their identity and multilingual name |
| [catalogs.json](../../config/catalogs.json) | Service bindings (adapter, source code, endpoint, publisher, order, options) and the catalogs that list them |

The registry answers two questions: `bindings(adapter)` gives the adapter's
`ServiceBinding`s in configured order, with the adapter defaults overlaid by the
binding's own options (including `0`, `False` and empty lists), and `owner(binding)`
gives the publishing organisation. A binding's `label` is the title of the one
catalog listing it, or the endpoint host when the service is shared. Nothing else is
derived: no URL recognition, no identifier minting, no aliases, no sub-catalogs.

Adapters, parsing and the CLI receive bindings, never dictionaries. `--config
DIRECTORY` selects another complete configuration directory; without it commands
read `config/` relative to this checkout. The installed retrieval library loads no
configuration.

When adding a source, reference an existing organisation or add its verified
identity, select an adapter and supply its endpoint and options in a service
binding, then list that binding from the catalog it belongs to. Do not derive
organisations from hostnames. Organisations will later be defined in and matched
against the `nordicintel/organisations` repository.

## Generated artifacts

```sh
uv run python scripts/generate/generate_configuration.py
uv run python scripts/generate/generate_configuration.py --check
```

These commands generate the [service inventory](inventory.md); CI checks for drift.
