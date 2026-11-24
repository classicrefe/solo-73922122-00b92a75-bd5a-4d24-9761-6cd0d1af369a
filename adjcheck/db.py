"""SQLite 持久化：图幅目录、测量表与图幅更正审计。

所有查询都通过 :meth:`Database.get_snapshot` 或 :meth:`Database.get_project`，
一次读取得到目录与测量表的一致快照。更正写入在单个事务内完成并校验项目
版本，因此页面上的旧版本不会覆盖更新后的目录；测量表的原始坐标从不改写。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import StorageError
from .geometry import Point, Rect
from .models import Measurement, Project, Sheet, Snapshot


@dataclass(frozen=True)
class CatalogProject:
    project: Project
    sheet_count: int
    measurement_count: int


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    feature     TEXT    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (length(feature) BETWEEN 1 AND 120)
);

CREATE TABLE IF NOT EXISTS sheets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    no           TEXT    NOT NULL,
    x0           INTEGER NOT NULL,
    y0           INTEGER NOT NULL,
    x1           INTEGER NOT NULL,
    y1           INTEGER NOT NULL,
    position     INTEGER NOT NULL,
    UNIQUE (project_id, no),
    CHECK (x0 < x1 AND y0 < y1)
);

CREATE TABLE IF NOT EXISTS measurements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    no           TEXT    NOT NULL,
    sheet_a      TEXT    NOT NULL,
    ax           INTEGER NOT NULL,
    ay           INTEGER NOT NULL,
    sheet_b      TEXT    NOT NULL,
    bx           INTEGER NOT NULL,
    by           INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (project_id, no),
    CHECK (sheet_a <> sheet_b)
);

CREATE TRIGGER IF NOT EXISTS measurement_limit
BEFORE INSERT ON measurements
WHEN (SELECT COUNT(*) FROM measurements WHERE project_id = NEW.project_id) >= 120
BEGIN
    SELECT RAISE(ABORT, '每个项目最多保存 120 条测量');
END;

CREATE INDEX IF NOT EXISTS idx_sheets_project ON sheets(project_id);
CREATE INDEX IF NOT EXISTS idx_measurements_project ON measurements(project_id);

CREATE TABLE IF NOT EXISTS correction_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at     TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    batch          TEXT    NOT NULL,
    version_before INTEGER NOT NULL,
    version_after  INTEGER
);
"""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _rect_from_row(row: sqlite3.Row) -> Rect:
    return Rect(row["x0"], row["y0"], row["x1"], row["y1"])


def _row_measurement(row: sqlite3.Row) -> Measurement:
    return Measurement(
        no=row["no"],
        sheet_a=row["sheet_a"],
        point_a=Point(row["ax"], row["ay"]),
        sheet_b=row["sheet_b"],
        point_b=Point(row["bx"], row["by"]),
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            name=row["name"],
            feature=row["feature"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_project(self, name: str) -> Project | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return None if row is None else self._project_from_row(row)

    def list_catalog(self) -> list[CatalogProject]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM sheets s WHERE s.project_id = p.id) AS sheet_count,
                       (SELECT COUNT(*) FROM measurements m WHERE m.project_id = p.id)
                           AS measurement_count
                  FROM projects p
                 ORDER BY p.name
                """
            ).fetchall()
        return [
            CatalogProject(
                project=self._project_from_row(row),
                sheet_count=row["sheet_count"],
                measurement_count=row["measurement_count"],
            )
            for row in rows
        ]

    def get_snapshot(self, name: str) -> Snapshot | None:
        """Read the project, its sheets and its measurements in one snapshot."""

        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                project_id = row["id"]
                sheet_rows = conn.execute(
                    "SELECT no, x0, y0, x1, y1 FROM sheets "
                    "WHERE project_id = ? ORDER BY position, no",
                    (project_id,),
                ).fetchall()
                measurement_rows = conn.execute(
                    "SELECT * FROM measurements WHERE project_id = ? ORDER BY id",
                    (project_id,),
                ).fetchall()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return Snapshot(
            project=self._project_from_row(row),
            sheets=tuple(Sheet(no=r["no"], rect=_rect_from_row(r)) for r in sheet_rows),
            measurements=tuple(_row_measurement(r) for r in measurement_rows),
        )

    def get_log(self, name: str, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT l.* FROM correction_log l JOIN projects p ON p.id = l.project_id
                 WHERE p.name = ? ORDER BY l.id DESC LIMIT ?
                """,
                (name, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "status": row["status"],
                "reason": row["reason"],
                "batch": json.loads(row["batch"]),
                "version_before": row["version_before"],
                "version_after": row["version_after"],
            }
            for row in rows
        ]

    # --------------------------------------------------------------- writes

    def create_project(
        self, name: str, feature: str, sheets: Sequence[Sheet]
    ) -> bool:
        """Create a project atomically.  Returns False when the name exists."""

        try:
            with self._transaction() as conn:
                if conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                    return False
                now = _now()
                cur = conn.execute(
                    "INSERT INTO projects (name, feature, version, created_at, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (name, feature, now, now),
                )
                project_id = cur.lastrowid
                for index, sheet in enumerate(sheets):
                    conn.execute(
                        "INSERT INTO sheets (project_id, no, x0, y0, x1, y1, position) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (project_id, sheet.no, *sheet.rect.as_tuple(), index),
                    )
            return True
        except sqlite3.Error as exc:
            raise StorageError(f"写入初始数据失败: {exc}") from exc

    def add_measurement(self, name: str, measurement: Measurement) -> None:
        try:
            with self._transaction() as conn:
                row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
                if row is None:
                    raise StorageError("项目不存在")
                conn.execute(
                    "INSERT INTO measurements "
                    "(project_id, no, sheet_a, ax, ay, sheet_b, bx, by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        measurement.no,
                        measurement.sheet_a,
                        measurement.point_a.x,
                        measurement.point_a.y,
                        measurement.sheet_b,
                        measurement.point_b.x,
                        measurement.point_b.y,
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"保存测量失败: {exc}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"保存测量失败: {exc}") from exc

    def delete_project(self, name: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            return cur.rowcount > 0

    def apply_correction(
        self, name: str, expected_version: int, entries: Sequence[tuple[str, Rect]]
    ) -> tuple[bool, str]:
        """Replace/insert sheets in one transaction; measurements are untouched."""

        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT id, version FROM projects WHERE name = ?", (name,)
                ).fetchone()
                if row is None:
                    return False, "项目不存在"
                if row["version"] != expected_version:
                    return (
                        False,
                        (
                            f"项目已更新（页面 v{expected_version}，当前 v{row['version']}），"
                            + "请刷新后重试"
                        ),
                    )
                project_id = row["id"]
                for no, rect in entries:
                    existing = conn.execute(
                        "SELECT id FROM sheets WHERE project_id = ? AND no = ?",
                        (project_id, no),
                    ).fetchone()
                    if existing is not None:
                        conn.execute(
                            "UPDATE sheets SET x0 = ?, y0 = ?, x1 = ?, y1 = ? WHERE id = ?",
                            (*rect.as_tuple(), existing["id"]),
                        )
                    else:
                        position = conn.execute(
                            "SELECT COALESCE(MAX(position), -1) + 1 FROM sheets "
                            "WHERE project_id = ?",
                            (project_id,),
                        ).fetchone()[0]
                        conn.execute(
                            "INSERT INTO sheets (project_id, no, x0, y0, x1, y1, position) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (project_id, no, *rect.as_tuple(), position),
                        )
                now = _now()
                conn.execute(
                    "UPDATE projects SET version = version + 1, updated_at = ? WHERE id = ?",
                    (now, project_id),
                )
                conn.execute(
                    "INSERT INTO correction_log "
                    "(project_id, created_at, status, reason, batch, version_before, version_after) "
                    "VALUES (?, ?, 'applied', ?, ?, ?, ?)",
                    (
                        project_id,
                        now,
                        "图幅更正已提交，测量原坐标保持不变",
                        json.dumps(
                            [{"no": no, "rect": list(rect.as_tuple())} for no, rect in entries],
                            ensure_ascii=False,
                        ),
                        expected_version,
                        expected_version + 1,
                    ),
                )
            return True, "更正已提交"
        except sqlite3.Error as exc:
            raise StorageError(f"写入失败，已保留原状: {exc}") from exc
