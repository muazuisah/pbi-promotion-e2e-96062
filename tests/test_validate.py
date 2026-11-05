import json
import shutil

import pytest

from pbi_promote.validate import validate_repository

CLAIMS = "src/finance-analytics/claims-summary"
CLAIMS_REPORT = f"{CLAIMS}/Claims Summary.Report"
CLAIMS_MODEL = f"{CLAIMS}/Claims Summary.SemanticModel"


def _messages(findings, level="error"):
    return [f.message for f in findings.items if f.level == level]


def _edit_json(path, mutate):
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_template_repository_is_valid(repo, config):
    findings, items = validate_repository(repo, config, codeowners=repo / ".github/CODEOWNERS")
    assert findings.ok, [f.format() for f in findings.items]
    assert len(items) == 7


def test_missing_platform_file(repo, config):
    (repo / CLAIMS_REPORT / ".platform").unlink()
    findings, _ = validate_repository(repo, config)
    assert any("no .platform file" in m for m in _messages(findings))


def test_platform_type_must_match_folder(repo, config):
    _edit_json(repo / CLAIMS_REPORT / ".platform", lambda d: d["metadata"].update(type="SemanticModel"))
    findings, _ = validate_repository(repo, config)
    assert any("folder suffix says 'Report'" in m for m in _messages(findings))


@pytest.mark.parametrize("logical_id", ["", "00000000-0000-0000-0000-000000000000", "abc"])
def test_logical_id_must_be_real_guid(repo, config, logical_id):
    _edit_json(repo / CLAIMS_MODEL / ".platform", lambda d: d["config"].update(logicalId=logical_id))
    findings, _ = validate_repository(repo, config)
    assert any("logicalId must be a unique" in m for m in _messages(findings))


def test_duplicate_logical_id_across_teams(repo, config):
    claims = json.loads((repo / CLAIMS_MODEL / ".platform").read_text(encoding="utf-8"))
    other = repo / "src/csam-analytics/member-engagement/Member Engagement.SemanticModel/.platform"
    _edit_json(other, lambda d: d["config"].update(logicalId=claims["config"]["logicalId"]))
    findings, _ = validate_repository(repo, config)
    assert any("is also used by" in m for m in _messages(findings))


def test_duplicate_display_name_within_team(repo, config):
    src = repo / CLAIMS
    dst = repo / "src/finance-analytics/claims-copy"
    shutil.copytree(src, dst)
    for p in dst.glob("*/.platform"):
        _edit_json(p, lambda d: d["config"].update(logicalId="12345678-1234-1234-1234-123456789abc" if "Report" in
                                                     d["metadata"]["type"] else "12345678-1234-1234-1234-123456789abd"))
    findings, _ = validate_repository(repo, config)
    assert any("names must be unique within the team" in m for m in _messages(findings))


def test_report_must_reference_model_in_same_unit(repo, config):
    _edit_json(
        repo / CLAIMS_REPORT / "definition.pbir",
        lambda d: d["datasetReference"]["byPath"].update(path="../../executive-kpis/Executive KPIs.SemanticModel"),
    )
    findings, _ = validate_repository(repo, config)
    assert any("outside this unit" in m for m in _messages(findings))


def test_report_reference_to_missing_model(repo, config):
    shutil.rmtree(repo / CLAIMS_MODEL)
    findings, _ = validate_repository(repo, config)
    assert any("does not exist" in m for m in _messages(findings))


def test_live_connection_report_is_a_warning(repo, config):
    _edit_json(
        repo / CLAIMS_REPORT / "definition.pbir",
        lambda d: d.update(datasetReference={"byConnection": {"connectionString": "x"}}),
    )
    findings, _ = validate_repository(repo, config)
    assert findings.ok
    assert any("live connection" in m for m in _messages(findings, "warning"))


def test_semantic_model_needs_definition(repo, config):
    shutil.rmtree(repo / CLAIMS_MODEL / "definition")
    findings, _ = validate_repository(repo, config)
    assert any("neither a definition/ folder (TMDL)" in m for m in _messages(findings))


@pytest.mark.parametrize(
    "relpath", ["Claims Summary.pbix", "Claims Summary.Report/.pbi/localSettings.json", "Claims Summary.SemanticModel/.pbi/cache.abf"]
)
def test_forbidden_files(repo, config, relpath):
    target = repo / CLAIMS / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    findings, _ = validate_repository(repo, config)
    assert any("must not be committed" in m for m in _messages(findings))


def test_invalid_json_is_reported(repo, config):
    (repo / CLAIMS_REPORT / "definition" / "report.json").write_text("{oops", encoding="utf-8")
    findings, _ = validate_repository(repo, config)
    assert any("Invalid JSON" in m for m in _messages(findings))


def test_empty_unit_and_bad_unit_name(repo, config):
    (repo / "src/finance-analytics/Empty_Unit").mkdir()
    (repo / "src/finance-analytics/Empty_Unit/notes.txt").write_text("x", encoding="utf-8")
    findings, _ = validate_repository(repo, config)
    msgs = _messages(findings)
    assert any("lowercase letters" in m for m in msgs)
    assert any("contains no Power BI items" in m for m in msgs)


def test_unregistered_team_folder(repo, config):
    shutil.copytree(repo / CLAIMS, repo / "src/hr-analytics/headcount")
    findings, _ = validate_repository(repo, config)
    assert any("not registered" in m for m in _messages(findings))


def test_codeowners_must_cover_every_team(repo, config):
    co = repo / ".github/CODEOWNERS"
    co.write_text("\n".join(l for l in co.read_text().splitlines() if "finance" not in l), encoding="utf-8")
    findings, _ = validate_repository(repo, config, codeowners=co)
    assert any("no rule for /src/finance-analytics/" in m for m in _messages(findings))


def test_codeowners_must_name_configured_reviewers(repo, config):
    co = repo / ".github/CODEOWNERS"
    co.write_text(co.read_text().replace("@ORG/finance-analytics-reviewers", "@ORG/someone-else"), encoding="utf-8")
    findings, _ = validate_repository(repo, config, codeowners=co)
    assert any("does not list @ORG/finance-analytics-reviewers" in m for m in _messages(findings))


def test_scoped_validation_ignores_other_teams(repo, config):
    """A broken unit in one team must not block another team's pull request."""
    (repo / "src/csam-analytics/member-engagement/Member Engagement.Report/definition.pbir").write_text("{", "utf-8")
    findings, _ = validate_repository(
        repo, config, units=["finance-analytics/claims-summary"], teams={"finance-analytics"}
    )
    assert findings.ok
    findings, _ = validate_repository(repo, config)
    assert not findings.ok
