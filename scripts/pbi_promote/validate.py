"""Structural checks for PBIP units, run before anything is deployed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pbi_promote.config import GUID_RE, PLACEHOLDER_GUID, Config
from pbi_promote.findings import Findings
from pbi_promote.units import ITEM_SUFFIXES, UNIT_NAME_RE, discover_units, find_items, team_of

JSON_SUFFIXES = (".json", ".platform", ".pbir", ".pbism")


@dataclass(frozen=True)
class ItemInfo:
    unit: str
    path: Path
    item_type: str
    display_name: str
    logical_id: str


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _load_json(path: Path, findings: Findings, repo_root: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        findings.error(f"Invalid JSON: {exc}", _rel(path, repo_root))
        return None
    except OSError as exc:
        findings.error(f"Cannot read file: {exc}", _rel(path, repo_root))
        return None
    if not isinstance(data, dict):
        findings.error("Expected a JSON object.", _rel(path, repo_root))
        return None
    return data


def _check_report(item: Path, unit_dir: Path, findings: Findings, repo_root: Path) -> None:
    pbir = item / "definition.pbir"
    if not pbir.is_file():
        findings.error("Report is missing definition.pbir.", _rel(item, repo_root))
        return
    if not ((item / "definition").is_dir() or (item / "report.json").is_file()):
        findings.error("Report has neither a definition/ folder (PBIR) nor report.json.", _rel(item, repo_root))
    data = _load_json(pbir, findings, repo_root)
    if data is None:
        return
    ref = data.get("datasetReference") or {}
    by_path = (ref.get("byPath") or {}).get("path")
    if by_path:
        target = (item / by_path).resolve()
        if not target.is_dir() or not target.name.endswith(".SemanticModel"):
            findings.error(
                f"definition.pbir points to semantic model '{by_path}', which does not exist.", _rel(pbir, repo_root)
            )
        elif target.parent != unit_dir.resolve():
            findings.error(
                f"definition.pbir points to '{by_path}', which is outside this unit. A report and its "
                "semantic model must sit in the same unit folder so they are promoted together.",
                _rel(pbir, repo_root),
            )
    elif ref.get("byConnection"):
        findings.warning(
            "Report uses a live connection (byConnection). Its semantic model ID is not rewritten per "
            "environment unless config/parameter.yml handles it.",
            _rel(pbir, repo_root),
        )
    else:
        findings.error("definition.pbir has no datasetReference.", _rel(pbir, repo_root))


def _check_semantic_model(item: Path, findings: Findings, repo_root: Path) -> None:
    if not (item / "definition.pbism").is_file():
        findings.error("Semantic model is missing definition.pbism.", _rel(item, repo_root))
    if not ((item / "definition").is_dir() or (item / "model.bim").is_file()):
        findings.error("Semantic model has neither a definition/ folder (TMDL) nor model.bim.", _rel(item, repo_root))


def validate_unit(unit_key: str, unit_dir: Path, config: Config, repo_root: Path) -> tuple[Findings, list[ItemInfo]]:
    findings = Findings()
    infos: list[ItemInfo] = []
    gov = config.governance
    unit_name = unit_key.split("/", 1)[1]
    rel_unit = _rel(unit_dir, repo_root)

    if not UNIT_NAME_RE.match(unit_name):
        findings.error(
            f"Unit folder '{unit_name}' must use lowercase letters, digits and hyphens (for example claims-summary).",
            rel_unit,
        )

    for file in sorted(p for p in unit_dir.rglob("*") if p.is_file()):
        rel_in_unit = file.relative_to(unit_dir).as_posix()
        for pattern in gov.forbidden_file_patterns:
            if pattern.search(rel_in_unit):
                findings.error(
                    "This file must not be committed (binary, cache or user-specific settings). "
                    "Remove it and make sure .gitignore is in place.",
                    _rel(file, repo_root),
                )
                break
        if file.suffix.lower() in JSON_SUFFIXES or file.name == ".platform":
            _load_json(file, findings, repo_root)

    for child in sorted(p for p in unit_dir.iterdir() if p.is_dir()):
        if child.name.endswith(ITEM_SUFFIXES) and not (child / ".platform").is_file():
            findings.error(
                "Item folder has no .platform file, so it would be skipped. Re-save the project from "
                "Power BI Desktop.",
                _rel(child, repo_root),
            )

    items = find_items(unit_dir)
    if not items:
        findings.error(
            "Unit contains no Power BI items. Save the report from Power BI Desktop as a project (.pbip) "
            "into this folder.",
            rel_unit,
        )
        return findings, infos

    for item in items:
        rel_item = _rel(item, repo_root)
        if item.parent != unit_dir:
            findings.error("Item folders must sit directly inside the unit folder.", rel_item)
            continue
        suffix = next((s for s in ITEM_SUFFIXES if item.name.endswith(s)), None)
        if suffix is None:
            findings.error(f"Item folder name must end with {' or '.join(ITEM_SUFFIXES)}.", rel_item)
            continue
        expected_type = suffix[1:]
        if expected_type not in gov.item_types:
            findings.error(f"{expected_type} items are not enabled in governance.json 'itemTypes'.", rel_item)
            continue

        # Parse errors for .platform were already reported by the JSON sweep above.
        platform = _load_json(item / ".platform", Findings(), repo_root)
        if platform is None:
            continue
        meta = platform.get("metadata") or {}
        item_type = meta.get("type")
        display_name = (meta.get("displayName") or "").strip()
        logical_id = str((platform.get("config") or {}).get("logicalId") or "").strip()

        if item_type != expected_type:
            findings.error(f".platform type is '{item_type}' but the folder suffix says '{expected_type}'.", rel_item)
        if not display_name:
            findings.error(".platform metadata.displayName is empty.", rel_item)
        elif display_name != item.name[: -len(suffix)]:
            findings.warning(
                f"displayName '{display_name}' differs from the folder name. The workspace item is named "
                "after displayName.",
                rel_item,
            )
        if not GUID_RE.match(logical_id) or logical_id == PLACEHOLDER_GUID:
            findings.error(".platform config.logicalId must be a unique, non-zero GUID.", rel_item)

        if expected_type == "Report":
            _check_report(item, unit_dir, findings, repo_root)
        else:
            _check_semantic_model(item, findings, repo_root)

        infos.append(ItemInfo(unit_key, item, str(item_type), display_name, logical_id.lower()))

    return findings, infos


def _codeowners_entries(path: Path) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        entries.append((parts[0], parts[1:]))
    return entries


def check_codeowners(path: Path, config: Config, teams: set[str] | None = None) -> Findings:
    findings = Findings()
    entries = _codeowners_entries(path)
    if not entries:
        findings.error("CODEOWNERS file is missing or empty.", ".github/CODEOWNERS")
        return findings
    src = config.governance.src_root
    for key, team in sorted(config.teams.items()):
        if teams is not None and key not in teams:
            continue
        wanted = {f"/{src}/{key}/", f"/{src}/{key}/**", f"{src}/{key}/"}
        owners = None
        for pattern, line_owners in entries:  # last matching rule wins, as on GitHub
            if pattern in wanted:
                owners = line_owners
        if owners is None:
            findings.error(f"CODEOWNERS has no rule for /{src}/{key}/.", ".github/CODEOWNERS")
        elif team.reviewers.lower() not in (o.lower() for o in owners):
            findings.error(
                f"CODEOWNERS rule for /{src}/{key}/ does not list {team.reviewers} (from config/teams.json).",
                ".github/CODEOWNERS",
            )
    return findings


def validate_repository(
    repo_root: Path,
    config: Config,
    *,
    units: list[str] | None = None,
    teams: set[str] | None = None,
    codeowners: Path | None = None,
) -> tuple[Findings, list[ItemInfo]]:
    """Validate units and the cross-unit rules.

    units limits per-unit checks to the given units; teams limits the cross-unit checks to those teams.
    Pull requests pass both so a problem in one team never blocks another team's release.
    """
    repo_root = Path(repo_root)
    findings = Findings()
    src = config.governance.src_root
    available = discover_units(repo_root, src)

    team_dirs = sorted(p.name for p in (repo_root / src).iterdir() if p.is_dir()) if (repo_root / src).is_dir() else []
    for team in team_dirs:
        if (teams is None or team in teams) and team not in config.teams:
            findings.error(f"Folder {src}/{team}/ is not registered in config/teams.json.", f"{src}/{team}")
    if teams is None:
        for team in sorted(set(config.teams) - set(team_dirs)):
            findings.warning(f"Team '{team}' is registered but has no {src}/{team}/ folder yet.")

    scope = [u for u in available if teams is None or team_of(u) in teams]
    all_items: list[ItemInfo] = []
    for unit in scope:
        unit_findings, infos = validate_unit(unit, available[unit], config, repo_root)
        if units is None or unit in units:
            findings.extend(unit_findings)
        all_items.extend(infos)

    seen_ids: dict[str, ItemInfo] = {}
    seen_names: dict[tuple[str, str, str], ItemInfo] = {}
    for info in all_items:
        rel = _rel(info.path, repo_root)
        if info.logical_id and info.logical_id != PLACEHOLDER_GUID and GUID_RE.match(info.logical_id):
            if info.logical_id in seen_ids:
                findings.error(
                    f"logicalId {info.logical_id} is also used by {_rel(seen_ids[info.logical_id].path, repo_root)}. "
                    "Copied items need a new logicalId (generate a new GUID).",
                    rel,
                )
            else:
                seen_ids[info.logical_id] = info
        name_key = (team_of(info.unit), info.item_type, info.display_name.lower())
        if info.display_name and name_key in seen_names:
            findings.error(
                f"{info.item_type} '{info.display_name}' also exists in {seen_names[name_key].unit}. Items of the "
                "same type in one team share a workspace, so their names must be unique within the team.",
                rel,
            )
        else:
            seen_names[name_key] = info

    if codeowners is not None:
        findings.extend(check_codeowners(codeowners, config, teams))

    if units is not None:
        all_items = [i for i in all_items if i.unit in units]
    return findings, all_items
