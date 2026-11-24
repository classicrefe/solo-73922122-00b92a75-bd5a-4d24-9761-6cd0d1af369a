"""Flask 应用：项目目录、测量录入、双路径核查与图幅更正。"""

from __future__ import annotations

import os

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import service
from .db import Database
from .errors import StorageError, ValidationError
from .geometry import Rect
from .models import MeasurementValidity, Snapshot, Verification


def _validity_dict(item: MeasurementValidity) -> dict:
    measurement = item.measurement
    return {
        "no": measurement.no,
        "sheet_a": measurement.sheet_a,
        "point_a": list(measurement.point_a.as_tuple()),
        "sheet_b": measurement.sheet_b,
        "point_b": list(measurement.point_b.as_tuple()),
        "valid": item.valid,
        "reasons": list(item.reasons),
    }


def _verification_dict(result: Verification) -> dict:
    return {
        "status": result.status,
        "status_label": result.status_label,
        "message": result.message,
        "reference": result.reference,
        "target": result.target,
        "version": result.version,
        "translation": list(result.translation) if result.translation else None,
        "translation_x": result.translation_x,
        "translation_y": result.translation_y,
        "max_paths": result.max_paths,
        "conflicts": list(result.conflicts),
        "measurements": [_validity_dict(item) for item in result.validity],
        "invalid": [_validity_dict(item) for item in result.invalid_measurements],
        "paths": [
            {
                "measurements": list(path.measurements),
                "sheets": list(path.sheets),
                "translation": list(path.translation),
            }
            for path in result.paths
        ],
    }


def _snapshot_dict(snapshot: Snapshot) -> dict:
    return {
        "project": {
            "name": snapshot.project.name,
            "feature": snapshot.project.feature,
            "version": snapshot.project.version,
            "created_at": snapshot.project.created_at,
            "updated_at": snapshot.project.updated_at,
        },
        "sheets": [
            {"no": sheet.no, "rect": list(sheet.rect.as_tuple())} for sheet in snapshot.sheets
        ],
        "measurements": [
            {
                "no": item.no,
                "sheet_a": item.sheet_a,
                "point_a": list(item.point_a.as_tuple()),
                "sheet_b": item.sheet_b,
                "point_b": list(item.point_b.as_tuple()),
            }
            for item in snapshot.measurements
        ],
    }


def _json_error(reasons) -> tuple[dict, int]:
    if isinstance(reasons, str):
        reasons = [reasons]
    return {"error": "；".join(reasons), "reasons": list(reasons)}, 400


def create_app(*, database_path: str | None = None, seed_demo: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("ADJCHECK_SECRET", "adjcheck-dev-secret"),
        DATABASE=database_path or os.environ.get("ADJCHECK_DB", "data/adjcheck.sqlite3"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    db = Database(app.config["DATABASE"])
    db.init_schema()
    app.extensions["adjcheck_db"] = db
    if seed_demo:
        try:
            service.ensure_demo(db)
        except Exception:  # 示例数据失败不应阻止站点启动
            app.logger.exception("示例项目初始化失败")

    # ------------------------------------------------------------- catalog

    @app.get("/")
    def index():
        return render_template("index.html", catalog=db.list_catalog())

    @app.post("/projects")
    def create():
        name = (request.form.get("name") or "").strip()
        try:
            name = service.parse_project_name(name)
            feature = service.parse_feature(request.form.get("feature", ""))
            entries = service.parse_sheet_lines(
                request.form.get("sheets", ""),
                max_entries=service.MAX_SHEETS,
                context="图幅清单",
            )
            created = service.create_project(db, name, feature, entries)
        except ValidationError as exc:
            for reason in exc.reasons:
                flash(reason, "error")
            return redirect(url_for("index"))
        except StorageError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if not created:
            flash(f"项目 {name!r} 已存在", "error")
            return redirect(url_for("index"))
        flash(f"项目 {name} 已创建", "ok")
        return redirect(url_for("project_detail", name=name))

    @app.post("/projects/<name>/delete")
    def delete(name: str):
        if db.delete_project(name):
            flash(f"项目 {name} 已删除", "ok")
        else:
            flash("项目不存在", "error")
        return redirect(url_for("index"))

    # -------------------------------------------------------------- detail

    def _load(name: str) -> Snapshot | None:
        return db.get_snapshot(name)

    def _render_project(
        snapshot: Snapshot,
        *,
        reference: str = "",
        target: str = "",
        result: Verification | None = None,
    ):
        validity = {
            item.measurement.no: item
            for item in service.valid_edges(snapshot)[1]
        }
        return render_template(
            "project.html",
            project=snapshot.project,
            sheets=list(snapshot.sheets),
            measurements=list(snapshot.measurements),
            validity=validity,
            reference=reference,
            target=target,
            result=result,
            log=db.get_log(snapshot.project.name),
            demo_notes=(
                service.DEMO_NOTES if snapshot.project.name == service.DEMO_NAME else []
            ),
            demo_correction=(
                service.DEMO_CORRECTION_TEXT
                if snapshot.project.name == service.DEMO_NAME
                else ""
            ),
        )

    @app.get("/projects/<name>")
    def project_detail(name: str):
        snapshot = _load(name)
        if snapshot is None:
            abort(404)
        reference = (request.args.get("reference") or "").strip()
        target = (request.args.get("target") or "").strip()
        result = None
        if reference or target:
            try:
                reference, target = service.parse_reference_pair(
                    {"reference": reference, "target": target}, snapshot.sheet_map
                )
                result = service.compute_verification(snapshot, reference, target)
            except ValidationError as exc:
                for reason in exc.reasons:
                    flash(reason, "error")
        return _render_project(snapshot, reference=reference, target=target, result=result)

    # ---------------------------------------------------------- measurements

    def _add_measurement_or_error(name: str, form: dict) -> tuple[bool, list[str]]:
        snapshot = _load(name)
        if snapshot is None:
            return False, ["项目不存在"]
        try:
            measurement = service.parse_measurement_form(form, snapshot.sheet_map)
            service.add_measurement(db, name, measurement)
        except ValidationError as exc:
            return False, exc.reasons
        except StorageError as exc:
            return False, [str(exc)]
        return True, []

    @app.post("/projects/<name>/measurements")
    def add_measurement_form(name: str):
        ok, reasons = _add_measurement_or_error(name, request.form.to_dict())
        if ok:
            flash("测量已保存（原坐标留存，核查时按现行图幅重判有效性）", "ok")
        else:
            for reason in reasons:
                flash(reason, "error")
        return redirect(url_for("project_detail", name=name))

    # ---------------------------------------------------------------- APIs

    @app.post("/api/projects/<name>/verify")
    def api_verify(name: str):
        snapshot = _load(name)
        if snapshot is None:
            return jsonify({"error": "项目不存在"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            reference, target = service.parse_reference_pair(payload, snapshot.sheet_map)
        except ValidationError as exc:
            return _json_error(exc.reasons)
        result = service.compute_verification(snapshot, reference, target)
        return jsonify({"verification": _verification_dict(result)})

    @app.post("/api/projects/<name>/measurements")
    def api_add_measurement(name: str):
        snapshot = _load(name)
        if snapshot is None:
            return jsonify({"error": "项目不存在"}), 404
        payload = request.get_json(silent=True) or {}
        ok, reasons = _add_measurement_or_error(name, payload)
        if not ok:
            return _json_error(reasons)
        return jsonify({"added": True}), 201

    @app.get("/api/projects/<name>/state")
    def api_state(name: str):
        snapshot = _load(name)
        if snapshot is None:
            return jsonify({"error": "项目不存在"}), 404
        data = _snapshot_dict(snapshot)
        data["validity"] = [
            _validity_dict(service.measurement_validity(item, snapshot.sheet_map))
            for item in snapshot.measurements
        ]
        return jsonify(data)

    @app.post("/api/projects/<name>/correct")
    def api_correct(name: str):
        snapshot = _load(name)
        if snapshot is None:
            return jsonify({"error": "项目不存在"}), 404
        payload = request.get_json(silent=True) or {}
        try:
            expected_version = int(payload.get("version"))
        except (TypeError, ValueError):
            return _json_error(["version 必须是整数"])
        try:
            entries = _parse_correction(payload)
        except ValidationError as exc:
            return _json_error(exc.reasons)
        try:
            ok, message = service.apply_correction(db, name, entries, expected_version)
        except ValidationError as exc:
            return _json_error(exc.reasons)
        except StorageError as exc:
            return jsonify({"applied": False, "message": str(exc)}), 500
        if not ok:
            return jsonify({"applied": False, "message": message}), 409
        return jsonify({"applied": True, "message": message}), 200

    @app.post("/projects/<name>/correct")
    def correct_form(name: str):
        snapshot = _load(name)
        if snapshot is None:
            abort(404)
        reference = (request.form.get("reference") or "").strip()
        target = (request.form.get("target") or "").strip()
        back = url_for("project_detail", name=name)
        if reference and target:
            back = url_for("project_detail", name=name, reference=reference, target=target)
        try:
            entries = service.parse_sheet_lines(
                request.form.get("sheets", ""),
                max_entries=service.MAX_CORRECTION_SHEETS,
                context="本批更正",
            )
            expected_version = int((request.form.get("version") or "").strip())
        except ValidationError as exc:
            for reason in exc.reasons:
                flash(reason, "error")
            return redirect(back)
        except ValueError:
            flash("页面版本号无效，请刷新后重试", "error")
            return redirect(back)
        try:
            ok, message = service.apply_correction(db, name, entries, expected_version)
        except ValidationError as exc:
            for reason in exc.reasons:
                flash(reason, "error")
        except StorageError as exc:
            flash(f"写入失败，已保留原状：{exc}", "error")
        else:
            flash(message, "ok" if ok else "error")
        return redirect(back)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


def _parse_correction(payload: dict) -> list[tuple[str, Rect]]:
    """接受 JSON 的 {"sheets": [[图幅号, x0, y0, x1, y1], ...]} 或文本行。"""

    if "sheets" in payload and isinstance(payload["sheets"], list):
        entries: list[tuple[str, Rect]] = []
        seen: set[str] = set()
        reasons: list[str] = []
        for index, item in enumerate(payload["sheets"], start=1):
            try:
                no, x0, y0, x1, y1 = item
                no = str(no)
                if not service.SHEET_NO_RE.match(no):
                    raise ValueError("图幅号非法")
                rect = Rect(int(x0), int(y0), int(x1), int(y1))
            except (TypeError, ValueError):
                reasons.append(f"第 {index} 项应为 [图幅号, x0, y0, x1, y1] 且坐标为整数")
                continue
            if no in seen:
                reasons.append(f"图幅号重复: {no}")
                continue
            seen.add(no)
            entries.append((no, rect))
        if reasons:
            raise ValidationError(reasons)
        return entries
    text = str(payload.get("batch") or "")
    return service.parse_sheet_lines(
        text, max_entries=service.MAX_CORRECTION_SHEETS, context="本批更正"
    )
