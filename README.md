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
`git` and `python3` 3.11.4 or newer with its `venv` module.

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

Trust: a pin installs qq only from the depot (`$QQ_DEPOT_URL`, default quirq-ai/depot, and
release archives under it) or from a source you list in `$QQ_TRUSTED_SOURCES`, so a pull request
that edits the manifest cannot make `qq status` run code from a host it picked. A `source` needs a
digest. A `git:` digest fixes the exact commit, and qq checks pip installed that commit; a
`sha256:` digest fixes the archive; a version alone trusts the depot tag `v<version>`.

Every qq environment (the launcher, each pinned version, the gate) installs PyPI packages only
by hash from `src/qqdepot/locks/pypi.txt`, and everything else (qqsync, qqrecipes, the gate) by
pinned commit with no index, so nothing unpinned runs next to your `gh` token. A pinned repo
that adds a PyPI dependency needs it added to `locks/pypi.in` and the lock regenerated (the
command is in that file); otherwise the install fails at the no-index step or `pip check`.
Locked packages install from wheels only, since building one would fetch unhashed build
requirements. pip still reads your own `PIP_*` variables and pip config (proxy, CA,
`find-links`): those come from you, not from a repo, but keep them pointed at sources you trust.

| Variable | Meaning |
|---|---|
| `QQ_HOME` | Where launchers and pinned versions live |
| `QQ_PYTHON` | The interpreter the bootstrap uses (default `python3`) |
| `QQ_TRUSTED_SOURCES` | Space-separated sources a `[qq]` pin may install from, besides the depot |
| `QQ_DEPOT_URL` | Where version tags are fetched from (default this repo; a mirror works) |
| `QQ_PINNED=1` | Run this `qq` as is, without reading the pin (applies to that one process) |

TODO(expert): once depot publishes releases (release repo), pin by `sha256` archive by default and
let the bootstrap update its own checkout.

## Get a repo's toolchains and dependencies

```sh
qq fetch https://github.com/quirq-ai/xo-space   # clone, then qq sync in it
qq sync                                        # inside a repo: fetch every pin, check, link
```

`qq sync` reads the manifest's `[toolchains]` and `[deps]` through `sync` and fetches each pin
once per machine into `$QQ_HOME/store`, checked against its digest. A tarball is unpacked. The
entry is then read only, and each `qq sync` checks it is unchanged before linking it again; a
changed entry is refused (remove it with `chmod -R u+w ENTRY && rm -rf ENTRY` and sync again). Git
fetches run over https, ssh and file only. The
pin is then linked at `<repo>/.qq/toolchains/<name>` or `<repo>/.qq/deps/<name>`. The source's
scheme picks the fetcher:

| Source | Digest | Fetched as |
|---|---|---|
| `https://…`, `file://…` | `sha256:` | the bytes at that URL |
| `oci://REGISTRY/REPO@sha256:<manifest>` | `sha256:<layer>` | that layer of that manifest, which is how `toolchains` publishes |
| any git URL | `git:<commit>` | the tree at that commit |

A pin with `platforms` uses this machine's entry (`linux-x86_64`, `macos-arm64`, …). A pin
dropped from the manifest loses its link at the next sync. Registry packages must be public:
`qq sync` asks for an anonymous token and never sends credentials.

## Build and test like CI

```sh
qq build [TARGET ...]     # fetch and build the targets (default all)
qq test [TARGET ...]      # fetch, build and test them
```

Both hand the manifest to `recipes`, qq's planner and runner, which leave JUnit XML, logs and
`results.json` under `<repo>/.qq/out`. Product CI does not run recipes yet: it still runs the
stand-in commands of its hand-written presubmit until infra-config generates the workflow
(V0-CFG-02), so a local run and a CI run of one commit can differ. Toolchains come from
`<repo>/.qq/toolchains/<name>`, where `qq sync` unpacks them, or `--toolchain NAME=ROOT`; any other
toolchain is the one on `PATH`.

## Send a change through the gate

```sh
qq upload [--title T] [--draft]   # push this branch, open or update its change
qq try [CHANGE] [--notify CMD]    # upload, print a run ID and exit; the verdict is pushed later
qq land [CHANGE] [--notify CMD]   # land once the gate passes (merge queue); returns at once
qq status [CHANGE]                # the gate's verdict now: exit 0 pass, 1 refused, 3 pending
```

Acknowledge, then push: `qq try` and `qq land` return a run ID at once, so an agent never holds a
tool call open while CI runs. A watcher follows the change and delivers the verdict (pass,
refused, landed, dequeued by the merge queue, superseded by a newer push, closed, timed-out or
error) to `$QQ_HOME/verdicts/<run ID>.json` and to the `--notify` command (default `$QQ_NOTIFY`),
which gets the verdict JSON on stdin and `QQ_VERDICT` in its environment. An agent harness plugs
its own channel in there. The command line shows in `ps` and in the receipt, so pass secrets
through the environment. If the watcher dies (a reboot), `qq status` still gives the verdict.

`qq land` queues nothing until the gate passes: without a merge queue, GitHub would merge at
once. A refused change is not queued, and a change the queue drops is reported as dequeued.

qq does not decide what must pass. It runs [gate](https://github.com/quirq-ai/gate)'s `qqgate`
at the commit `src/qqdepot/gate.py` pins, in its own environment under `$QQ_HOME/gate`, with
infra-config at the commit the gate pins. A repo infra-config does not list (qqgate's exit 3) is
reported as ungated and judged strictly from the backend's own checks: at least one check, every
check a success (skipped and neutral are refusals), and no run of the commit still going; `qq land` then squash-merges unless `--method` says otherwise. On GitHub the commands are thin wrappers over `gh`
(signed in with `gh auth login`); backend code sits in `src/qqdepot/backends/<backend>.py`,
picked by `--backend` or `$QQ_BACKEND`.

## Subcommands from other repos

A package adds a `qq` subcommand with an entry point in the `qq.commands` group naming a function
`register(subparsers)`. It adds one parser and sets `run`, a function of the parsed arguments that
returns the exit code.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Python 3.14 in CI (the org pin in infra-config); `qq` itself needs 3.11.4 or newer.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-DEP-01 | `qq` skeleton with version pinning | #2 | merged; done-when shown by `tests/test_fresh_machine.py` in presubmit |
| V0-DEP-02 | `qq fetch` and `qq sync` | #4 | merged; a fresh clone builds after `qq sync` alone in `tests/test_sync.py`. The `e2e-sync` workflow (#17) runs `qq fetch` (clone at main, then `qq sync`) on xo-space and innernet nightly and on demand, from public ghcr with no credentials |
| V0-DEP-03 | `qq build` and `qq test` | #3, #5 | merged; presubmit `parity` runs `qq test` and `qqrecipes execute` on xo-space and innernet on separate runners and compares their JUnit. Manifests are sync's onboarding fixtures until onboarding lands the real ones |
| V0-DEP-04 | `qq upload`, `try`, `land`, `status` | #7 | merged; `tests/test_change.py` shows try returning a run ID and the verdict pushed later (fake gh); presubmit `live` runs the pinned gate and `qq status` through the real `gh`. Live verdicts on xo-space and innernet wait on the delivered workflows (xo-space #211, innernet #37) and the rulesets (V0-ORG-03) |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.

## License

Apache-2.0; see [LICENSE](LICENSE).
