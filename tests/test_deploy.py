import dataclasses
import json

import pytest

from pbi_promote import deploy as deploy_mod
from pbi_promote.config import ConfigError
from pbi_promote.deploy import deploy, stage_units

FINANCE_TEST_WS = "22222222-2222-2222-2222-222222222222"
CSAM_TEST_WS = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def live_config(config):
    """The template config with real-looking workspace IDs for the test environment."""
    teams = dict(config.teams)
    for key, ws in (("finance-analytics", FINANCE_TEST_WS), ("csam-analytics", CSAM_TEST_WS)):
        team = teams[key]
        teams[key] = dataclasses.replace(team, workspaces={**team.workspaces, "test": ws})
    return dataclasses.replace(config, teams=teams)


class RecordingPublisher:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def __call__(self, **kwargs):
        staged = sorted(
            p.relative_to(kwargs["repository_directory"]).as_posix()
            for p in kwargs["repository_directory"].rglob("*")
            if p.is_file()
        )
        self.calls.append({**kwargs, "staged": staged})
        if kwargs["workspace_id"] in self.fail_for:
            raise RuntimeError("simulated Fabric outage")


def test_stage_only_selected_units(repo, tmp_path):
    labels = stage_units(repo, ["finance-analytics/claims-summary"], tmp_path / "s", "src", "preserve")
    assert labels == ["SemanticModel: Claims Summary", "Report: Claims Summary"] or sorted(labels) == [
        "Report: Claims Summary", "SemanticModel: Claims Summary"]
    top = sorted(p.name for p in (tmp_path / "s").iterdir())
    assert top == ["Claims Summary.Report", "Claims Summary.SemanticModel"]


def test_stage_excludes_local_files(repo, tmp_path):
    unit = repo / "src/finance-analytics/claims-summary"
    (unit / "Claims Summary.Report/.pbi").mkdir()
    (unit / "Claims Summary.Report/.pbi/localSettings.json").write_text("{}", "utf-8")
    (unit / "Claims Summary.SemanticModel/.pbi").mkdir(exist_ok=True)
    (unit / "Claims Summary.SemanticModel/.pbi/cache.abf").write_text("x", "utf-8")
    stage_units(repo, ["finance-analytics/claims-summary"], tmp_path / "s", "src", "preserve")
    staged = [p.name for p in (tmp_path / "s").rglob("*")]
    assert ".pbi" not in staged and "cache.abf" not in staged and "localSettings.json" not in staged


def test_stage_unit_folder_mode(repo, tmp_path):
    stage_units(repo, ["finance-analytics/executive-kpis"], tmp_path / "s", "src", "unit")
    assert [p.name for p in (tmp_path / "s").iterdir()] == ["executive-kpis"]


def test_stage_rejects_item_name_collision(repo, tmp_path):
    import shutil

    shutil.copytree(repo / "src/finance-analytics/claims-summary", repo / "src/finance-analytics/claims-copy")
    with pytest.raises(ConfigError, match="unique within a team workspace"):
        stage_units(
            repo, ["finance-analytics/claims-summary", "finance-analytics/claims-copy"], tmp_path / "s", "src", "preserve"
        )


def test_stage_rejects_unknown_unit(repo, tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        stage_units(repo, ["finance-analytics/nope"], tmp_path / "s", "src", "preserve")


def test_deploy_groups_units_by_team(repo, live_config):
    pub = RecordingPublisher()
    report = deploy(
        repo, live_config, "test",
        ["finance-analytics/claims-summary", "csam-analytics/member-engagement", "finance-analytics/executive-kpis"],
        publisher=pub,
    )
    assert report.ok
    assert {c["workspace_id"] for c in pub.calls} == {FINANCE_TEST_WS, CSAM_TEST_WS}
    finance = next(c for c in pub.calls if c["workspace_id"] == FINANCE_TEST_WS)
    assert not any(s.startswith("Member Engagement") for s in finance["staged"])
    assert finance["environment"] == "test"
    assert finance["parameter_file"].name == "parameter.yml" and finance["parameter_file"].is_absolute()
    assert finance["item_types"] == ["SemanticModel", "Report"]
    assert finance["folder_mode"] == "preserve"


def test_one_team_failure_does_not_stop_others(repo, live_config):
    pub = RecordingPublisher(fail_for={CSAM_TEST_WS})
    report = deploy(
        repo, live_config, "test", ["csam-analytics/member-engagement", "finance-analytics/claims-summary"],
        publisher=pub,
    )
    assert not report.ok
    status = {r.team: r.status for r in report.results}
    assert status == {"csam-analytics": "failed", "finance-analytics": "succeeded"}
    failed = next(r for r in report.results if r.status == "failed")
    assert "simulated Fabric outage" in failed.error
    data = json.loads(report.to_json())
    assert data["environment"] == "test" and len(data["results"]) == 2
    assert "failed: RuntimeError" in report.to_markdown()


def test_live_deploy_refuses_placeholder_workspace(repo, config):
    pub = RecordingPublisher()
    report = deploy(repo, config, "prod", ["finance-analytics/claims-summary"], publisher=pub)
    assert not report.ok and pub.calls == []
    assert "placeholder" in report.results[0].error


def test_dry_run_allows_placeholder_and_validates_parameters(repo, config):
    report = deploy(repo, config, "prod", ["finance-analytics/claims-summary"], dry_run=True)
    assert report.ok, report.to_json()
    assert report.results[0].status == "dry-run"


def test_dry_run_reports_bad_parameter_file(repo, config, tmp_path):
    bad = tmp_path / "parameter.yml"
    bad.write_text("find_replace:\n  - find_value: x\n", encoding="utf-8")
    report = deploy(repo, config, "test", ["finance-analytics/claims-summary"], dry_run=True, parameter_file=bad)
    assert not report.ok


def test_missing_parameter_file_is_an_error(repo, config, tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        deploy(repo, config, "test", ["finance-analytics/claims-summary"], parameter_file=tmp_path / "missing.yml")


def test_unknown_team_is_reported(repo, live_config):
    report = deploy(repo, live_config, "test", ["hr-analytics/x"], publisher=RecordingPublisher())
    assert report.results[0].status == "failed"


def test_deploy_module_never_unpublishes():
    source = deploy_mod.__file__
    text = open(source, encoding="utf-8").read()
    assert "unpublish" not in text.replace("never deleted", "")


CRED_VARS = ("AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
             "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")


@pytest.fixture
def clean_env(monkeypatch):
    for name in CRED_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_credential_uses_github_oidc_in_actions(clean_env):
    from azure.identity import ClientAssertionCredential

    clean_env.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    clean_env.setenv("AZURE_CLIENT_ID", "22222222-2222-2222-2222-222222222222")
    clean_env.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.actions.example/x?api-version=2.0")
    clean_env.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-token")
    assert isinstance(deploy_mod._credential(), ClientAssertionCredential)


def test_github_oidc_assertion_request(clean_env):
    import io

    clean_env.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.actions.example/x?api-version=2.0")
    clean_env.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-token")
    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _Resp(b'{"value": "github-jwt"}')

    clean_env.setattr(deploy_mod.urllib.request, "urlopen", fake_urlopen)
    assert deploy_mod.github_oidc_assertion() == "github-jwt"
    assert seen["url"].endswith("&audience=api%3A%2F%2FAzureADTokenExchange")
    assert seen["auth"] == "Bearer runner-token"


def test_credential_prefers_client_secret(clean_env):
    from azure.identity import ClientSecretCredential

    clean_env.setenv("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
    clean_env.setenv("AZURE_CLIENT_ID", "22222222-2222-2222-2222-222222222222")
    clean_env.setenv("AZURE_CLIENT_SECRET", "s")
    assert isinstance(deploy_mod._credential(), ClientSecretCredential)


def test_credential_secret_without_ids_is_an_error(clean_env):
    clean_env.setenv("AZURE_CLIENT_SECRET", "s")
    with pytest.raises(ConfigError):
        deploy_mod._credential()


def test_credential_falls_back_to_azure_cli(clean_env):
    from azure.identity import AzureCliCredential

    assert isinstance(deploy_mod._credential(), AzureCliCredential)
