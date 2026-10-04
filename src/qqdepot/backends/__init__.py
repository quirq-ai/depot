"""Backend-specific code for qq upload, try, land and status: one module per backend.

`load(name)` imports `qqdepot.backends.<name>`; nothing registers backends, so `launchpad`
(quirq's own cloud, v2) is one new module. Each backend module provides:

  repo(given)                    "owner/name" of the repo, from `given` or the checkout
  default_branch(repo)           the branch changes land on
  current_branch(root)           the checked-out branch
  push(root, base)               push the current branch; returns its name
  find(repo, branch)             the open change for a branch, or None
  create(repo, branch, base, title, body, draft)
  view(repo, ref)                one change (number, URL or branch) as a Change
  runs(repo, sha)                the backend's run IDs for one commit, with their status
  checks(repo, sha)              check name -> conclusion (or status while running)
  enqueue(repo, number, sha, method)   land it (merge queue, or merge); returns at once
  queued(repo, number)           True while it waits to land
  token()                        a credential the gate can read check results with, or None
"""
from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass
from types import ModuleType


class ChangeError(Exception):
    """A backend call failed; the message says what to fix."""


@dataclass(frozen=True)
class Change:
    repo: str          # owner/name
    number: int
    url: str
    state: str         # open, merged or closed
    head: str          # head commit
    base: str          # target branch
    branch: str        # source branch

    def to_json(self) -> dict:
        return asdict(self)


def load(name: str) -> ModuleType:
    if not name.isidentifier():
        raise ChangeError(f"bad backend name {name!r}")
    try:
        return importlib.import_module(f"qqdepot.backends.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"qqdepot.backends.{name}":
            raise ChangeError(f"no qq backend {name!r} (qqdepot/backends/{name}.py)") from None
        raise
