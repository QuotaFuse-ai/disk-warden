# -*- coding: utf-8 -*-
"""Fixture data returned by the fake bridge.

Every value here is pure JSON (numbers, strings, lists, dicts).  The fake
bridge serialises this module's output into the page, so keep it free of
callables and non-UTF-8 surprises.

Conventions
-----------
* Sizes are byte counts (ints); the UI formats them itself.
* ``reason`` values are i18n keys the UI already knows (``reason.extCache``),
  so a fixture renders correctly in both zh-CN and en.
* Timestamps are fixed ISO-8601 strings so screenshots stay byte-stable.
* Paths are Windows-shaped on purpose: that is what the product ships.
* Edge samples live here too (>260-char path, over-long tool name, empty
  lists, permission errors, process-held tool, migrated / rolled-back state).
"""

GB = 1024 ** 3
MB = 1024 ** 2

HOME = "C:\\Users\\dev"

# A path long enough to blow past the 260-char Win32 limit.  Kept as a real
# looking build cache path so reviewers can eyeball truncation behaviour.
_LONG_PATH = (
    HOME
    + "\\AppData\\Local\\Temp\\trae-build-cache\\2026-10-18T09-14-22.113Z"
    + "\\workspace-a1b2c3d4e5f6a7b8\\packages\\renderer\\node_modules\\.cache"
    + "\\webpack"
    + "".join("\\chunk-%02d-9f2c7b1e.deps" % i for i in range(1, 10))
)


def _item(item_id, tool_id, tool_name, rel_path, source_path, size, safety,
          reason, rule_id="", can_migrate=None, requires_confirm=False,
          blocked_code="", blocked_reason="", migrated_job_id="",
          target_path="", file_count=0, access_errors=0, is_reparse=False,
          target_exists=False):
    if can_migrate is None:
        can_migrate = safety != "red" and not migrated_job_id
    return {
        "item_id": item_id,
        "tool_id": tool_id,
        "tool_name": tool_name,
        "kind": "dir",
        "rel_path": rel_path,
        "source_path": source_path,
        "target_path": target_path,
        "bytes": size,
        "safety": safety,
        "reason": reason,
        "rule_id": rule_id,
        "can_migrate": bool(can_migrate),
        "requires_confirm": bool(requires_confirm),
        "reason_key": "",
        "blocked_code": blocked_code,
        "blocked_reason": blocked_reason,
        "migrated_job_id": migrated_job_id,
        "file_count": file_count,
        "access_errors": access_errors,
        "is_reparse": is_reparse,
        "mtime_max": "2026-10-20T08:40:00Z",
        "target_exists": target_exists,
        "process": None,
        "source": "scan",
    }


def _tool(tool_id, name, kind, processes, present):
    return {
        "id": tool_id,
        "name": name,
        "kind": kind,
        "counts": {"present": present},
        "runtime": {
            "catalog_names": [p["name"] for p in processes],
            "processes": processes,
        },
    }


def _tools_and_items():
    tools = [
        _tool("cursor", "Cursor", "ide",
              [{"name": "Cursor.exe", "pid": 8812, "running": False}], present=12),
        _tool("windsurf", "Windsurf", "ide",
              [{"name": "Windsurf.exe", "pid": 14208, "running": True},
               {"name": "language_server.exe", "pid": 14390, "running": True}], present=9),
        _tool("vscode", "VS Code", "ide", [], present=7),
        _tool("trae", "Trae CN Enterprise Edition — Internal Preview (2026.10.8841)",
              "ide", [], present=6),
    ]

    items = [
        # --- Cursor: mixed green / yellow / red, one already migrated -----
        _item("cursor-ext", "cursor", "Cursor", "extensions",
              HOME + "\\.cursor\\extensions", int(2.6 * GB), "green",
              "reason.extCache", "cursor.extensions", file_count=1842),
        _item("cursor-cache", "cursor", "Cursor", "AppData\\Roaming\\Cursor\\Cache",
              HOME + "\\AppData\\Roaming\\Cursor\\Cache", int(1.4 * GB), "green",
              "reason.chromeCache", "cursor.browserCache", file_count=5310),
        _item("cursor-codecache", "cursor", "Cursor", "AppData\\Roaming\\Cursor\\Code Cache",
              HOME + "\\AppData\\Roaming\\Cursor\\Code Cache", int(512 * MB), "green",
              "reason.codeCache", "cursor.codeCache", file_count=904),
        _item("cursor-logs", "cursor", "Cursor", "AppData\\Roaming\\Cursor\\logs",
              HOME + "\\AppData\\Roaming\\Cursor\\logs", int(96 * MB), "yellow",
              "reason.logs", "", requires_confirm=True, file_count=41),
        _item("cursor-user", "cursor", "Cursor", "AppData\\Roaming\\Cursor\\User\\globalStorage",
              HOME + "\\AppData\\Roaming\\Cursor\\User\\globalStorage", int(780 * MB), "red",
              "reason.settings", "", blocked_code="PROTECTED_DATA", file_count=612),
        _item("cursor-projects", "cursor", "Cursor", ".cursor\\projects",
              HOME + "\\.cursor\\projects", int(1.1 * GB), "green",
              "reason.workspace", "cursor.workspace", migrated_job_id="jb_20261012_01",
              target_path="D:\\DiskWarden\\cursor\\projects", file_count=288),

        # --- Windsurf: running process + a permission-denied directory ----
        _item("windsurf-ext", "windsurf", "Windsurf", "extensions",
              HOME + "\\.windsurf\\extensions", int(1.8 * GB), "green",
              "reason.extCache", "windsurf.extensions", file_count=1207),
        _item("windsurf-cascade", "windsurf", "Windsurf", "cascade",
              HOME + "\\.codeium\\windsurf\\cascade", int(640 * MB), "green",
              "reason.cascade", "windsurf.cascade", file_count=88),
        _item("windsurf-brain", "windsurf", "Windsurf", "memories",
              HOME + "\\.codeium\\windsurf\\memories", int(120 * MB), "yellow",
              "reason.brain", "", requires_confirm=True, file_count=17),
        _item("windsurf-perm", "windsurf", "Windsurf", "AppData\\Local\\windsurf\\shared",
              HOME + "\\AppData\\Local\\windsurf\\shared", int(18 * MB), "yellow",
              "reason.unknownProfile", "", requires_confirm=True, access_errors=3,
              file_count=4),

        # --- VS Code: fully migrated, so the rollback entry point shows ---
        _item("vscode-ext", "vscode", "VS Code", "extensions",
              HOME + "\\.vscode\\extensions", int(2.1 * GB), "green",
              "reason.extCache", "vscode.extensions", migrated_job_id="jb_20261018_02",
              target_path="D:\\DiskWarden\\vscode\\extensions", file_count=1590),
        _item("vscode-cache", "vscode", "VS Code", "AppData\\Roaming\\Code\\Cache",
              HOME + "\\AppData\\Roaming\\Code\\Cache", int(860 * MB), "green",
              "reason.chromeCache", "vscode.browserCache", migrated_job_id="jb_20261018_02",
              target_path="D:\\DiskWarden\\vscode\\Cache", file_count=2210),

        # --- Trae: over-long tool name + over-long path --------------------
        _item("trae-ext", "trae", "Trae CN", ".trae\\extensions",
              HOME + "\\.trae\\extensions", int(1.2 * GB), "green",
              "reason.extCache", "trae.extensions", file_count=743),
        _item("trae-long", "trae", "Trae CN", "AppData\\Local\\Temp\\trae-build-cache",
              _LONG_PATH, int(2.4 * GB), "yellow", "reason.nodeModules", "",
              requires_confirm=True, file_count=18422),
        _item("trae-user", "trae", "Trae CN", "AppData\\Roaming\\Trae\\User",
              HOME + "\\AppData\\Roaming\\Trae\\User", int(310 * MB), "red",
              "reason.settings", "", blocked_code="PROTECTED_DATA", file_count=96),
    ]
    return tools, items


def _stats(items):
    return {
        "tools": 4,
        "tools_present": 4,
        "items": len(items),
        "bytes": sum(it["bytes"] for it in items),
        "files": sum(it["file_count"] for it in items),
        "green_bytes": sum(it["bytes"] for it in items if it["safety"] == "green"),
        "yellow_bytes": sum(it["bytes"] for it in items if it["safety"] == "yellow"),
        "red_bytes": sum(it["bytes"] for it in items if it["safety"] == "red"),
        "migratable_bytes": sum(
            it["bytes"] for it in items
            if it["safety"] != "red" and it["can_migrate"] and not it["migrated_job_id"]
        ),
        "blocked_items": sum(1 for it in items if it["blocked_code"]),
        "access_errors": sum(it["access_errors"] for it in items),
    }


def snapshot():
    tools, items = _tools_and_items()
    return {
        "scanned_at": "2026-10-20T09:12:00Z",
        "stats": _stats(items),
        "tools": tools,
        "items": items,
        "warnings": [],
    }


def empty_snapshot():
    snap = snapshot()
    snap["tools"] = []
    snap["items"] = []
    snap["stats"] = _stats([])
    snap["stats"].update({"tools": 0, "tools_present": 0})
    snap["scanned_at"] = "2026-10-20T09:20:00Z"
    return snap


def disk_info():
    return {
        "source": {
            "path": "C:\\",
            "available": True,
            "total": int(512 * GB),
            "used": int(438 * GB),
            "free": int(74 * GB),
        },
        "target": {
            "path": "D:\\",
            "available": True,
            "total": int(1024 * GB),
            "used": int(412 * GB),
            "free": int(612 * GB),
        },
    }


def settings():
    return {
        "app_version": "0.9.3",
        "protocol_version": 1,
        "data_root": HOME + "\\AppData\\Local\\DiskWarden",
        "target_root": "D:\\DiskWarden",
        "launch_at_startup": True,
        "launch_at_startup_supported": True,
        "launch_at_startup_error": "",
        "launch_at_startup_command": "\"C:\\Program Files\\Disk Warden\\DiskWarden.exe\" --tray",
        "auto_maintenance": True,
        "uninstall_policy": "rollback",
        "language": "zh-CN",
        "paths": {
            "root": "D:\\DiskWarden",
            "data_dir": "D:\\DiskWarden",
            "logs_dir": HOME + "\\AppData\\Local\\DiskWarden\\logs",
            "jobs_dir": HOME + "\\AppData\\Local\\DiskWarden\\jobs",
        },
        "ai": {
            "enabled": True,
            "available": True,
            "disabled_reason": "",
            "consent": True,
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key_set": True,
            "api_key_source": "stored",
            "api_key_error": "",
            "warning_key": "ai.third_party_warning",
            "warning": "",
        },
        "settings_warnings": [],
    }


def _provider(configured=True):
    return {
        "provider": "github",
        "label": "GitHub",
        "configured": configured,
        "disabled_reason": "" if configured else "missing client id",
        "client_id_env": "DISK_WARDEN_GITHUB_CLIENT_ID",
        "flow": "device_code",
        "device_flow": True,
        "scopes": ["read:user"],
    }


def auth(welcome_seen=True, signed_in=True, flow=None, star_pending=False):
    return {
        "providers": [_provider()],
        "signed_in": signed_in,
        "login": {"login": "diskwarden-dev", "name": "Disk Warden Dev"} if signed_in else None,
        "welcome_seen": welcome_seen,
        "star": {"pending": star_pending, "dismissed": False, "starred": False},
        "secure_storage": True,
        "flow": flow,
        "repository_url": "https://github.com/QuotaFuse-ai/disk-warden",
        "gitee": {"label": "Gitee", "url": "", "available": False},
    }


def device_flow():
    """GitHub device-login in progress.  ``@now+600`` is resolved by the bridge."""
    return {
        "flow_id": "fl_20261021_01",
        "provider": "github",
        "state": "pending",
        "user_code": "WD8K-2Q9X",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 600,
        "expires_at": "@now+600",
        "interval": 5,
        "started_at": "2026-10-21T10:00:00Z",
        "updated_at": "2026-10-21T10:00:00Z",
        "error": None,
    }


def learned_rules():
    return {
        "items": [
            {"item_id": "cursor-ext", "safety": "green",
             "reason": "扩展缓存，可再生", "rule_id": "cursor.extensions"},
            {"item_id": "cursor-projects", "safety": "green",
             "reason": "各项目工作区索引，可再生", "rule_id": "cursor.workspace"},
            {"item_id": "windsurf-cascade", "safety": "green",
             "reason": "Cascade 对话历史，可再生", "rule_id": "windsurf.cascade"},
            {"item_id": "cursor-user", "safety": "red",
             "reason": "用户配置，不可移动", "rule_id": "cursor.user"},
            {"item_id": "trae-user", "safety": "red",
             "reason": "人工标记为不可迁移", "rule_id": "manual"},
        ],
        "warnings": [
            "windsurf 规则来自 2026-10-11 的人工标记，3 个目录读取被拒绝",
        ],
    }


def _record(id_, type_, result, at, app, tool_id, frm, to, freed, seconds,
            can_rollback=False, job_id="", code="", item="", note=""):
    return {
        "id": id_,
        "at": at,
        "type": type_,
        "tool_id": tool_id,
        "app": app,
        "item": item,
        "from": frm,
        "to": to,
        "freed": freed,
        "duration_seconds": seconds,
        "result": result,
        "raw_result": "failure" if result == "failed" else result,
        "can_rollback": can_rollback,
        "code": code,
        "note": note,
        "stage": code,
        "job_id": job_id,
    }


def history():
    return {
        "items": [
            _record("h_0005", "migrate", "success", "2026-10-18T15:04:00Z", "VS Code",
                    "vscode", HOME + "\\.vscode\\extensions",
                    "D:\\DiskWarden\\vscode\\extensions", int(2.9 * GB), 52,
                    can_rollback=True, job_id="jb_20261018_02", item="extensions"),
            _record("h_0004", "migrate", "success", "2026-10-12T11:26:00Z", "Cursor",
                    "cursor", HOME + "\\.cursor\\projects",
                    "D:\\DiskWarden\\cursor\\projects", int(1.1 * GB), 34,
                    can_rollback=True, job_id="jb_20261012_01", item="projects"),
            _record("h_0003", "rollback", "success", "2026-10-09T18:41:00Z", "Windsurf",
                    "windsurf", "D:\\DiskWarden\\windsurf\\extensions",
                    HOME + "\\.windsurf\\extensions", 0, 27,
                    job_id="jb_20261009_07", item="extensions"),
            _record("h_0002", "migrate", "rolledback", "2026-10-09T18:12:00Z", "Trae CN",
                    "trae", HOME + "\\.trae\\extensions",
                    "D:\\DiskWarden\\trae\\extensions", 0, 19,
                    job_id="jb_20261009_06", item="extensions"),
            _record("h_0001", "migrate", "failed", "2026-10-08T09:52:00Z", "Trae CN",
                    "trae", _LONG_PATH, "D:\\DiskWarden\\trae\\node_modules", 0, 8,
                    job_id="jb_20261008_04", code="EACCES",
                    item="AppData\\Local\\Temp\\trae-build-cache"),
        ],
        "count": 5,
        "stats": {"total": 5, "rolled_back": 1, "complete": True},
    }


def plan_preview():
    total = int(2.6 * GB) + int(1.4 * GB) + int(512 * MB) + int(96 * MB)
    return {
        "plan_id": "pl_20261021_01",
        "confirm_token": "tok_pl_20261021_01",
        "expires_at": "2026-10-21T10:15:00Z",
        "tool_name": "Cursor",
        "tool_id": "cursor",
        "total_bytes": total,
        "need_bytes": int(total * 1.05) + 64 * MB,
        "free_bytes": int(612 * GB),
        "yellow_item_ids": ["cursor-logs"],
        "unconfirmed_yellow": [],
        "requires_confirm": False,
        "items": [
            {"item_id": "cursor-ext", "tool_id": "cursor", "tool_name": "Cursor",
             "safety": "green", "requires_confirm": False, "bytes": int(2.6 * GB),
             "file_count": 1842,
             "source_path": HOME + "\\.cursor\\extensions",
             "final_path": "D:\\DiskWarden\\cursor\\extensions"},
            {"item_id": "cursor-cache", "tool_id": "cursor", "tool_name": "Cursor",
             "safety": "green", "requires_confirm": False, "bytes": int(1.4 * GB),
             "file_count": 5310,
             "source_path": HOME + "\\AppData\\Roaming\\Cursor\\Cache",
             "final_path": "D:\\DiskWarden\\cursor\\Cache"},
            {"item_id": "cursor-codecache", "tool_id": "cursor", "tool_name": "Cursor",
             "safety": "green", "requires_confirm": False, "bytes": int(512 * MB),
             "file_count": 904,
             "source_path": HOME + "\\AppData\\Roaming\\Cursor\\Code Cache",
             "final_path": "D:\\DiskWarden\\cursor\\CodeCache"},
            {"item_id": "cursor-logs", "tool_id": "cursor", "tool_name": "Cursor",
             "safety": "yellow", "requires_confirm": True, "bytes": int(96 * MB),
             "file_count": 41,
             "source_path": HOME + "\\AppData\\Roaming\\Cursor\\logs",
             "final_path": "D:\\DiskWarden\\cursor\\logs"},
        ],
    }


def rollback_preview():
    return {
        "job_id": "jb_20261018_02",
        "plan_id": "pl_rb_20261021_01",
        "confirm_token": "tok_rb_20261021_01",
        "expires_at": "2026-10-21T10:15:00Z",
        "tool_id": "vscode",
        "tool_name": "VS Code",
        "source_path": HOME + "\\.vscode\\extensions",
        "final_path": "D:\\DiskWarden\\vscode\\extensions",
        "restore_bytes": int(2.1 * GB),
        "free_bytes": int(612 * GB),
        "required_bytes": int(2.1 * GB) + 64 * MB,
        "can_proceed": True,
        "mode": "restore",
        "requires_residual_confirm": False,
        "error_code": "",
        "error_message": "",
    }


def finished_job(job_id="jb_20261021_09", kind="migrate"):
    return {
        "job_id": job_id,
        "kind": kind,
        "title": kind,
        "state": "done",
        "step": "done",
        "progress": {"stage": "done", "items_total": 4, "items_done": 4,
                     "index": 4, "status": "done", "done": True},
        "summary": {"ok": True, "failed": 0, "cancelled": False,
                    "bytes": int(4.5 * GB), "items": []},
        "error": None,
        "warnings": [],
        "created_at": "2026-10-21T10:02:11Z",
        "started_at": "2026-10-21T10:02:11Z",
        "updated_at": "2026-10-21T10:02:49Z",
        "finished_at": "2026-10-21T10:02:49Z",
    }


def active_scan_job():
    return {
        "job_id": "jb_20261021_scan",
        "kind": "scan",
        "title": "scan",
        "state": "running",
        "step": "measure",
        "progress": {"stage": "measure", "targets_total": 40, "targets_done": 26},
        "summary": None,
        "error": None,
        "warnings": [],
        "created_at": "2026-10-21T10:05:00Z",
        "started_at": "2026-10-21T10:05:00Z",
        "updated_at": "2026-10-21T10:05:41Z",
        "finished_at": None,
    }


def fixtures():
    """Every rpc method the SPA can call, mapped to a plain-JSON result.

    Shapes mirror ``src/app/service.py`` one-for-one, so a screenshot and the
    real host render the same markup.  When the service adds a field the UI
    reads, add it here too -- a missing key shows up as an empty cell.
    """
    snap = snapshot()
    return {
        # -- boot ---------------------------------------------------------
        "app.hello": {
            "app": "disk-warden",
            "version": "0.9.3",
            "protocol": 1,
            "started_at": "2026-10-21T09:58:00Z",
            "settings": settings(),
            "auth": auth(),
            "disk": disk_info(),
            "recovery": {"items": [], "count": 0, "warnings": []},
            "scan": {
                "available": True,
                "scanned_at": snap["scanned_at"],
                "stats": snap["stats"],
            },
        },
        "app.ping": {"ok": True, "protocol": 1},
        "app.open_external": {"ok": True},
        "app.reveal": {"ok": True},

        # -- scan ---------------------------------------------------------
        "scan.result": snap,
        "scan.start": {"job_id": "jb_20261021_scan", "state": "queued"},
        "scan.status": {"job": active_scan_job(), "recent_events": []},
        "scan.cancel": {"job_id": "jb_20261021_scan",
                        "cancel_requested": True, "state": "cancelling"},

        # -- read-only info ----------------------------------------------
        "disk.info": disk_info(),
        "rules.learned.list": learned_rules(),
        "rules.override": learned_rules(),
        "rules.override.clear": learned_rules(),
        "history.list": history(),
        "job.list": {"jobs": []},
        "job.status": {"job": finished_job(), "recent_events": []},

        # -- recovery -----------------------------------------------------
        "recovery.list": {"items": [], "count": 0, "warnings": []},
        "recovery.run": {"job_id": "jb_recovery_01", "type": "migrate",
                         "state": "running"},
        "recovery.ignore": {"job_id": "jb_recovery_01", "type": "migrate",
                            "state": "ignored"},

        # -- settings / AI ------------------------------------------------
        "settings.get": settings(),
        "settings.set": settings(),
        "ai.status": settings()["ai"],
        "ai.consent": settings()["ai"],
        "ai.test": {"ok": True, "latency_ms": 812, "model": "deepseek-chat",
                    "error_code": "", "message": ""},

        # -- auth / welcome / star ---------------------------------------
        "welcome.seen": auth(welcome_seen=True),
        "auth.status": auth(),
        "auth.start": auth(flow=device_flow()),
        "auth.cancel": auth(flow=None),
        "auth.sign_out": auth(signed_in=False, welcome_seen=True),
        "star.check": auth(),
        "star.show": auth(star_pending=True),
        "star.give": auth(),
        "star.dismiss": auth(),

        # -- planning / migration / rollback ------------------------------
        "plan.preview": plan_preview(),
        "migrate.start": {"job_id": "jb_20261021_09", "state": "queued",
                          "items": 4},
        "rollback.preview": rollback_preview(),
        "rollback.start": {"job_id": "jb_20261021_10", "state": "queued",
                           "target_job_id": "jb_20261018_02"},
    }
