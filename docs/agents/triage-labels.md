# Triage Labels

The skills speak in terms of five canonical triage roles. This repo has no labels:
the section an item sits in inside [`docs/backlog.md`](../backlog.md) is its state.

| Role in mattpocock/skills | In our backlog                                   | Meaning                                  |
| ------------------------- | ------------------------------------------------ | ---------------------------------------- |
| `needs-triage`            | line under `## Ideas`                            | Maintainer needs to evaluate this item   |
| `needs-info`              | line under `## Ideas` ending in `(needs: …)`     | Waiting on the user for more information |
| `ready-for-agent`         | line under `## Next` or `## Now`                 | Fully specified, ready for an AFK agent  |
| `ready-for-human`         | line under `## Next` or `## Now` tagged `(human)` | Requires human implementation            |
| `wontfix`                 | delete the line, explain in the commit message   | Will not be actioned                     |

When a skill says "apply the X label", move or annotate the line accordingly. Only the
user moves items into `## Now`.
