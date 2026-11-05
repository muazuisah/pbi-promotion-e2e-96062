import shutil

import pytest

from pbi_promote.gitutil import GitError, changed_files
from pbi_promote.plan import build_plan

from conftest import git

CLAIMS_TMDL = "src/finance-analytics/claims-summary/Claims Summary.SemanticModel/definition/tables/Claims.tmdl"


def _touch(repo, rel, text="\n// changed\n"):
    path = repo / rel
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_single_unit_change_plans_only_that_unit(repo, config, commit, head):
    base = head(repo)
    _touch(repo, CLAIMS_TMDL)
    new = commit(repo)
    plan = build_plan(repo, config, base=base, head=new)
    assert plan.deploy == ["finance-analytics/claims-summary"]
    assert plan.teams == ["finance-analytics"]
    assert plan.findings.ok


def test_platform_only_change_plans_nothing(repo, config, commit, head):
    base = head(repo)
    _touch(repo, "README.md")
    plan = build_plan(repo, config, base=base, head=commit(repo))
    assert plan.deploy == [] and plan.findings.ok


def test_deleted_unit_is_reported_not_deployed(repo, config, commit, head):
    base = head(repo)
    shutil.rmtree(repo / "src/finance-analytics/claims-summary")
    plan = build_plan(repo, config, base=base, head=commit(repo))
    assert plan.deploy == []
    assert plan.removed == ["finance-analytics/claims-summary"]
    assert any("never deleted automatically" in w.message for w in plan.findings.warnings)


def test_renamed_unit_deploys_new_and_reports_old(repo, config, commit, head):
    base = head(repo)
    git(repo, "mv", "src/finance-analytics/claims-summary", "src/finance-analytics/claims-overview")
    plan = build_plan(repo, config, base=base, head=commit(repo))
    assert plan.deploy == ["finance-analytics/claims-overview"]
    assert plan.removed == ["finance-analytics/claims-summary"]


def test_three_dot_ignores_changes_made_on_main(repo, config, commit, head):
    """Feature branch diffs must not pick up other teams' work merged to main meanwhile."""
    git(repo, "checkout", "-q", "-b", "STRY0000001-claims")
    _touch(repo, CLAIMS_TMDL)
    branch_head = commit(repo, "branch work")
    git(repo, "checkout", "-q", "main")
    _touch(repo, "src/csam-analytics/member-engagement/Member Engagement.SemanticModel/definition/model.tmdl")
    commit(repo, "other team on main")
    three = build_plan(repo, config, base="main", head=branch_head, diff_mode="three-dot")
    two = build_plan(repo, config, base="main", head=branch_head, diff_mode="two-dot")
    assert three.deploy == ["finance-analytics/claims-summary"]
    assert "csam-analytics/member-engagement" in two.deploy


def test_selector_overrides_diff(repo, config):
    plan = build_plan(repo, config, selector="finance-analytics/*")
    assert plan.deploy == ["finance-analytics/claims-summary", "finance-analytics/executive-kpis"]
    assert plan.source.startswith("manual selection")


def test_invalid_unit_blocks_plan(repo, config, commit, head):
    base = head(repo)
    (repo / "src/finance-analytics/claims-summary/Claims Summary.Report/definition.pbir").write_text("{", "utf-8")
    plan = build_plan(repo, config, base=base, head=commit(repo))
    assert not plan.findings.ok


def test_placeholder_workspace_blocks_live_plan(repo, config):
    plan = build_plan(repo, config, selector="csam-analytics/*", environments=["test", "prod"])
    assert sum("placeholder" in e.message for e in plan.findings.errors) == 2
    dry = build_plan(repo, config, selector="csam-analytics/*", environments=["test"], allow_placeholder=True)
    assert dry.findings.ok


def test_team_limit_enforced_on_request(repo, config, commit, head):
    base = head(repo)
    _touch(repo, CLAIMS_TMDL)
    _touch(repo, "src/csam-analytics/member-engagement/Member Engagement.SemanticModel/definition/model.tmdl")
    new = commit(repo)
    assert build_plan(repo, config, base=base, head=new).findings.ok
    assert not build_plan(repo, config, base=base, head=new, enforce_team_limit=True).findings.ok


def test_paths_with_spaces_and_unicode(repo, config, commit, head):
    base = head(repo)
    target = repo / "src/finance-analytics/claims-summary/Claims Summary.SemanticModel/definition/tables/Ünits Ä.tmdl"
    target.write_text("table x\n", encoding="utf-8")
    new = commit(repo)
    files = changed_files(repo, base, new)
    assert files == [target.relative_to(repo).as_posix()]


@pytest.mark.parametrize("bad", ["0" * 40, "--output=x", "a b", ""])
def test_bad_refs_rejected(repo, bad):
    with pytest.raises(GitError):
        changed_files(repo, bad, "HEAD")


def test_requires_base_and_head_without_selector(repo, config):
    with pytest.raises(ValueError):
        build_plan(repo, config)
