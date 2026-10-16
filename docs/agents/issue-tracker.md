# Issue tracker: docs/backlog.md

Issues for this repo are lines in [`docs/backlog.md`](../backlog.md). There is no
other tracker: do not create GitHub issues, `.scratch/` files, or ticket files.

## Conventions

The rules in the file's own "How to use this file" section are authoritative. In short:

- **One line per item**: `- [ ] **<area>** Short imperative title — why it matters (link).`
  The area tag matches `apps/` or the adapter name (`**kolada**`, `**catalog**`, `**ci**`).
- **Sections are priority**: `## Now` (max five, in progress or next up), `## Next`
  (agreed and ready), `## Later` (wanted, unscheduled), `## Ideas` (unvetted).
- **Detail lives elsewhere**: link the doc, run, file, or spec that holds it. A spec
  that needs more than one line goes in the owning guide under `docs/` and is linked.
- **Done means deleted**: remove the line in the commit that completes it.
  **Dropped means deleted**: say why in the commit message. No "Done" section.
- **Ordering is intentional**: move items between sections; never reorder within one.

## When a skill says "publish to the issue tracker"

Append one line to the matching section of `docs/backlog.md`, usually `## Ideas` for
new or unvetted work and `## Next` only when the user has agreed it is ready.

## When a skill says "fetch the relevant ticket"

Read `docs/backlog.md` and find the line by its area tag and title, then follow its
link for detail. The user normally names the item directly.

## When a skill says "comment on" or "close" an issue

There are no comments: amend the line or its linked doc. Closing is deleting the line
in the commit that finishes or drops the work.

## Triage state

The section is the state; see `triage-labels.md` for how the triage roles map to
sections.

## Wayfinding operations

Used by `/wayfinder`. The backlog holds only the one-line pointer; the map and its
tickets live as files.

- **Map**: `docs/wayfinding/<effort>/map.md` (Notes / Decisions-so-far / Fog), linked
  from one backlog line.
- **Child ticket**: `docs/wayfinding/<effort>/issues/NN-<slug>.md`, numbered from `01`,
  with `Type:` (`research`/`prototype`/`grilling`/`task`) and `Status:`
  (`claimed`/`resolved`) lines near the top.
- **Blocking**: a `Blocked by: NN, NN` line; unblocked when every listed file is `resolved`.
- **Frontier**: scan the `issues/` directory for open, unblocked, unclaimed files; lowest number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under `## Answer`, set `Status: resolved`, then add a
  context pointer to the map's Decisions-so-far.
