# Branching

Default branch: **`master`**.

## Naming

```
<type>/<short-description>
```

Types match the Conventional Commit types, so a branch's name predicts its
commits:

| Prefix      | For                                        |
|-------------|--------------------------------------------|
| `feat/`     | a new capability                           |
| `fix/`      | a bug fix                                  |
| `refactor/` | behaviour-preserving restructure           |
| `perf/`     | a performance improvement                  |
| `test/`     | tests only                                 |
| `docs/`     | documentation only                         |
| `build/`    | build system or dependencies               |
| `ci/`       | CI configuration                           |
| `chore/`    | tooling, housekeeping                      |

Use lowercase and hyphens. Keep it short enough to type.

```
feat/underweight-ranking
fix/ladder-reprices-filled-order
refactor/extract-kite-client
```

## Rules

- Branch from `master` unless there is a stated reason not to.
- One logical change per branch. If a branch grows a second purpose, split it.
- Rebase onto `master` before merging; keep history linear.
- Delete the branch after merge.

## A note on trading code

Anything touching order placement, the placement ladder or the cash ledger
moves real money in a live broker account. Such branches are verified in paper
mode — orders recorded in full, never sent, filling against the live tick
feed — before they reach `master`. A bug here is not a failed test, it is an
unintended trade.

There is no dry-run mode and no simulator to verify against. Paper is the
ordinary operating mode of any environment that is not placing real orders,
and live is refused unless the registered static IP matches the address the
system is calling from.
