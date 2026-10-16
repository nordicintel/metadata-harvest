# Documentation

## Maintained guides

| Task | Guide |
| --- | --- |
| Understand module ownership and dependencies | [Architecture](architecture.md) |
| Retrieve observations from catalog distributions | [Retrieval](retrieval/README.md) |
| Configure organisations, catalogs and concrete services | [Configuration](configuration/README.md) |
| Understand published metadata and field mappings | [StatDCAT contract](model/output-contract.md) |
| Run, resume and monitor metadata collection | [Harvesting](harvest/README.md) |
| Add or change a metadata adapter | [Adapter implementation](harvest/adapter-guide.md) |
| Track open bugs, features and investigations | [Backlog](backlog.md) |

Adapter-specific scope, source quirks and limitations are linked from the harvesting
guide. The [service inventory](configuration/inventory.md) is generated from `config/`.

## Maintaining this baseline

Keep each rule in its owning guide and link to it elsewhere. Document behavior and
operational decisions; derive inventories, defaults and schedules from configuration.
Update guides with the implementation. Open work lives only in the [backlog](backlog.md);
use Git history for completed plans and delivery narratives instead of adding
another status document.

Generated references are changed through their generators. Supplied standards,
discovery evidence and migration ledgers retain their original bytes. Historical
receipts describe their recorded run, not current live coverage. Local ignored
`docs/api/` research is outside this maintained baseline.
