import json
import os
import subprocess
import sys

import pytest

from pbi_promote.cli import main

from conftest import TEMPLATE_ROOT, git

CLAIMS_TMDL = "src/finance-analytics/claims-summary/Claims Summary.SemanticModel/definition/tables/Claims.tmdl"
CSAM_TMDL = "src/csam-analytics/member-engagement/Member Engagement.SemanticModel/definition/model.tmdl"


def _outputs(path):
    """Parse a GITHUB_OUTPUT file written with heredoc delimiters."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, i = {}, 0
    while i < len(lines):
        name, delim = lines[i].split("<<", 1)
        end = lines.index(delim, i + 1)
        out[name] = "\n".join(lines[i + 1:end])
        i = end + 1
    return out


@pytest.fixture
def actions_env(tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    summary = tmp_path / "github_summary"
    out.touch()
    summary.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    return out, summary


def _change(repo, rel, commit):
    with (repo / rel).open("a", encoding="utf-8") as fh:
        fh.write("\n// change\n")
    return commit(repo)


def _branch(repo, name):
    git(repo, "checkout", "-q", "-b", name)


def test_check_branch_exit_codes(repo, capsys):
    cfg = str(repo / "config")
    assert main(["check-branch", "--config-root", cfg, "--branch", "STRY0012345-claims-fix"]) == 0
    assert main(["check-branch", "--config-root", cfg, "--branch", "feature/claims"]) == 1
    assert "feature/claims" in capsys.readouterr().out


def test_validate_pr_good(repo, commit, actions_env, capsys):
    base = git(repo, "rev-parse", "HEAD")
    _branch(repo, "STRY0012345-claims")
    head = _change(repo, CLAIMS_TMDL, commit)
    rc = main([
        "validate-pr", "--repo-root", str(repo), "--config-root", str(repo / "config"),
        "--base", base, "--head", head, "--branch", "STRY0012345-claims",
        "--title", "STRY0012345: tidy claims measures", "--codeowners", str(repo / ".github/CODEOWNERS"),
    ])
    assert rc == 0, capsys.readouterr().out
    out = _outputs(actions_env[0])
    assert json.loads(out["teams"]) == ["finance-analytics"]
    assert json.loads(out["units"]) == ["finance-analytics/claims-summary"]
    assert "Work item: **STRY0012345**" in actions_env[1].read_text(encoding="utf-8")


def test_validate_pr_rejects_cross_team_change(repo, commit, actions_env, capsys):
    base = git(repo, "rev-parse", "HEAD")
    _branch(repo, "RITM0000001-both")
    _change(repo, CLAIMS_TMDL, commit)
    head = _change(repo, CSAM_TMDL, commit)
    rc = main([
        "validate-pr", "--repo-root", str(repo), "--config-root", str(repo / "config"),
        "--base", base, "--head", head, "--branch", "RITM0000001-both", "--title", "RITM0000001 both teams",
    ])
    assert rc == 1
    assert "::error" in capsys.readouterr().out


def test_validate_pr_rejects_bad_title(repo, commit, actions_env):
    base = git(repo, "rev-parse", "HEAD")
    _branch(repo, "INC0000002-hotfix")
    head = _change(repo, CLAIMS_TMDL, commit)
    rc = main([
        "validate-pr", "--repo-root", str(repo), "--config-root", str(repo / "config"),
        "--base", base, "--head", head, "--branch", "INC0000002-hotfix", "--title", "quick fix",
    ])
    assert rc == 1


def test_validate_pr_with_deleted_unit(repo, commit, actions_env):
    import shutil

    base = git(repo, "rev-parse", "HEAD")
    _branch(repo, "STRY0000003-retire")
    shutil.rmtree(repo / "src/finance-analytics/claims-summary")
    head = commit(repo)
    rc = main([
        "validate-pr", "--repo-root", str(repo), "--config-root", str(repo / "config"),
        "--base", base, "--head", head, "--branch", "STRY0000003-retire", "--title", "STRY0000003 retire claims",
    ])
    assert rc == 0
    assert json.loads(_outputs(actions_env[0])["units"]) == []


def test_plan_outputs(repo, commit, actions_env):
    base = git(repo, "rev-parse", "HEAD")
    head = _change(repo, CLAIMS_TMDL, commit)
    rc = main([
        "plan", "--repo-root", str(repo), "--config-root", str(repo / "config"),
        "--base", base, "--head", head, "--env", "test", "--env", "prod", "--allow-placeholder",
    ])
    assert rc == 0
    out = _outputs(actions_env[0])
    assert json.loads(out["units"]) == ["finance-analytics/claims-summary"]
    assert out["has_changes"] == "true"


def test_plan_no_changes(repo, commit, actions_env):
    base = git(repo, "rev-parse", "HEAD")
    head = _change(repo, "README.md", commit)
    assert main(["plan", "--repo-root", str(repo), "--config-root", str(repo / "config"),
                 "--base", base, "--head", head]) == 0
    assert _outputs(actions_env[0])["has_changes"] == "false"


def test_plan_placeholder_blocks_live(repo, actions_env):
    rc = main(["plan", "--repo-root", str(repo), "--config-root", str(repo / "config"),
               "--units", "finance-analytics/*", "--env", "prod"])
    assert rc == 1
    out = _outputs(actions_env[0])
    assert out["has_changes"] == "false" and json.loads(out["units"]) == []


@pytest.mark.parametrize("units_json", ["not json", '"finance-analytics/x"', '["../etc/passwd"]', '["a/b; rm -rf /"]'])
def test_deploy_rejects_bad_units(repo, units_json, capsys):
    rc = main(["deploy", "--repo-root", str(repo), "--config-root", str(repo / "config"),
               "--env", "test", "--units-json", units_json])
    assert rc == 2


def test_deploy_nothing(repo):
    assert main(["deploy", "--repo-root", str(repo), "--config-root", str(repo / "config"),
                 "--env", "test", "--units-json", "[]"]) == 0


def test_deploy_dry_run_writes_report(repo, tmp_path, actions_env):
    report = tmp_path / "out" / "report.json"
    rc = main(["deploy", "--repo-root", str(repo), "--config-root", str(repo / "config"), "--env", "prod",
               "--units-json", '["finance-analytics/executive-kpis"]', "--dry-run", "--commit", "abc",
               "--report", str(report)])
    assert rc == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["dryRun"] is True and data["results"][0]["status"] == "dry-run"
    assert "Deployment to `prod` (dry run)" in actions_env[1].read_text(encoding="utf-8")


def test_unknown_ref_is_exit_2(repo):
    assert main(["plan", "--repo-root", str(repo), "--config-root", str(repo / "config"),
                 "--base", "does-not-exist", "--head", "HEAD"]) == 2


def test_validate_repo_on_template(capsys):
    rc = main(["validate-repo", "--repo-root", str(TEMPLATE_ROOT), "--config-root", str(TEMPLATE_ROOT / "config")])
    assert rc == 0, capsys.readouterr().out


def test_module_entry_point():
    env = {**os.environ, "PYTHONPATH": str(TEMPLATE_ROOT / "scripts")}
    env.pop("GITHUB_ACTIONS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pbi_promote", "check-branch", "--config-root", str(TEMPLATE_ROOT / "config"),
         "--branch", "main"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 1
    assert "STRY" in result.stdout + result.stderr
