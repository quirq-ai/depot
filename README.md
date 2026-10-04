# depot

`depot` is `qq`, the quirq infra (`qq`) command line. Every repo that quirq infra builds pins the
`qq` version it runs in its manifest, `infra/repo.toml`, and `qq` installs and runs exactly that
version.

## Chromium counterpart

`depot_tools`: the one checkout on your `PATH` that bootstraps itself and wraps the tools a
contributor needs (`gclient`, `git cl upload`, `git cl try`, `git cl land`).

## Shape

- Thin. `qq` wraps git and `gh` (behind a `github` backend; `launchpad`, quirq's own cloud, later)
  and calls other qq repos' libraries by pinned commit: manifests through `sync`, toolchains
  through `toolchains`, builds through `recipes`.
- Pluggable. Other repos add subcommands; `release` adds `qq channel rollback`.
- Acknowledge, then push. Commands that start long work return a run ID at once and the verdict
  arrives later, so no agent holds a tool call open during a build.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-DEP-01 | `qq` skeleton with version pinning | | not started |
| V0-DEP-02 | `qq fetch` and `qq sync` | | waiting on V0-SYN-02, V0-TCH-01, V0-TCH-02 |
| V0-DEP-03 | `qq build` and `qq test` | | waiting on V0-REC-01 |
| V0-DEP-04 | `qq upload`, `try`, `land`, `status` | | waiting on V0-GAT-01 |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.
