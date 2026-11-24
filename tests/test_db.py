from __future__ import annotations

import sqlite3

import pytest

from adjcheck import service
from adjcheck.db import Database, StorageError
from adjcheck.geometry import Rect
from adjcheck.models import BatchEntry, Stats


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "unit.sqlite3"))
    database.init_schema()
    return database


def test_schema_rejects_malformed_issue_rows(db):
    service.create_project(
        db, "P", Rect(0, 0, 10, 10), [BatchEntry("A", Rect(0, 0, 10, 10))]
    )
    with db.connection() as conn:
        project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
        # overlap needs at least two sheet numbers
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
                "VALUES (?, 'overlap', 0, 0, 1, 1, '[\"A\"]')",
                (project_id,),
            )
        # missing must carry no sheet numbers
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
                "VALUES (?, 'missing', 0, 0, 1, 1, '[\"A\",\"B\"]')",
                (project_id,),
            )
        # unknown kinds and zero-area rectangles are rejected too
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
                "VALUES (?, 'weird', 0, 0, 1, 1, '[]')",
                (project_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
                "VALUES (?, 'missing', 1, 1, 1, 2, '[]')",
                (project_id,),
            )
        conn.execute("DELETE FROM issues")


def test_sheet_numbers_are_unique_per_project(db):
    service.create_project(
        db, "P", Rect(0, 0, 10, 10), [BatchEntry("A", Rect(0, 0, 5, 5))]
    )
    with db.connection() as conn:
        project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sheets (project_id, no, x0, y0, x1, y1, position) "
                "VALUES (?, 'A', 5, 5, 10, 10, 9)",
                (project_id,),
            )


def test_delete_project_cascades(db):
    service.create_project(
        db, "P", Rect(0, 0, 10, 10),
        [BatchEntry("A", Rect(0, 0, 4, 4)), BatchEntry("B", Rect(6, 0, 10, 4))],
    )
    assert db.get_issues("P")
    assert db.delete_project("P")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_concurrent_issue_modification_aborts_merge(db):
    """If the stored result changes between read and write, nothing is merged."""

    service.create_project(
        db, "P", Rect(0, 0, 100, 100),
        [BatchEntry("A", Rect(0, 0, 40, 100)), BatchEntry("B", Rect(60, 0, 100, 100))],
    )
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project,
        sheets,
        issues,
        [BatchEntry("B", Rect(40, 0, 100, 100))],
        expected_version=0,
    )
    assert plan.accepted, plan.reasons
    # simulate an external writer inserting an extra issue row
    with db.connection() as conn:
        project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
        conn.execute(
            "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
            "VALUES (?, 'missing', 0, 90, 100, 100, '[]')",
            (project_id,),
        )
    with pytest.raises(StorageError):
        db.apply_correction("P", 0, plan)
    project, sheets, issues = db.get_state("P")
    assert project.version == 0
    assert any(i.rect.as_tuple() == (0, 90, 100, 100) for i in issues)


def test_issue_rows_outside_window_are_reused(db):
    # bottom band: gap at x=40..60; top band: overlap at x=50..100
    service.create_project(
        db, "P", Rect(0, 0, 200, 200),
        [
            BatchEntry("A", Rect(0, 0, 40, 100)),
            BatchEntry("B", Rect(60, 0, 200, 100)),
            BatchEntry("F1", Rect(0, 150, 100, 200)),
            BatchEntry("F2", Rect(50, 150, 150, 200)),
        ],
    )
    project, sheets, issues = db.get_state("P")
    far = [i for i in issues if i.rect.y0 >= 150]
    assert far
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("B", Rect(40, 0, 200, 100))], expected_version=0
    )
    assert plan.accepted, plan.reasons
    replaced = {i.id for i in plan.split.replaced}
    assert not (replaced & {i.id for i in far})
    ok, message = db.apply_correction("P", 0, plan)
    assert ok, message
    _, _, after = db.get_state("P")
    after_ids = {i.id for i in after}
    assert {i.id for i in far} <= after_ids


def test_rejected_attempt_is_logged_with_reason(db):
    service.create_project(
        db, "P", Rect(0, 0, 100, 100),
        [BatchEntry("A", Rect(0, 0, 40, 100)), BatchEntry("B", Rect(60, 0, 100, 100))],
    )
    project, sheets, issues = db.get_state("P")
    plan = service.build_plan(
        project, sheets, issues, [BatchEntry("A", Rect(0, 0, 70, 100))], expected_version=0
    )
    assert not plan.accepted
    log_id = db.log_rejection("P", 0, plan)
    assert log_id
    log = db.get_log("P")
    assert log[0]["status"] == "rejected"
    assert "新的重叠位置" in log[0]["reason"]
    assert log[0]["version_after"] is None
    project, sheets, issues = db.get_state("P")
    assert project.version == 0
    assert Stats.from_issues(issues).missing_area == 2000  # 20 x 100
