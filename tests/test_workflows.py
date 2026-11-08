"""The GitHub workflow files must be valid YAML: GitHub rejects an invalid one
before any job runs, so CI can't report the mistake itself."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parent.parent / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses_and_installs_from_a_hashed_lock(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    installs = [s["run"] for s in steps if "pip install" in str(s.get("run", ""))]
    assert installs and all("--require-hashes" in run for run in installs)
    assert all("@" in s["uses"] and len(s["uses"].split("@")[1].split()[0]) == 40
               for s in steps if "uses" in s), "actions must be pinned to a full commit SHA"
