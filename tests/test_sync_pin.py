import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_presubmit_checks_out_the_sync_commit_qq_pins():
    # The parity job copies its onboarding manifests from this checkout; a stale ref would test
    # against another qqsync than the one qq installs.
    pinned = re.findall(r"quirq-ai/sync@([0-9a-f]{40})", (ROOT / "pyproject.toml").read_text())
    workflow = (ROOT / ".github" / "workflows" / "presubmit.yml").read_text()
    checked_out = re.findall(r"repository: quirq-ai/sync\s+ref: ([0-9a-f]{40})", workflow)
    assert len(pinned) == 1 and checked_out == pinned
