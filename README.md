# depot

`depot` is `qq`, the quirq infra command line. Every repo that quirq infra builds pins the
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

## Install

Clone this repo once and put its `bin` directory on `PATH`, as with depot_tools. You need
`git` and `python3` 3.11 or newer with its `venv` module.

```sh
git clone https://github.com/quirq-ai/depot ~/depot
export PATH="$HOME/depot/bin:$PATH"
qq --version
```

## Version pinning

Every repo pins the `qq` it runs in its manifest, read only through `sync`:

```toml
[qq]
version = "0.1.0"          # installs depot tag v0.1.0
# source = "https://github.com/quirq-ai/depot"   # optional: pin the bytes too,
# digest = "git:<commit>"                         # by commit, or sha256:<archive digest>
```

Inside such a repo, `qq` runs exactly the pinned version. The first run on a machine installs it
into its own environment under `$QQ_HOME` (default `~/.cache/qq/versions/<version>`) and checks
that it reports the pinned version; later runs reuse it. When a roll moves the pin, the next `qq`
installs the new version by itself: that is how `qq` updates. Outside a repo, or in one without a
`[qq]` table, `qq` runs the version of your depot checkout.

| Variable | Meaning |
|---|---|
| `QQ_HOME` | Where launcher and pinned versions live |
| `QQ_PYTHON` | The interpreter the bootstrap uses (default `python3`) |
| `QQ_DEPOT_URL` | Where version tags are fetched from (default this repo; a mirror works) |
| `QQ_PINNED=1` | Run this `qq` as is, without reading the pin |

TODO(expert): once depot publishes releases (release repo), pin by `sha256` archive by default and
let the bootstrap update its own checkout.

## Subcommands from other repos

A package adds a `qq` subcommand with an entry point in the `qq.commands` group naming a function
`register(subparsers)`. It adds one parser and sets `run`, a function of the parsed arguments that
returns the exit code.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Python 3.14 in CI (the org pin in infra-config); `qq` itself needs 3.11 or newer.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-DEP-01 | `qq` skeleton with version pinning | #2 | in review |
| V0-DEP-02 | `qq fetch` and `qq sync` | | waiting on V0-SYN-02, V0-TCH-01, V0-TCH-02 |
| V0-DEP-03 | `qq build` and `qq test` | | waiting on V0-REC-01 |
| V0-DEP-04 | `qq upload`, `try`, `land`, `status` | | waiting on V0-GAT-01 |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.
