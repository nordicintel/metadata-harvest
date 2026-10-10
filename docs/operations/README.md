# Repository operations

After `uv sync --locked --extra harvest`, run commands from the checkout:

| Command module | Role |
| --- | --- |
| `apps.operations.harvest` | Existing adapter run, collect, refresh and explicit publish commands |
| `apps.operations.catalog` | Offline candidate build and explicit production publication |
| `apps.operations.portal` | Snapshot import, refresh and explicit publication |
| `apps.operations.scheduling` | Resolve schedules and synchronise workflow blocks |
| `apps.operations.monitor` | Progress and GitHub check monitoring |
| `apps.operations.outcome` | Report failures for the selected scope |

Use `uv run python -m <module> --help`. Command options and source-selection aliases
remain unchanged; catalog additionally accepts `--config` independently of `--data`.
Schedule settings are in `config/schedules.toml`; run `uv run python -m
apps.operations.scheduling sync --check` to check generated blocks.

Operations does not own adapter retry or pacing policy. The scheduled workflows
keep their existing queues, continuation logic and production publication locks.
