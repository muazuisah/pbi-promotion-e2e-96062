"""Static checks on the GitHub Actions workflows (security conventions and wiring to the CLI)."""

from __future__ import annotations

import re

import pytest

yaml = pytest.importorskip("yaml")

from conftest import TEMPLATE_ROOT  # noqa: E402

WORKFLOW_DIR = TEMPLATE_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
UNSAFE_IN_RUN = re.compile(r"\$\{\{\s*(github\.event\.|inputs\.|github\.head_ref)")


def _load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML (YAML 1.1) reads the key "on" as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _steps(wf):
    for job_id, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, step


def test_expected_workflows_exist():
    names = {p.name for p in WORKFLOWS}
    assert {"pr-validation.yml", "platform-ci.yml", "deploy.yml", "deploy-dev.yml", "_deploy.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_conventions(path):
    wf = _load(path)
    assert "permissions" in wf, "set least-privilege permissions at the top level"
    assert wf["permissions"].get("contents") == "read"
    assert wf["permissions"].get("id-token") != "write" or path.name == "_deploy.yml"

    for job_id, step in _steps(wf):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert PINNED.match(uses), f"{path.name}:{job_id} action not pinned to a full SHA: {uses}"
        if uses and uses.startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, f"{path.name}:{job_id} checkout keeps credentials"
        run = step.get("run", "")
        assert not UNSAFE_IN_RUN.search(run), f"{path.name}:{job_id} interpolates untrusted input in run: {run}"

    for job_id, job in wf["jobs"].items():
        if "steps" in job:
            assert "timeout-minutes" in job, f"{path.name}:{job_id} has no timeout"


def test_pr_validation_uses_trusted_validator():
    wf = _load(WORKFLOW_DIR / "pr-validation.yml")
    job = wf["jobs"]["validate"]
    assert job["name"] == "Validate pull request"
    checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkouts[0]["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkouts[0]["with"]["path"] == "trusted"
    validate = next(s for s in job["steps"] if "validate-pr" in s.get("run", ""))
    assert validate["env"]["PYTHONPATH"] == "trusted/scripts"
    assert "--config-root trusted/config" in validate["run"]
    assert "--codeowners trusted/.github/CODEOWNERS" in validate["run"]
    assert set(wf["on"]["pull_request"]["types"]) >= {"opened", "edited", "synchronize", "reopened"}


def test_deploy_pipeline_order_and_protection():
    wf = _load(WORKFLOW_DIR / "deploy.yml")
    jobs = wf["jobs"]
    assert jobs["test"]["needs"] == "plan"
    assert jobs["prod"]["needs"] == ["plan", "test"]
    assert jobs["test"]["with"]["environment"] == "test"
    assert jobs["prod"]["with"]["environment"] == "prod"
    for job in ("test", "prod"):
        assert jobs[job]["uses"] == "./.github/workflows/_deploy.yml"
        assert jobs[job]["permissions"]["id-token"] == "write"
        assert jobs[job]["with"]["sha"] == "${{ github.sha }}"
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["on"]["push"]["branches"] == ["main"]
    assert wf["on"]["workflow_dispatch"]["inputs"]["units"]["required"] is True
    plan_run = "\n".join(s.get("run", "") for s in jobs["plan"]["steps"])
    assert "--diff-mode two-dot" in plan_run
    assert "refs/heads/main" in str(jobs["plan"]["steps"][0]["if"])


def test_reusable_deploy_runs_in_environment():
    wf = _load(WORKFLOW_DIR / "_deploy.yml")
    job = wf["jobs"]["deploy"]
    assert job["environment"] == "${{ inputs.environment }}"
    assert job["concurrency"]["cancel-in-progress"] is False
    assert set(wf["on"]["workflow_call"]["inputs"]) == {"environment", "units", "sha", "dry_run"}
    deploy = next(s for s in job["steps"] if "pbi_promote" in s.get("run", ""))
    assert deploy["env"]["AZURE_CLIENT_ID"] == "${{ vars.AZURE_CLIENT_ID }}"
    assert "--units-json" in deploy["run"] and "--dry-run" in deploy["run"]
    upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"


def test_dev_deploy_is_opt_in():
    wf = _load(WORKFLOW_DIR / "deploy-dev.yml")
    assert wf["jobs"]["plan"]["if"] == "vars.DEV_AUTO_DEPLOY == 'true'"
    assert wf["on"]["push"]["branches-ignore"] == ["main"]
    plan_run = "\n".join(s.get("run", "") for s in wf["jobs"]["plan"]["steps"])
    assert "check-branch" in plan_run and "--enforce-team-limit" in plan_run


def test_cli_subcommands_used_by_workflows_exist():
    from pbi_promote.cli import build_parser

    parser = build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001
    text = "\n".join(p.read_text(encoding="utf-8") for p in WORKFLOWS)
    used = set(re.findall(r"pbi_promote (?:\"\$\{args\[@\]\}\"|([a-z-]+))", text)) - {""}
    used |= set(re.findall(r"args=\(([a-z-]+)", text))
    assert used, "no CLI calls found"
    assert used <= commands, f"unknown subcommands: {used - commands}"
