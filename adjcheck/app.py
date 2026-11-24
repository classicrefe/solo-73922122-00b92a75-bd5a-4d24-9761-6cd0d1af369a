"""Flask application factory for the adjacency (接幅) checker."""

from __future__ import annotations

import os
from dataclasses import asdict

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from .db import Database, StorageError
from .geometry import Rect
from .models import Issue, Stats
from . import service


def _stats_dict(stats: Stats) -> dict:
    data = asdict(stats)
    data["problem_area"] = stats.problem_area
    return data


def _issue_dict(issue: Issue, extent: Rect) -> dict:
    return {
        "id": issue.id,
        "kind": issue.kind,
        "rect": list(issue.rect.as_tuple()),
        "area": issue.rect.area,
        "sheet_ids": list(issue.sheet_ids),
        "svg": service.svg_rect(issue.rect, extent),
    }


def _plan_dict(plan, extent: Rect) -> dict:
    return {
        "status": plan.status,
        "accepted": plan.accepted,
        "reasons": plan.reasons,
        "window": [list(r.as_tuple()) for r in plan.window],
        "window_svg": [service.svg_rect(r, extent) for r in plan.window],
        "before": _stats_dict(plan.before),
        "after": _stats_dict(plan.after),
        "eliminated": _stats_dict(plan.eliminated),
        "new_problems": _stats_dict(plan.new_problems),
        "batch": [
            {"no": e.sheet_no, "rect": list(e.rect.as_tuple())} for e in plan.batch
        ],
        "version": plan.expected_version,
    }


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
        except Exception:  # demo seeding must not take the site down
            app.logger.exception("示例项目初始化失败")

    # ------------------------------------------------------------- catalog

    @app.get("/")
    def index():
        catalog = db.list_catalog()
        return render_template("index.html", catalog=catalog)

    @app.post("/projects")
    def create():
        name = (request.form.get("name") or "").strip()
        try:
            extent = service.parse_extent(
                request.form.get("x0", ""),
                request.form.get("y0", ""),
                request.form.get("x1", ""),
                request.form.get("y1", ""),
            )
            entries = service.parse_sheet_lines(
                request.form.get("sheets", ""),
                max_entries=service.MAX_SHEETS,
                context="图幅清单",
            )
            created = service.create_project(db, name, extent, entries)
        except service.ValidationError as exc:
            for reason in exc.reasons:
                flash(reason, "error")
            return redirect(url_for("index"))
        except StorageError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if not created:
            flash(f"项目 {name!r} 已存在", "error")
            return redirect(url_for("index"))
        flash(f"项目 {name} 已创建并完成初始核查", "ok")
        return redirect(url_for("project_detail", name=name))

    @app.post("/projects/<name>/delete")
    def delete(name: str):
        if db.delete_project(name):
            flash(f"项目 {name} 已删除", "ok")
        else:
            flash("项目不存在", "error")
        return redirect(url_for("index"))

    # -------------------------------------------------------------- detail

    @app.get("/projects/<name>")
    def project_detail(name: str):
        state = db.get_state(name)
        if state is None:
            abort(404)
        project, sheets, issues = state
        missing = [i for i in issues if i.kind == "missing"]
        overlap = [i for i in issues if i.kind == "overlap"]
        stats = Stats.from_issues(issues)
        log = db.get_log(name)
        return render_template(
            "project.html",
            project=project,
            sheets=sheets,
            issues=issues,
            missing=missing,
            overlap=overlap,
            stats=stats,
            log=log,
            svg=service.svg_geometry(project.extent),
            sheet_boxes=[
                {"no": s.no, "svg": service.svg_rect(s.rect, project.extent), "rect": s.rect}
                for s in sheets
            ],
            issue_boxes=[
                {"kind": i.kind, "sheet_ids": i.sheet_ids, "svg": service.svg_rect(i.rect, project.extent)}
                for i in issues
            ],
            demo_batch=(service.DEMO_BATCH_TEXT if name == service.DEMO_NAME else ""),
            demo_reject=(service.DEMO_REJECT_TEXT if name == service.DEMO_NAME else ""),
            demo_notes=(service.DEMO_NOTES if name == service.DEMO_NAME else []),
        )

    # ----------------------------------------------------------- check/apply

    def _load(name: str):
        state = db.get_state(name)
        if state is None:
            return None
        return state

    def _plan_from_request(project, sheets, issues, payload):
        batch = service.parse_batch_json(payload.get("batch"))
        expected = payload.get("version")
        try:
            expected_version = int(expected) if expected not in (None, "") else None
        except (TypeError, ValueError):
            raise service.ValidationError(["version 必须是整数"])
        return service.build_plan(
            project, sheets, issues, batch, expected_version=expected_version
        )

    @app.post("/api/projects/<name>/check")
    def api_check(name: str):
        state = _load(name)
        if state is None:
            return jsonify({"error": "项目不存在"}), 404
        project, sheets, issues = state
        payload = request.get_json(silent=True) or {}
        try:
            plan = _plan_from_request(project, sheets, issues, payload)
        except service.ValidationError as exc:
            return jsonify({"error": "；".join(exc.reasons), "reasons": exc.reasons}), 400
        return jsonify(_plan_dict(plan, project.extent))

    @app.post("/api/projects/<name>/apply")
    def api_apply(name: str):
        state = _load(name)
        if state is None:
            return jsonify({"error": "项目不存在"}), 404
        project, sheets, issues = state
        payload = request.get_json(silent=True) or {}
        try:
            plan = _plan_from_request(project, sheets, issues, payload)
        except service.ValidationError as exc:
            return jsonify({"error": "；".join(exc.reasons), "reasons": exc.reasons}), 400

        if plan.status == "stale":
            return (
                jsonify(
                    {
                        "applied": False,
                        "message": "页面数据已过期，未做任何修改，请刷新后重试",
                        "reasons": plan.reasons,
                        "plan": _plan_dict(plan, project.extent),
                    }
                ),
                409,
            )

        if not plan.accepted:
            log_id = db.log_rejection(
                name,
                plan.expected_version if plan.expected_version is not None else project.version,
                plan,
            )
            return (
                jsonify(
                    {
                        "applied": False,
                        "message": "更正被退回，目录与核查结果保持原状",
                        "log_id": log_id,
                        "plan": _plan_dict(plan, project.extent),
                    }
                ),
                200,
            )

        try:
            ok, message = service.commit_plan(db, name, plan)
        except StorageError as exc:
            return (
                jsonify(
                    {
                        "applied": False,
                        "message": f"写入失败，已回滚并保留原状：{exc}",
                        "plan": _plan_dict(plan, project.extent),
                    }
                ),
                500,
            )
        if not ok:
            return (
                jsonify(
                    {
                        "applied": False,
                        "message": message,
                        "plan": _plan_dict(plan, project.extent),
                    }
                ),
                409,
            )
        return jsonify(
            {
                "applied": True,
                "message": message,
                "plan": _plan_dict(plan, project.extent),
                "state_url": url_for("project_detail", name=name),
            }
        )

    # server-side fallback used by the detail page when JS is unavailable
    @app.post("/projects/<name>/correct")
    def correct_form(name: str):
        state = _load(name)
        if state is None:
            abort(404)
        project, sheets, issues = state
        action = request.form.get("action", "check")
        batch_text = request.form.get("batch", "")
        expected_raw = request.form.get("version", "").strip()
        try:
            expected_version = int(expected_raw) if expected_raw else None
        except ValueError:
            flash("页面版本号无效，请刷新后重试", "error")
            return redirect(url_for("project_detail", name=name))
        try:
            batch = service.parse_sheet_lines(
                batch_text, max_entries=service.MAX_BATCH, context="本批更正"
            )
            plan = service.build_plan(
                project,
                sheets,
                issues,
                batch,
                expected_version=expected_version,
            )
        except service.ValidationError as exc:
            for reason in exc.reasons:
                flash(reason, "error")
            return redirect(url_for("project_detail", name=name))

        if action == "apply":
            if plan.status == "stale":
                for reason in plan.reasons:
                    flash(reason, "error")
            elif not plan.accepted:
                db.log_rejection(
                    name,
                    plan.expected_version if plan.expected_version is not None else project.version,
                    plan,
                )
                for reason in plan.reasons:
                    flash(f"退回：{reason}", "error")
            else:
                try:
                    ok, message = service.commit_plan(db, name, plan)
                except StorageError as exc:
                    flash(f"写入失败，已保留原状：{exc}", "error")
                else:
                    if ok:
                        flash(
                            "更正已写入：消除缺片 "
                            f"{plan.eliminated.missing_area} m²、重叠 {plan.eliminated.overlap_area} m²",
                            "ok",
                        )
                    else:
                        flash(message, "error")
            return redirect(url_for("project_detail", name=name))

        label = "通过" if plan.accepted else ("已过期" if plan.status == "stale" else "退回")
        for reason in plan.reasons:
            flash(f"预检{label}：{reason}", "ok" if plan.accepted else "error")
        flash(
            "受影响重算范围：" + ("；".join(str(r) for r in plan.window) or "无"),
            "info",
        )
        return redirect(url_for("project_detail", name=name))

    @app.get("/api/projects/<name>/state")
    def api_state(name: str):
        state = _load(name)
        if state is None:
            return jsonify({"error": "项目不存在"}), 404
        project, sheets, issues = state
        return jsonify(
            {
                "project": {
                    "name": project.name,
                    "version": project.version,
                    "extent": list(project.extent.as_tuple()),
                },
                "sheets": [
                    {"no": s.no, "rect": list(s.rect.as_tuple())} for s in sheets
                ],
                "issues": [_issue_dict(i, project.extent) for i in issues],
                "stats": _stats_dict(Stats.from_issues(issues)),
            }
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app
