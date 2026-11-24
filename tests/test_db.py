from __future__ import annotations

import sqlite3

import pytest

from adjcheck import service
from adjcheck.db import Database
from adjcheck.errors import StorageError
from adjcheck.geometry import Point, Rect
from adjcheck.models import Measurement


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "unit.sqlite3"))
    database.init_schema()
    return database


def make(db: Database, name: str = "P") -> None:
    assert service.create_project(db, name, "树", [("A", Rect(0, 0, 10, 10)), ("B", Rect(0, 0, 10, 10))])


def test_measurement_number_is_unique_per_project(db):
    make(db)
    service.add_measurement(db, "P", Measurement("M1", "A", Point(1, 1), "B", Point(1, 1)))
    with pytest.raises(service.ValidationError):
        service.add_measurement(db, "P", Measurement("M1", "A", Point(2, 2), "B", Point(2, 2)))


def test_measurement_limit_is_120(db):
    make(db)
    for index in range(120):
        service.add_measurement(
            db, "P", Measurement(f"M{index}", "A", Point(1, 1), "B", Point(1, 1))
        )
    with pytest.raises(service.ValidationError):
        service.add_measurement(db, "P", Measurement("M120", "A", Point(1, 1), "B", Point(1, 1)))
    # the database enforces the same limit even if the service check is bypassed
    with pytest.raises(StorageError):
        db.add_measurement("P", Measurement("M120", "A", Point(1, 1), "B", Point(1, 1)))


def test_schema_rejects_same_sheet_on_both_sides(db):
    make(db)
    with db.connection() as conn:
        project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO measurements (project_id, no, sheet_a, ax, ay, sheet_b, bx, by, created_at) "
                "VALUES (?, 'M1', 'A', 1, 1, 'A', 1, 1, 'now')",
                (project_id,),
            )


def test_correction_keeps_measurements_and_bumps_version(db):
    make(db)
    service.add_measurement(db, "P", Measurement("M1", "A", Point(1, 1), "B", Point(9, 9)))
    project = db.get_project("P")
    ok, message = service.apply_correction(db, "P", [("B", Rect(0, 0, 5, 5))], project.version)
    assert ok, message
    snapshot = db.get_snapshot("P")
    assert snapshot.project.version == 1
    assert snapshot.sheet_map["B"].rect == Rect(0, 0, 5, 5)
    # original coordinates are preserved verbatim
    measurement = snapshot.measurements[0]
    assert (measurement.point_a.x, measurement.point_a.y) == (1, 1)
    assert (measurement.point_b.x, measurement.point_b.y) == (9, 9)
    # the measurement is now invalid, because 9 is outside B's new 0..5 range
    validity = service.valid_edges(snapshot)[1]
    assert validity[0].valid is False


def test_stale_version_is_rejected_without_writes(db):
    make(db)
    ok, message = service.apply_correction(db, "P", [("B", Rect(0, 0, 5, 5))], 7)
    assert not ok
    assert "已过期" in message
    assert db.get_project("P").version == 0
    assert db.get_snapshot("P").sheet_map["B"].rect == Rect(0, 0, 10, 10)


def test_correction_can_add_a_sheet_and_logs_it(db):
    make(db)
    project = db.get_project("P")
    ok, _ = service.apply_correction(db, "P", [("C", Rect(20, 0, 30, 10))], project.version)
    assert ok
    snapshot = db.get_snapshot("P")
    assert [s.no for s in snapshot.sheets] == ["A", "B", "C"]
    log = db.get_log("P")
    assert log[0]["status"] == "applied"
    assert [item["no"] for item in log[0]["batch"]] == ["C"]


def test_original_coordinates_survive_close_and_reopen(db):
    make(db)
    service.add_measurement(db, "P", Measurement("M1", "A", Point(3, 4), "B", Point(5, 6)))
    reopened = Database(db.path)
    measurement = reopened.get_snapshot("P").measurements[0]
    assert measurement.point_a == Point(3, 4)
    assert measurement.point_b == Point(5, 6)


def test_delete_cascades_sheets_and_measurements(db):
    make(db)
    service.add_measurement(db, "P", Measurement("M1", "A", Point(1, 1), "B", Point(1, 1)))
    assert db.delete_project("P")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0] == 0


def test_snapshot_is_consistent_while_a_correction_is_applied(db):
    """A verification that holds a snapshot is unaffected by a concurrent correction."""

    make(db)
    service.add_measurement(db, "P", Measurement("M1", "A", Point(1, 1), "B", Point(9, 9)))
    first = db.get_snapshot("P")
    ok, _ = service.apply_correction(db, "P", [("B", Rect(0, 0, 5, 5))], first.project.version)
    assert ok
    # the held snapshot still uses the submitted catalog and measurements
    before = service.valid_edges(first)[1]
    assert before[0].valid is True
    # the next verification re-judges against the corrected catalog
    after = service.valid_edges(db.get_snapshot("P"))[1]
    assert after[0].valid is False
