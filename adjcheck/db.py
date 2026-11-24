"""SQLite persistence layer.

The catalog (``sheets``), the verification result (``issues``) and the audit
trail (``correction_log``) live in one database.  Applying a correction is a
single transaction that also performs an optimistic-lock check on the project
version, so a stale page cannot overwrite a newer catalog.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Sequence

from .geometry import Rect, Region
from .models import CorrectionPlan, Issue, Project, Sheet


class StorageError(RuntimeError):
    """Raised when a write cannot be completed; the transaction is rolled back."""


@dataclass(frozen=True)
class CatalogProject:
    project: Project
    sheet_count: int
    issue_count: int
    problem_area: int


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    x1          INTEGER NOT NULL,
    y1          INTEGER NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (x0 < x1 AND y0 < y1)
);

CREATE TABLE IF NOT EXISTS sheets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    no          TEXT    NOT NULL,
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    x1          INTEGER NOT NULL,
    y1          INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    UNIQUE (project_id, no),
    CHECK (x0 < x1 AND y0 < y1)
);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL CHECK (kind IN ('missing', 'overlap')),
    x0          INTEGER NOT NULL,
    y0          INTEGER NOT NULL,
    x1          INTEGER NOT NULL,
    y1          INTEGER NOT NULL,
    sheet_ids   TEXT    NOT NULL DEFAULT '[]',
    CHECK (x0 < x1 AND y0 < y1),
    CHECK (kind <> 'overlap' OR json_array_length(sheet_ids) >= 2),
    CHECK (kind <> 'missing' OR json_array_length(sheet_ids) = 0)
);

CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id);
CREATE INDEX IF NOT EXISTS idx_sheets_project ON sheets(project_id);

CREATE TABLE IF NOT EXISTS correction_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    batch        TEXT    NOT NULL,
    window       TEXT    NOT NULL,
    before_stats TEXT    NOT NULL,
    after_stats  TEXT    NOT NULL,
    version_before INTEGER NOT NULL,
    version_after  INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rect_from_row(row: sqlite3.Row) -> Rect:
    return Rect(row["x0"], row["y0"], row["x1"], row["y1"])


def _row_issue(row: sqlite3.Row) -> Issue:
    ids = tuple(json.loads(row["sheet_ids"]))
    return Issue(kind=row["kind"], rect=_rect_from_row(row), sheet_ids=ids, id=row["id"])


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
            extent=_rect_from_row(row),
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
                       (SELECT COUNT(*) FROM issues i WHERE i.project_id = p.id) AS issue_count,
                       (SELECT COALESCE(SUM((i.x1-i.x0)*(i.y1-i.y0)), 0)
                          FROM issues i WHERE i.project_id = p.id) AS problem_area
                  FROM projects p
                 ORDER BY p.name
                """
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                CatalogProject(
                    project=self._project_from_row(row),
                    sheet_count=row["sheet_count"],
                    issue_count=row["issue_count"],
                    problem_area=row["problem_area"],
                )
            )
        return result

    def get_sheets(self, name: str) -> tuple[Project, list[Sheet]] | None:
        with self.connection() as conn:
            project, sheets = self._get_sheets(conn, name)
        return None if project is None else (project, sheets)

    def _get_sheets(
        self, conn: sqlite3.Connection, name: str
    ) -> tuple[Project | None, list[Sheet]]:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None, []
        project = self._project_from_row(row)
        sheet_rows = conn.execute(
            "SELECT no, x0, y0, x1, y1 FROM sheets "
            "WHERE project_id = ? ORDER BY position, no",
            (row["id"],),
        ).fetchall()
        return project, [Sheet(no=r["no"], rect=_rect_from_row(r)) for r in sheet_rows]

    def get_state(self, name: str) -> tuple[Project, list[Sheet], list[Issue]] | None:
        with self.connection() as conn:
            project, sheets = self._get_sheets(conn, name)
            if project is None:
                return None
            issues = self._get_issues(conn, name)
        return project, sheets, issues

    def _get_issues(self, conn: sqlite3.Connection, name: str) -> list[Issue]:
        rows = conn.execute(
            """
            SELECT i.* FROM issues i JOIN projects p ON p.id = i.project_id
             WHERE p.name = ?
             ORDER BY i.kind, i.x0, i.y0, i.x1, i.y1, i.id
            """,
            (name,),
        ).fetchall()
        return [_row_issue(row) for row in rows]

    def get_issues(self, name: str) -> list[Issue]:
        with self.connection() as conn:
            return self._get_issues(conn, name)

    def get_log(self, name: str, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT l.* FROM correction_log l JOIN projects p ON p.id = l.project_id
                 WHERE p.name = ? ORDER BY l.id DESC LIMIT ?
                """,
                (name, limit),
            ).fetchall()
        entries = []
        for row in rows:
            entries.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                    "reason": row["reason"],
                    "batch": json.loads(row["batch"]),
                    "window": [Rect(*w) for w in json.loads(row["window"])],
                    "before": json.loads(row["before_stats"]),
                    "after": json.loads(row["after_stats"]),
                    "version_before": row["version_before"],
                    "version_after": row["version_after"],
                }
            )
        return entries

    # --------------------------------------------------------------- writes

    def create_project(
        self,
        name: str,
        extent: Rect,
        sheets: Sequence[Sheet],
        issues: Sequence[Issue | Region],
    ) -> bool:
        """Create a project atomically.  Returns False when the name exists."""

        try:
            with self._transaction() as conn:
                if conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                    return False
                now = _now()
                cur = conn.execute(
                    "INSERT INTO projects (name, x0, y0, x1, y1, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (name, *extent.as_tuple(), now, now),
                )
                project_id = cur.lastrowid
                for index, sheet in enumerate(sheets):
                    conn.execute(
                        "INSERT INTO sheets (project_id, no, x0, y0, x1, y1, position) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (project_id, sheet.no, *sheet.rect.as_tuple(), index),
                    )
                for issue in issues:
                    self._insert_issue(conn, project_id, issue)
            return True
        except sqlite3.Error as exc:
            raise StorageError(f"写入初始数据失败: {exc}") from exc

    @staticmethod
    def _insert_issue(
        conn: sqlite3.Connection, project_id: int, issue: Issue | Region
    ) -> int:
        cur = conn.execute(
            "INSERT INTO issues (project_id, kind, x0, y0, x1, y1, sheet_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                issue.kind,
                *issue.rect.as_tuple(),
                json.dumps(list(issue.sheet_ids)),
            ),
        )
        return cur.lastrowid

    def delete_project(self, name: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            return cur.rowcount > 0

    def apply_correction(
        self,
        name: str,
        expected_version: int,
        plan: CorrectionPlan,
    ) -> tuple[bool, str]:
        """Replace the batch sheets and the verification result atomically.

        ``plan.split`` determines which stored issue rows are reused verbatim
        and which are replaced by freshly computed regions.  Any failure rolls
        everything back, so the caller can report the cause and keep the old
        state.
        """

        split = plan.split
        if split is None:
            raise StorageError("内部错误: 缺少增量重算结果")

        if not plan.accepted:
            raise StorageError("内部错误: 拒绝的更正不能写入")

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
                        f"项目已被更新（期望版本 {expected_version}，当前版本 {row['version']}），请刷新后重试",
                    )
                project_id = row["id"]

                # 1. catalog: replace the batch sheets in place, keep everything
                #    else (including its stored order and row id).
                for entry in plan.batch:
                    existing = conn.execute(
                        "SELECT id FROM sheets WHERE project_id = ? AND no = ?",
                        (project_id, entry.sheet_no),
                    ).fetchone()
                    if existing is not None:
                        conn.execute(
                            "UPDATE sheets SET x0 = ?, y0 = ?, x1 = ?, y1 = ? WHERE id = ?",
                            (*entry.rect.as_tuple(), existing["id"]),
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
                            (
                                project_id,
                                entry.sheet_no,
                                *entry.rect.as_tuple(),
                                position,
                            ),
                        )

                # 2. verification result: drop only the replaced window rows,
                #    keep every row outside it byte-for-byte.
                replaced_ids = [issue.id for issue in split.replaced if issue.id is not None]
                if replaced_ids:
                    conn.executemany(
                        "DELETE FROM issues WHERE id = ? AND project_id = ?",
                        [(issue_id, project_id) for issue_id in replaced_ids],
                    )
                untouched_by_id = {
                    issue.id: issue for issue in split.untouched if issue.id is not None
                }
                expected_keep = set(untouched_by_id)
                actual_keep = {
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM issues WHERE project_id = ?", (project_id,)
                    ).fetchall()
                }
                if actual_keep != expected_keep:
                    raise StorageError(
                        "核查结果已被并发修改，无法安全合并增量结果，请刷新后重试"
                    )

                for region in split.in_window:
                    self._insert_issue(conn, project_id, region)

                # verify the persisted state before committing
                stored = conn.execute(
                    "SELECT COUNT(*) FROM issues WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                if stored != len(split.untouched) + len(split.in_window):
                    raise StorageError("核查结果写入数量不一致，已回滚")

                now = _now()
                conn.execute(
                    "UPDATE projects SET version = version + 1, updated_at = ? WHERE id = ?",
                    (now, project_id),
                )
                conn.execute(
                    "INSERT INTO correction_log "
                    "(project_id, created_at, status, reason, batch, window, before_stats, "
                    " after_stats, version_before, version_after) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        now,
                        "accepted",
                        "；".join(plan.reasons) or "通过",
                        json.dumps(
                            [
                                {"no": e.sheet_no, "rect": list(e.rect.as_tuple())}
                                for e in plan.batch
                            ],
                            ensure_ascii=False,
                        ),
                        json.dumps([list(r.as_tuple()) for r in plan.window]),
                        json.dumps(plan.before.__dict__, ensure_ascii=False),
                        json.dumps(plan.after.__dict__, ensure_ascii=False),
                        expected_version,
                        expected_version + 1,
                    ),
                )
            return True, "更正已提交"
        except (sqlite3.Error, StorageError) as exc:
            raise StorageError(f"写入失败，已保留原状: {exc}") from exc

    def log_rejection(
        self,
        name: str,
        expected_version: int,
        plan: CorrectionPlan,
    ) -> int | None:
        """Best-effort audit entry for a rejected attempt (never fatal)."""

        try:
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT id, version FROM projects WHERE name = ?", (name,)
                ).fetchone()
                if row is None:
                    return None
                cur = conn.execute(
                    "INSERT INTO correction_log "
                    "(project_id, created_at, status, reason, batch, window, before_stats, "
                    " after_stats, version_before, version_after) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        row["id"],
                        _now(),
                        plan.status,
                        "；".join(plan.reasons) or plan.status,
                        json.dumps(
                            [
                                {"no": e.sheet_no, "rect": list(e.rect.as_tuple())}
                                for e in plan.batch
                            ],
                            ensure_ascii=False,
                        ),
                        json.dumps([list(r.as_tuple()) for r in plan.window]),
                        json.dumps(plan.before.__dict__, ensure_ascii=False),
                        json.dumps(plan.after.__dict__, ensure_ascii=False),
                        expected_version,
                    ),
                )
                return cur.lastrowid
        except sqlite3.Error:
            return None
