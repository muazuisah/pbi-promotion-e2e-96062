import pytest

from pbi_promote.units import SelectorError, classify_paths, discover_units, find_items, resolve_selector


def test_classify_paths_splits_units_teams_and_platform():
    cs = classify_paths(
        [
            "src/finance-analytics/claims-summary/Claims Summary.Report/definition.pbir",
            "src/finance-analytics/executive-kpis/Executive KPIs.SemanticModel/definition/model.tmdl",
            "src/finance-analytics/README.md",
            "src/README.md",
            ".github/workflows/deploy.yml",
            "config/teams.json",
        ]
    )
    assert cs.units == {"finance-analytics/claims-summary", "finance-analytics/executive-kpis"}
    assert cs.teams == {"finance-analytics"}
    assert cs.team_level_paths == ["src/finance-analytics/README.md"]
    assert cs.platform_paths == ["src/README.md", ".github/workflows/deploy.yml", "config/teams.json"]


def test_classify_handles_windows_separators():
    cs = classify_paths(["src\\csam-analytics\\member-engagement\\x.json"])
    assert cs.units == {"csam-analytics/member-engagement"}


def test_discover_units_and_items(template_root):
    units = discover_units(template_root)
    assert sorted(units) == [
        "csam-analytics/member-engagement",
        "finance-analytics/claims-summary",
        "finance-analytics/executive-kpis",
    ]
    names = [p.name for p in find_items(units["finance-analytics/executive-kpis"])]
    assert names == ["Executive KPIs - Overview.Report", "Executive KPIs - Trends.Report", "Executive KPIs.SemanticModel"]


def test_selector_single_wildcard_and_dedup(template_root):
    units = discover_units(template_root)
    assert resolve_selector("finance-analytics/claims-summary", units) == ["finance-analytics/claims-summary"]
    assert resolve_selector("finance-analytics/*, finance-analytics/claims-summary", units) == [
        "finance-analytics/claims-summary",
        "finance-analytics/executive-kpis",
    ]
    assert resolve_selector("csam-analytics/*;finance-analytics/executive-kpis", units) == [
        "csam-analytics/member-engagement",
        "finance-analytics/executive-kpis",
    ]


@pytest.mark.parametrize(
    "selector, message",
    [
        ("", "No units"),
        ("finance-analytics", "Invalid unit selector"),
        ("Finance/claims", "Invalid unit selector"),
        ("finance-analytics/../../etc", "Invalid unit selector"),
        ("finance-analytics/claims-summary; rm -rf /", "Invalid unit selector"),
        ("finance-analytics/missing", "does not exist"),
        ("hr-analytics/*", "matched no units"),
    ],
)
def test_selector_errors(template_root, selector, message):
    with pytest.raises(SelectorError, match=message):
        resolve_selector(selector, discover_units(template_root))
