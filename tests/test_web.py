from __future__ import annotations

import pytest

from adjcheck import service
from adjcheck.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_path=str(tmp_path / "web.sqlite3"))
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client, app


def test_index_lists_demo_projects(client):
    c, _ = client
    resp = c.get("/")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "图幅配准核查" in body
    for name in (service.DEMO_NAME, service.DISCONNECTED_NAME, service.CONFLICT_NAME):
        assert name in body


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").get_json() == {"ok": True}


def test_detail_with_verified_pair_shows_translation_and_both_paths(client):
    c, _ = client
    resp = c.get(f"/projects/{service.DEMO_NAME}?reference=A&target=C")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "已核实" in body
    assert "x = 3 m" in body and "y = -2 m" in body
    assert "M1 → M2" in body
    assert "M3" in body


def test_detail_shows_disconnected(client):
    c, _ = client
    body = c.get(f"/projects/{service.DISCONNECTED_NAME}?reference=P&target=R").data.decode()
    assert "失联" in body


def test_detail_shows_contradiction(client):
    c, _ = client
    body = c.get(f"/projects/{service.CONFLICT_NAME}?reference=X&target=Y").data.decode()
    assert "测量矛盾" in body
    assert "K2" in body


def test_verification_api(client):
    c, _ = client
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    )
    data = resp.get_json()["verification"]
    assert resp.status_code == 200
    assert data["status"] == "verified"
    assert data["translation"] == [3, -2]
    assert [path["measurements"] for path in data["paths"]] == [["M1", "M2"], ["M3"]]


def test_verification_api_rejects_same_sheet(client):
    c, _ = client
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "A"}
    )
    assert resp.status_code == 400
    assert "必须不同" in resp.get_json()["error"]


def test_configuration_requires_measurements_before_verifying(client):
    c, _ = client
    app = c.application
    db = app.extensions["adjcheck_db"]
    project = db.get_project(service.DEMO_NAME)
    service.apply_correction(
        db,
        service.DEMO_NAME,
        [("C", __import__("adjcheck.geometry", fromlist=["Rect"]).Rect(0, 0, 57, 100))],
        project.version,
    )
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    )
    data = resp.get_json()["verification"]
    assert data["status"] == "insufficient"
    assert [item["no"] for item in data["invalid"]] == ["M3"]
    # the original coordinate is still displayed on the page, marked as not used
    body = c.get(f"/projects/{service.DEMO_NAME}").data.decode()
    assert "57, 62" in body
    assert "不参与" in body


def test_add_measurement_form_and_api(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    before = len(db.get_snapshot(service.DEMO_NAME).measurements)
    form = {
        "no": "M9",
        "sheet_a": "A",
        "ax": "5",
        "ay": "5",
        "sheet_b": "B",
        "bx": "7",
        "by": "4",
    }
    resp = c.post(f"/projects/{service.DEMO_NAME}/measurements", data=form, follow_redirects=True)
    assert "测量已保存" in resp.data.decode()
    assert len(db.get_snapshot(service.DEMO_NAME).measurements) == before + 1


def test_add_measurement_rejects_unknown_sheet_and_duplicate(client):
    c, _ = client
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/measurements",
        json={"no": "M1", "sheet_a": "A", "ax": 1, "ay": 1, "sheet_b": "B", "bx": 1, "by": 1},
    )
    assert resp.status_code == 400
    assert "测量号重复" in resp.get_json()["error"]
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/measurements",
        json={"no": "M2", "sheet_a": "Z", "ax": 1, "ay": 1, "sheet_b": "B", "bx": 1, "by": 1},
    )
    assert resp.status_code == 400
    assert "不在现行目录" in resp.get_json()["error"]


def test_correction_flow_shows_before_and_after(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    project = db.get_project(service.DEMO_NAME)
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/correct",
        json={"version": project.version, "sheets": [["C", 0, 0, 57, 100]]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["applied"] is True
    data = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    ).get_json()["verification"]
    assert data["status"] == "insufficient"
    assert data["invalid"][0]["no"] == "M3"
    # measurements are kept verbatim
    state = c.get(f"/api/projects/{service.DEMO_NAME}/state").get_json()
    m3 = next(m for m in state["measurements"] if m["no"] == "M3")
    assert m3["point_b"] == [57, 62]


def test_correction_to_58_keeps_measurement_valid(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    project = db.get_project(service.DEMO_NAME)
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/correct",
        json={"version": project.version, "sheets": [["C", 0, 0, 58, 100]]},
    )
    assert resp.status_code == 200
    data = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    ).get_json()["verification"]
    # x=57 < 58, so M3 stays valid and both paths remain
    assert data["status"] == "verified"
    assert data["invalid"] == []
    assert data["translation"] == [3, -2]


def test_stale_correction_returns_409_and_keeps_state(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    project = db.get_project(service.DEMO_NAME)
    ok, _ = service.apply_correction(
        db, service.DEMO_NAME, [("C", __import__("adjcheck.geometry", fromlist=["Rect"]).Rect(0, 0, 90, 100))], project.version
    )
    assert ok
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/correct",
        json={"version": project.version, "sheets": [["C", 0, 0, 57, 100]]},
    )
    assert resp.status_code == 409
    assert "已过期" in resp.get_json()["message"]
    assert db.get_project(service.DEMO_NAME).version == project.version + 1


def test_correction_form_fallback(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    project = db.get_project(service.DEMO_NAME)
    resp = c.post(
        f"/projects/{service.DEMO_NAME}/correct",
        data={"version": str(project.version), "sheets": "C 0 0 57 100"},
        follow_redirects=True,
    )
    assert "更正已提交" in resp.data.decode()
    snapshot = db.get_snapshot(service.DEMO_NAME)
    assert snapshot.project.version == project.version + 1
    assert snapshot.sheet_map["C"].rect.x1 == 57
    assert len(snapshot.measurements) == len(service.DEMO_MEASUREMENTS)


def test_create_project_and_validation(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    resp = c.post(
        "/projects",
        data={"name": "NEW-1", "feature": "井盖", "sheets": "A 0 0 10 10\nB 10 0 20 10"},
        follow_redirects=True,
    )
    assert "已创建" in resp.data.decode()
    assert db.get_project("NEW-1") is not None
    resp = c.post(
        "/projects",
        data={"name": "BAD", "feature": "井盖", "sheets": "A 0 0 0 10"},
        follow_redirects=True,
    )
    assert "必须满足 x0&lt;x1" in resp.data.decode()


def test_correction_cannot_exceed_500_sheets(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    sheets = [f"S{index}" for index in range(499)]
    assert service.create_project(
        db, "BIG", "树", [(no, __import__("adjcheck.geometry", fromlist=["Rect"]).Rect(0, 0, 1, 1)) for no in sheets]
    )
    project = db.get_project("BIG")
    resp = c.post(
        "/api/projects/BIG/correct",
        json={"version": project.version, "sheets": [["X1", 0, 0, 1, 1], ["X2", 0, 0, 1, 1]]},
    )
    assert resp.status_code == 400
    assert "超过 500" in resp.get_json()["error"]
    # adding one sheet is still fine
    resp = c.post(
        "/api/projects/BIG/correct",
        json={"version": project.version, "sheets": [["X1", 0, 0, 1, 1]]},
    )
    assert resp.status_code == 200


def test_measurement_limit_is_120_via_api(client):
    c, app = client
    db = app.extensions["adjcheck_db"]
    existing = len(db.get_snapshot(service.DEMO_NAME).measurements)
    for index in range(existing, 120):
        resp = c.post(
            f"/api/projects/{service.DEMO_NAME}/measurements",
            json={
                "no": f"BULK{index}",
                "sheet_a": "A",
                "ax": 1,
                "ay": 1,
                "sheet_b": "B",
                "bx": 2,
                "by": 2,
            },
        )
        assert resp.status_code == 201, resp.get_json()
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/measurements",
        json={"no": "OVER", "sheet_a": "A", "ax": 1, "ay": 1, "sheet_b": "B", "bx": 2, "by": 2},
    )
    assert resp.status_code == 400
    assert "最多保存 120 条测量" in resp.get_json()["error"]


def test_persistence_across_app_restart(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    first = create_app(database_path=path)
    with first.test_client() as c:
        project = first.extensions["adjcheck_db"].get_project(service.DEMO_NAME)
        c.post(
            f"/api/projects/{service.DEMO_NAME}/correct",
            json={"version": project.version, "sheets": [["C", 0, 0, 57, 100]]},
        )
    second = create_app(database_path=path)
    with second.test_client() as c:
        data = c.post(
            f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
        ).get_json()["verification"]
        assert data["status"] == "insufficient"
        assert data["version"] == 1


def test_verification_uses_one_snapshot_even_if_catalog_changes(client, monkeypatch):
    """A single verification request must use the submitted catalog and measurements."""

    c, app = client
    db = app.extensions["adjcheck_db"]
    original = service.compute_verification
    captured = {}

    def wrapped(snapshot, reference, target):
        captured["snapshot"] = snapshot
        # simulate a concurrent correction between the read and the computation
        service.apply_correction(
            db,
            service.DEMO_NAME,
            [("C", __import__("adjcheck.geometry", fromlist=["Rect"]).Rect(0, 0, 57, 100))],
            snapshot.project.version,
        )
        return original(snapshot, reference, target)

    monkeypatch.setattr(service, "compute_verification", wrapped)
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    )
    assert resp.get_json()["verification"]["status"] == "verified"
    # the next request sees the corrected catalog
    monkeypatch.setattr(service, "compute_verification", original)
    resp = c.post(
        f"/api/projects/{service.DEMO_NAME}/verify", json={"reference": "A", "target": "C"}
    )
    assert resp.get_json()["verification"]["status"] == "insufficient"
