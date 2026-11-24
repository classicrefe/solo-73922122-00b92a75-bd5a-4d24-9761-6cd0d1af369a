from __future__ import annotations

import pytest

from adjcheck.app import create_app
from adjcheck.db import Database


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_path=str(tmp_path / "web.sqlite3"))
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client, app


DEMO_BATCH = [
    {"no": "S3", "x0": 1000, "y0": 0, "x1": 1100, "y1": 500},
    {"no": "S5", "x0": 1000, "y0": 500, "x1": 1100, "y1": 1000},
]


def test_index_lists_demo_project(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "DEMO-ADJ-001" in body
    assert "接幅核查" in body


def test_detail_shows_missing_and_overlap_with_all_sheet_numbers(client):
    c, _ = client
    resp = c.get("/projects/DEMO-ADJ-001")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "缺片区（1）" in body
    assert "重叠区（2）" in body
    # every sheet number participating in the overlap is rendered
    for token in ["S1", "S2", "S3"]:
        assert token in body
    assert "1000 500 1100 1000" in body  # missing rectangle
    assert "1100 0 1400 500" in body  # overlap rectangle


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").get_json() == {"ok": True}


def test_create_project_validation_errors(client):
    c, app = client
    resp = c.post(
        "/projects",
        data={"name": "BAD", "x0": "0", "y0": "0", "x1": "0", "y1": "10", "sheets": "A 0 0 1 1"},
        follow_redirects=True,
    )
    assert "必须满足 x0&lt;x1" in resp.data.decode()
    resp = c.post(
        "/projects",
        data={"name": "BAD", "x0": "0", "y0": "0", "x1": "10", "y1": "10", "sheets": "A 0 0 1"},
        follow_redirects=True,
    )
    assert "格式应为" in resp.data.decode()
    db: Database = app.extensions["adjcheck_db"]
    assert db.get_project("BAD") is None


def test_create_project_success_and_limit(client):
    c, app = client
    sheets = "\n".join(f"S{i} {i * 10} 0 {i * 10 + 9} 10" for i in range(3))
    resp = c.post(
        "/projects",
        data={"name": "P1", "x0": "0", "y0": "0", "x1": "100", "y1": "10", "sheets": sheets},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert "已创建" in body
    assert "缺片区（3）" in body  # gaps between the three sheets, plus trailing extent
    db: Database = app.extensions["adjcheck_db"]
    project, stored = db.get_sheets("P1")
    assert len(stored) == 3

    too_many = "\n".join(f"S{i} {i} 0 {i + 1} 1" for i in range(101))
    resp = c.post(
        "/projects",
        data={"name": "P2", "x0": "0", "y0": "0", "x1": "200", "y1": "10", "sheets": too_many},
        follow_redirects=True,
    )
    assert "最多 100 张图幅" in resp.data.decode()
    assert db.get_project("P2") is None


def test_api_check_rejects_bad_batch(client):
    c, _ = client
    resp = c.post("/api/projects/DEMO-ADJ-001/check", json={"batch": [], "version": 0})
    assert resp.status_code == 400
    resp = c.post(
        "/api/projects/DEMO-ADJ-001/check",
        json={"batch": DEMO_BATCH * 3, "version": 0},
    )
    assert resp.status_code == 400
    assert "最多更正 4 张" in resp.get_json()["error"]


def test_api_check_then_apply_then_state(client):
    c, _ = client
    plan = c.post("/api/projects/DEMO-ADJ-001/check", json={"batch": DEMO_BATCH, "version": 0})
    body = plan.get_json()
    assert plan.status_code == 200
    assert body["accepted"] is True
    assert body["eliminated"]["missing_area"] == 50000
    assert body["eliminated"]["overlap_area"] == 200000

    applied = c.post("/api/projects/DEMO-ADJ-001/apply", json={"batch": DEMO_BATCH, "version": 0})
    payload = applied.get_json()
    assert applied.status_code == 200 and payload["applied"] is True

    state = c.get("/api/projects/DEMO-ADJ-001/state").get_json()
    assert state["project"]["version"] == 1
    assert state["stats"]["missing_count"] == 0
    assert state["stats"]["overlap_count"] == 0
    assert [s["no"] for s in state["sheets"]] == ["S1", "S2", "S3", "S5"]

    page = c.get("/projects/DEMO-ADJ-001")
    assert "更正已提交" not in page.data.decode()  # flash was on the previous request
    assert "更正记录" in page.data.decode()


def test_api_apply_rejected_keeps_state_and_logs(client):
    c, _ = client
    bad = [{"no": "S3", "x0": 1050, "y0": 0, "x1": 1150, "y1": 500}]
    resp = c.post("/api/projects/DEMO-ADJ-001/apply", json={"batch": bad, "version": 0})
    payload = resp.get_json()
    assert resp.status_code == 200
    assert payload["applied"] is False
    assert "退回" in payload["message"]
    assert payload["plan"]["new_problems"]["missing_area"] > 0
    state = c.get("/api/projects/DEMO-ADJ-001/state").get_json()
    assert state["project"]["version"] == 0
    assert state["stats"]["missing_count"] == 1 and state["stats"]["overlap_count"] == 2
    assert [s["no"] for s in state["sheets"]] == ["S1", "S2", "S3"]


def test_api_apply_stale_version_conflict(client):
    c, _ = client
    assert c.post(
        "/api/projects/DEMO-ADJ-001/apply", json={"batch": DEMO_BATCH, "version": 0}
    ).get_json()["applied"]
    resp = c.post("/api/projects/DEMO-ADJ-001/apply", json={"batch": DEMO_BATCH, "version": 0})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["applied"] is False
    assert "过期" in body["message"]
    state = c.get("/api/projects/DEMO-ADJ-001/state").get_json()
    assert state["project"]["version"] == 1


def test_form_fallback_check_and_apply(client):
    c, _ = client
    text = "S3 1000 0 1100 500\nS5 1000 500 1100 1000\n"
    pre = c.post(
        "/projects/DEMO-ADJ-001/correct",
        data={"action": "check", "version": "0", "batch": text},
        follow_redirects=True,
    )
    assert "预检通过" in pre.data.decode()
    applied = c.post(
        "/projects/DEMO-ADJ-001/correct",
        data={"action": "apply", "version": "0", "batch": text},
        follow_redirects=True,
    )
    assert "更正已写入" in applied.data.decode()
    state = c.get("/api/projects/DEMO-ADJ-001/state").get_json()
    assert state["project"]["version"] == 1 and state["stats"]["problem_area"] == 0


def test_form_fallback_rejects_invalid_version(client):
    c, _ = client
    resp = c.post(
        "/projects/DEMO-ADJ-001/correct",
        data={"action": "check", "version": "abc", "batch": "S3 1000 0 1100 500"},
        follow_redirects=True,
    )
    assert "版本号无效" in resp.data.decode()


def test_404_for_unknown_project(client):
    c, _ = client
    assert c.get("/projects/NOPE").status_code == 404
    assert c.get("/api/projects/NOPE/state").status_code == 404
    assert c.post("/api/projects/NOPE/check", json={"batch": DEMO_BATCH}).status_code == 404


def test_write_failure_reports_and_keeps_state(client):
    c, app = client
    db: Database = app.extensions["adjcheck_db"]
    with db.connection() as conn:
        conn.executescript(
            """
            CREATE TRIGGER fail_sheet_insert BEFORE INSERT ON sheets
            BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END;
            """
        )
    resp = c.post("/api/projects/DEMO-ADJ-001/apply", json={"batch": DEMO_BATCH, "version": 0})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["applied"] is False
    assert "写入失败" in body["message"]
    state = c.get("/api/projects/DEMO-ADJ-001/state").get_json()
    assert state["project"]["version"] == 0
    assert state["stats"] == {
        "missing_count": 1,
        "missing_area": 50000,
        "overlap_count": 2,
        "overlap_area": 200000,
        "problem_area": 250000,
    }


def test_state_persists_across_app_restart(tmp_path):
    path = str(tmp_path / "persist.sqlite3")
    app1 = create_app(database_path=path)
    with app1.test_client() as c1:
        assert c1.post(
            "/api/projects/DEMO-ADJ-001/apply", json={"batch": DEMO_BATCH, "version": 0}
        ).get_json()["applied"]
    app2 = create_app(database_path=path)
    with app2.test_client() as c2:
        state = c2.get("/api/projects/DEMO-ADJ-001/state").get_json()
        assert state["project"]["version"] == 1
        assert state["stats"]["problem_area"] == 0
