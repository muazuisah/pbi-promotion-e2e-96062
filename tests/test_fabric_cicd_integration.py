"""Runs the real fabric-cicd publish path against an in-memory fake of the Fabric REST API.

This proves what the pipeline sends to Fabric for a selective deployment:
the right workspace, models before reports, report bindings rewritten to the
deployed model, parameter.yml values applied, folders left alone and nothing
deleted.
"""

import base64
import dataclasses
import json
import re
import uuid

import pytest

fabric_cicd = pytest.importorskip("fabric_cicd")

from fabric_cicd import constants  # noqa: E402

from pbi_promote import deploy as deploy_mod  # noqa: E402
from pbi_promote.deploy import deploy  # noqa: E402

WS = "44444444-4444-4444-4444-444444444444"
EXISTING_MODEL_ID = "55555555-5555-5555-5555-555555555555"
ADMIN_FOLDER_ID = "66666666-6666-6666-6666-666666666666"


class FakeCredential:
    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken

        return AccessToken("fake-token", 4102444800)


class FakeFabric:
    def __init__(self, existing=()):
        self.calls = []
        self.items = {i["id"]: i for i in existing}

    def endpoint_factory(self, *args, **kwargs):
        fake = self

        class _Endpoint:
            def invoke(self, method, url, body="{}", **_):
                return fake.handle(method, url, body)

        return _Endpoint()

    @staticmethod
    def _ok(body=None):
        return {"header": {}, "body": body or {}, "status_code": 200}

    def handle(self, method, url, body):
        path = re.sub(r"^https://[^/]+", "", url)
        self.calls.append((method, path, body))
        if path == f"/v1/workspaces/{WS}" and method == "GET":
            return self._ok({"id": WS, "displayName": "Finance [TEST]", "capacityId": "cap-1"})
        if not path.startswith(f"/v1/workspaces/{WS}/"):
            raise AssertionError(f"Call to unexpected workspace: {method} {path}")
        rest = path[len(f"/v1/workspaces/{WS}"):]
        if method == "GET" and rest == "/items":
            return self._ok({"value": list(self.items.values())})
        if method == "GET" and rest == "/folders":
            return self._ok({"value": [{"id": ADMIN_FOLDER_ID, "displayName": "Certified"}]})
        if method == "POST" and rest == "/items":
            new_id = str(uuid.uuid4())
            self.items[new_id] = {
                "id": new_id, "type": body["type"], "displayName": body["displayName"], "description": "",
            }
            return self._ok({"id": new_id})
        if method == "POST" and rest.endswith("/updateDefinition?updateMetadata=True"):
            return self._ok()
        raise AssertionError(f"Unexpected Fabric call: {method} {path}")


def _parts(body):
    return {
        p["path"]: base64.b64decode(p["payload"]).decode("utf-8")
        for p in body["definition"]["parts"]
    }


@pytest.fixture
def fabric(monkeypatch, config):
    flags_before = set(constants.FEATURE_FLAG)
    fake = FakeFabric(existing=[{
        "id": EXISTING_MODEL_ID, "type": "SemanticModel", "displayName": "Claims Summary",
        "description": "", "folderId": ADMIN_FOLDER_ID,
    }])
    monkeypatch.setattr(fabric_cicd.fabric_workspace, "FabricEndpoint", fake.endpoint_factory)
    monkeypatch.setattr(deploy_mod, "_credential", lambda: FakeCredential())
    yield fake
    constants.FEATURE_FLAG.clear()
    constants.FEATURE_FLAG.update(flags_before)


@pytest.fixture
def finance_config(config):
    team = config.teams["finance-analytics"]
    teams = {**config.teams, "finance-analytics": dataclasses.replace(team, workspaces={**team.workspaces, "test": WS})}
    return dataclasses.replace(config, teams=teams)


def test_selective_publish_through_real_fabric_cicd(repo, finance_config, fabric):
    report = deploy(repo, finance_config, "test", ["finance-analytics/claims-summary"], commit="abc123")
    assert report.ok, report.to_json()

    writes = [(m, p, b) for m, p, b in fabric.calls if m != "GET"]
    assert not any(m == "DELETE" for m, _, _ in fabric.calls), "nothing may be deleted"
    assert not any(p.endswith("/move") for _, p, _ in writes), "admin-placed folders must be preserved"

    # The existing model is updated in place; the new report is created afterwards.
    assert writes[0][1].endswith(f"/items/{EXISTING_MODEL_ID}/updateDefinition?updateMetadata=True")
    created = [b for m, p, b in writes if m == "POST" and p.endswith("/items")]
    assert [b["displayName"] for b in created] == ["Claims Summary"]
    assert created[0]["type"] == "Report"

    model_files = _parts(writes[0][2])
    expressions = model_files["definition/expressions.tmdl"]
    assert "test-sql.abcbs.example" in expressions and "dev-sql.abcbs.example" not in expressions
    assert "FinanceTest" in expressions and "FinanceDev" not in expressions

    pbir = json.loads(_parts(created[0])["definition.pbir"])
    conn = pbir["datasetReference"]["byConnection"]
    assert EXISTING_MODEL_ID in json.dumps(conn)
    assert "byPath" not in pbir["datasetReference"]


def test_other_units_of_the_team_are_not_sent(repo, finance_config, fabric):
    deploy(repo, finance_config, "test", ["finance-analytics/claims-summary"])
    names = {b.get("displayName") for m, _, b in fabric.calls if m == "POST" and isinstance(b, dict)}
    assert not any(n and n.startswith("Executive KPIs") for n in names)
    sent_paths = json.dumps([b for _, _, b in fabric.calls])
    assert "Executive KPIs" not in sent_paths


def test_unit_folder_mode_creates_folder(repo, finance_config, fabric):
    governance = dataclasses.replace(finance_config.governance, workspace_folder_mode="unit")
    cfg = dataclasses.replace(finance_config, governance=governance)
    created_folders = []

    original = fabric.handle

    def handle(method, url, body):
        if method == "POST" and url.endswith("/folders"):
            created_folders.append(body["displayName"])
            fabric.calls.append((method, url, body))
            return fabric._ok({"id": str(uuid.uuid4())})
        return original(method, url, body)

    fabric.handle = handle
    report = deploy(repo, cfg, "test", ["finance-analytics/executive-kpis"])
    assert report.ok, report.to_json()
    assert created_folders == ["executive-kpis"]
