# Agent guide

Read `README.md` first.

- Every change is a pull request against `main`, titled with its item id (for example `V0-DEP-02`).
  It lands only with the `presubmit` check green.
- This is a core repo: no file names a language or a build tool. Keep `qq` thin: it wraps git and
  `gh` (behind the `github` backend) and calls other qq repos' libraries, pinned by commit.
- Manifests (`infra/repo.toml`) are read only through `qqsync`. Never parse one here.
- Commands that start long work return a run ID at once; the verdict is pushed later. No command
  holds a caller open for a build.
- `.github/CODEOWNERS` names suraj (`@sharmasuraj0123`) as owner of the policy and trust paths;
  owner names are his call, so never change them. Leave any other `owners` list empty.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- Never commit secrets, tokens or internal hostnames. This repo is public.
