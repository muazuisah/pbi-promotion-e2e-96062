import json
import shutil

import pytest

from pbi_promote.config import PLACEHOLDER_GUID, ConfigError, load_config


def _copy_config(template_root, tmp_path):
    dest = tmp_path / "config"
    shutil.copytree(template_root / "config", dest)
    return dest


def _edit(path, mutate):
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_template_config_loads(config):
    assert set(config.teams) == {"csam-analytics", "finance-analytics"}
    assert config.teams["finance-analytics"].display_name == "Finance Analytics"
    assert config.governance.max_teams_per_pull_request == 1
    assert config.governance.workspace_folder_mode == "preserve"
    assert config.root.is_absolute()


def test_placeholder_workspace_refused_unless_allowed(config):
    team = config.team("finance-analytics")
    with pytest.raises(ConfigError, match="placeholder"):
        team.workspace_id("prod")
    assert team.workspace_id("prod", allow_placeholder=True) == PLACEHOLDER_GUID


def test_real_workspace_id_is_returned(template_root, tmp_path):
    cfg = _copy_config(template_root, tmp_path)
    ws = "11111111-2222-3333-4444-555555555555"
    _edit(cfg / "teams.json", lambda d: d["teams"]["csam-analytics"]["workspaces"].update(prod=ws.upper()))
    assert load_config(cfg).team("csam-analytics").workspace_id("prod") == ws


def test_unknown_team_lookup_fails(config):
    with pytest.raises(ConfigError, match="not defined"):
        config.team("hr-analytics")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["teams"].update({"Finance Analytics": d["teams"]["finance-analytics"]}), "lowercase"),
        (lambda d: d["teams"]["csam-analytics"]["workspaces"].pop("test"), "workspaces.test"),
        (lambda d: d["teams"]["csam-analytics"]["workspaces"].update(dev="not-a-guid"), "workspaces.dev"),
        (lambda d: d["teams"]["csam-analytics"].update(reviewers="csam team"), "reviewers"),
        (lambda d: d.update(teams={}), "non-empty"),
    ],
)
def test_invalid_teams_rejected(template_root, tmp_path, mutate, message):
    cfg = _copy_config(template_root, tmp_path)
    _edit(cfg / "teams.json", mutate)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(branchNamePattern="^(STRY)[0-9]+$"), "workItem"),
        (lambda d: d.update(pullRequestTitlePattern="(unclosed"), "not a valid regex"),
        (lambda d: d.update(maxTeamsPerPullRequest=0), "maxTeamsPerPullRequest"),
        (lambda d: d.update(workspaceFolderMode="flat"), "workspaceFolderMode"),
        (lambda d: d.update(itemTypes=["Notebook"]), "itemTypes"),
        (lambda d: d.pop("branchNamePattern"), "branchNamePattern"),
    ],
)
def test_invalid_governance_rejected(template_root, tmp_path, mutate, message):
    cfg = _copy_config(template_root, tmp_path)
    _edit(cfg / "governance.json", mutate)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_missing_and_malformed_files(template_root, tmp_path):
    cfg = _copy_config(template_root, tmp_path)
    (cfg / "teams.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        load_config(cfg)
    (cfg / "teams.json").unlink()
    with pytest.raises(ConfigError, match="Missing configuration file"):
        load_config(cfg)
