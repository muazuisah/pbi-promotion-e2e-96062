import dataclasses

import pytest

from pbi_promote.governance import check_branch, check_pull_request

FIN = "src/finance-analytics/claims-summary/Claims Summary.Report/definition.pbir"
CSAM = "src/csam-analytics/member-engagement/Member Engagement.Report/definition.pbir"


@pytest.mark.parametrize(
    "branch",
    ["STRY0012345", "STRY0012345-claims-trend", "RITM0004567-fix-filter", "INC0098765-hotfix", "dependabot/pip/x"],
)
def test_valid_branch_names(config, branch):
    assert check_branch(branch, config).ok


@pytest.mark.parametrize(
    "branch",
    ["main", "feature/claims", "stry0012345", "STRY12345", "STRY00123456", "STRY0012345_Claims", "CHG0012345-x",
     "STRY0012345-Claims", "STRY0012345-"],
)
def test_invalid_branch_names(config, branch):
    findings = check_branch(branch, config)
    assert not findings.ok
    assert "naming rule" in findings.errors[0].message


@pytest.mark.parametrize("title", ["STRY0012345: Add trend page", "STRY0012345 - Add trend page", "STRY0012345 Add"])
def test_matching_title_passes(config, title):
    assert check_pull_request([FIN], "STRY0012345-trend", title, config).findings.ok


def test_title_without_work_item_fails(config):
    result = check_pull_request([FIN], "STRY0012345-trend", "Add trend page", config)
    assert any("must start with the work item ID" in f.message for f in result.findings.errors)


def test_title_with_different_work_item_fails(config):
    result = check_pull_request([FIN], "STRY0012345-trend", "RITM0000001: Add trend page", config)
    assert any("must match" in f.message for f in result.findings.errors)


def test_work_item_is_reported(config):
    assert check_pull_request([FIN], "INC0000042-hotfix", "INC0000042: fix", config).work_item == "INC0000042"


def test_cross_team_change_is_blocked(config):
    result = check_pull_request([FIN, CSAM], "STRY0012345", "STRY0012345: both", config)
    assert any("At most 1 team" in f.message for f in result.findings.errors)


def test_cross_team_allowed_when_limit_raised(config):
    gov = dataclasses.replace(config.governance, max_teams_per_pull_request=2)
    cfg = dataclasses.replace(config, governance=gov)
    assert check_pull_request([FIN, CSAM], "STRY0012345", "STRY0012345: both", cfg).findings.ok


def test_mixing_platform_and_team_changes_is_blocked(config):
    result = check_pull_request([FIN, ".github/workflows/deploy.yml"], "STRY0012345", "STRY0012345: x", config)
    assert any("mixes platform files" in f.message for f in result.findings.errors)


def test_platform_only_change_passes(config):
    result = check_pull_request(["config/parameter.yml", "docs/RUNBOOK.md"], "RITM0000001-param", "RITM0000001: p",
                                config)
    assert result.findings.ok
    assert not result.changes.teams


def test_unregistered_team_folder_is_blocked(config):
    result = check_pull_request(["src/hr-analytics/headcount/x.json"], "STRY0012345", "STRY0012345: hr", config)
    assert any("not a registered team" in f.message for f in result.findings.errors)


def test_exempt_branch_skips_naming_rules(config):
    result = check_pull_request([".github/workflows/deploy.yml"], "dependabot/github_actions/x", "Bump x", config)
    assert result.findings.ok and result.exempt


def test_exempt_branch_still_cannot_mix_scopes(config):
    result = check_pull_request([FIN, "scripts/x.py"], "dependabot/pip/x", "Bump x", config)
    assert not result.findings.ok


def test_empty_change_warns(config):
    result = check_pull_request([], "STRY0012345", "STRY0012345: nothing", config)
    assert result.findings.ok and result.findings.warnings
