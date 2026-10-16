# Documentation

## Maintained guides

| Task | Guide |
| --- | --- |
| Understand module ownership and dependencies | [Architecture](architecture.md) |
| Retrieve observations from worker documents | [Retrieval](retrieval/README.md) |
| Configure organisations, services and catalogs | [Configuration](configuration/README.md) |
| Run, collect and monitor harvests | [Harvesting](harvest/README.md) |
| Build accepted collections from source files | [Parsing](parsing/README.md) |
| Where the repository is going and what the sweep removed | [Clean sweep](clean-sweep.md) |
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

Generated references are changed through their generators. Local ignored
`docs/api/` research is outside this maintained baseline.
