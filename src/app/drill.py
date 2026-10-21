# -*- coding: utf-8 -*-
"""QA-only sandbox drills for the packaged build (T15).

The drills are reached through the frozen executable itself::

    DiskWarden.exe --drill migrate-rollback --sandbox C:\\DiskWardenSandbox\\t15 --report report.json

so packaging, resource lookup and the migration engine are exercised exactly
as a user would run them.  Nothing here is imported by the application; the
launcher only imports this module when ``--drill`` is present.

Every drill builds its own tree below ``--sandbox`` (an ``appdata`` folder used
as ``%APPDATA%`` and a data root); the real profile is never read or written.
The report is a JSON file because a windowed build has no console.

Scenarios
    migrate-rollback  scan -> plan -> migrate -> verify junction -> rollback
    crash-prepare     scan -> migrate, frozen at the configured crash point;
                      the harness force-kills the process
    crash-recover     recovery list -> recover -> migrate again -> rollback
    settings          settings payload, log directory, size rotation
    autostart         real per-user Run key toggled through settings.set
"""

import hashlib
import json
import os
import shutil
import sys
import time
import traceback

from . import APP_VERSION

SCENARIOS = (
    "migrate-rollback",
    "crash-prepare",
    "crash-recover",
    "settings",
    "autostart",
)

CRASH_POINTS = ("before-swap", "after-rename")
CRASH_FREEZE_SECONDS = 600

CHUNK = 64 * 1024


class DrillFailure(Exception):
    """A drill check failed; the message is written into the report."""


# -- small helpers -------------------------------------------------------


def _norm(path):
    # type: (str) -> str
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _remove_tree(path):
    # type: (str) -> None
    if not os.path.lexists(path):
        return
    # A junction must be unlinked, never followed: rmtree would delete the
    # data it points at.
    try:
        if os.path.islink(path):
            os.rmdir(path)
            return
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _write_json(path, payload):
    # type: (str, object) -> None
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _sha256(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root):
    # type: (str) -> dict
    """``{relative posix path: [size, sha256]}`` for regular files."""
    root = os.path.abspath(root)
    state = {}
    for current, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            if name.startswith(".dw-owner-"):
                continue
            path = os.path.join(current, name)
            try:
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                state[relative] = [os.path.getsize(path), _sha256(path)]
            except OSError:
                continue
    return state


def _write_pattern(path, size, seed):
    # type: (str, int, bytes) -> None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = (seed * (CHUNK // len(seed) + 1))[:CHUNK]
    remaining = int(size)
    with open(path, "wb") as handle:
        while remaining > 0:
            chunk = block[: min(CHUNK, remaining)]
            handle.write(chunk)
            remaining -= len(chunk)


# -- sandbox -------------------------------------------------------------


def build_sandbox(base):
    # type: (str) -> dict
    """Create a fresh fake profile and data root below ``base``."""
    appdata = os.path.join(base, "appdata")
    root = os.path.join(base, "root")
    for path in (appdata, root):
        _remove_tree(path)
    cursor = os.path.join(appdata, "Cursor")
    cache = os.path.join(cursor, "Cache")
    user = os.path.join(cursor, "User")

    _write_pattern(os.path.join(cache, "level1", "data.bin"), 512 * 1024, b"cache-data-")
    _write_pattern(os.path.join(cache, "level1", "sub", "more.bin"), 256 * 1024, b"more-data-")
    _write_pattern(os.path.join(cache, "index.bin"), 64 * 1024, b"index----")

    os.makedirs(user, exist_ok=True)
    with open(os.path.join(user, "settings.json"), "w", encoding="utf-8") as handle:
        handle.write('{"theme": "drill"}')
    _write_pattern(os.path.join(user, "globalStorage", "state.vscdb"), 32 * 1024, b"state----")

    for name in ("localappdata", "profile"):
        os.makedirs(os.path.join(base, name), exist_ok=True)

    return {
        "base": os.path.abspath(base),
        "appdata": appdata,
        "root": root,
        "tool_root": cursor,
        "source": cache,
    }


def _existing_sandbox(base):
    # type: (str) -> dict
    appdata = os.path.join(base, "appdata")
    root = os.path.join(base, "root")
    cursor = os.path.join(appdata, "Cursor")
    return {
        "base": os.path.abspath(base),
        "appdata": appdata,
        "root": root,
        "tool_root": cursor,
        "source": os.path.join(cursor, "Cache"),
    }


def _sandbox_catalog(sandbox):
    # type: (dict) -> str
    """Copy of the shipped catalog whose tools can never be "running".

    The build machine is normally running the very IDE the catalog watches
    (this workspace lives inside Cursor), so the real process guard would
    block every sandbox migration.  Scanning, planning and the completion
    guard all read this file from disk exactly like the shipped catalog; only
    the executable names differ, and none of them is running.
    """
    from .service import DEFAULT_CATALOG_PATH

    path = os.path.join(sandbox["base"], "drill-catalog.json")
    if not os.path.isfile(path):
        with open(DEFAULT_CATALOG_PATH, "r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        for tool in catalog.get("ides", catalog.get("tools", [])):
            tool["processes"] = ["DiskWardenDrillIdle.exe"]
        _write_json(path, catalog)
    return path


def _service(sandbox):
    # type: (dict) -> object
    from .service import AppService

    environ = dict(os.environ)
    environ["APPDATA"] = sandbox["appdata"]
    environ["LOCALAPPDATA"] = os.path.join(sandbox["base"], "localappdata")
    environ["USERPROFILE"] = os.path.join(sandbox["base"], "profile")
    return AppService(
        data_root=sandbox["root"],
        catalog_path=_sandbox_catalog(sandbox),
        environ=environ,
        process_provider=lambda: (),
    )


# -- service driving -----------------------------------------------------


def _find_item(service, source_path):
    # type: (object, str) -> dict
    wanted = _norm(source_path)
    for item in service.items_list()["items"]:
        if _norm(item["source_path"]) == wanted and item.get("kind") == "dir":
            return item
    raise DrillFailure("scan did not report the sandbox Cache directory")


def _migrate(service, source_path, timeout=240.0):
    # type: (object, str, float) -> dict
    job = service.runner.wait(service.scan_start()["job_id"], timeout=120.0)
    if job.get("state") != "done":
        raise DrillFailure("scan job ended as %r" % (job.get("state"),))

    item = _find_item(service, source_path)
    if not item.get("can_migrate"):
        raise DrillFailure(
            "sandbox item is not migratable: %s %s"
            % (item.get("blocked_code"), item.get("blocked_reason"))
        )

    plan = service.plan_preview([item["item_id"]], [item["item_id"]])
    started = service.migrate_start(
        plan["plan_id"], plan["confirm_token"], [item["item_id"]]
    )
    finished = service.runner.wait(started["job_id"], timeout=timeout)
    summary = finished.get("summary") or {}
    items = summary.get("items") or []
    if finished.get("state") != "done" or not items:
        raise DrillFailure("migration job ended as %r (%r)" % (finished.get("state"), finished.get("error")))
    result = items[0]
    if not result.get("ok"):
        raise DrillFailure(
            "migration failed: %s %s"
            % (result.get("state"), result.get("error_message"))
        )
    return result


def _rollback(service, job_item_id, timeout=240.0):
    # type: (object, str, float) -> dict
    preview = service.rollback_preview(job_item_id)
    if not preview.get("can_proceed"):
        raise DrillFailure(
            "rollback precheck refused: %s %s"
            % (preview.get("error_code"), preview.get("error_message"))
        )
    started = service.rollback_start(preview["plan_id"], preview["confirm_token"])
    finished = service.runner.wait(started["job_id"], timeout=timeout)
    summary = finished.get("summary") or {}
    if finished.get("state") != "done" or not summary.get("ok"):
        raise DrillFailure(
            "rollback failed: %s %s"
            % (summary.get("state"), summary.get("error_message"))
        )
    return summary


def _link_evidence(source_path):
    # type: (str) -> dict
    from ..platform import is_junction, read_junction_target

    target = read_junction_target(source_path) if is_junction(source_path) else None
    probe = ""
    if target:
        probe_path = os.path.join(source_path, "index.bin")
        try:
            with open(probe_path, "rb") as handle:
                probe = hashlib.sha256(handle.read()).hexdigest()
        except OSError as exc:
            probe = "read failed: %s" % exc
    return {
        "is_junction": bool(is_junction(source_path)),
        "target": target or "",
        "probe_sha256": probe,
    }


def _leftovers(sandbox, tool_id):
    # type: (dict, str) -> list
    found = []
    for name in os.listdir(os.path.dirname(sandbox["source"])):
        if name.startswith(".dwtombstone-") or name.startswith(".dw-restore-"):
            found.append(os.path.join(os.path.dirname(sandbox["source"]), name))
    target_parent = os.path.join(sandbox["root"], "data", tool_id)
    if os.path.isdir(target_parent):
        for name in os.listdir(target_parent):
            if name.startswith(".dw-partial-"):
                found.append(os.path.join(target_parent, name))
    return found


def _install_crash(point, marker_path):
    # type: (str, str) -> None
    """Freeze the migration worker at ``point`` so the harness can kill it."""
    from . import service as service_module
    from ..core.migrate import MigrationOps

    real = service_module.complete_migration

    class CrashOps(MigrationOps):
        def rename(self, source, destination):
            result = super(CrashOps, self).rename(source, destination)
            if point == "after-rename" and os.path.basename(destination).startswith(
                ".dwtombstone-"
            ):
                _freeze(marker_path, point, {"from": source, "to": destination})
            return result

    def crashing(result, *args, **kwargs):
        if point == "before-swap":
            _freeze(
                marker_path,
                point,
                {"source": result.source_path, "staging": result.staging_path},
            )
        kwargs["ops"] = CrashOps()
        return real(result, *args, **kwargs)

    service_module.complete_migration = crashing


def _freeze(marker_path, point, detail):
    # type: (str, str, object) -> None
    _write_json(
        marker_path,
        {
            "point": point,
            "pid": os.getpid(),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "detail": detail,
        },
    )
    # The harness sees the marker and runs taskkill /F; the sleep only keeps
    # the process alive long enough for that to happen.
    time.sleep(CRASH_FREEZE_SECONDS)
    os._exit(9)


def _wait_marker(path, timeout):
    # type: (str, float) -> dict
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    raise DrillFailure("crash marker was not written within %.0fs" % timeout)


# -- scenarios -----------------------------------------------------------


def _scenario_migrate_rollback(report, sandbox, options):
    # type: (dict, dict, dict) -> None
    checks = report["checks"]

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise DrillFailure("%s: %s" % (name, detail))

    before = tree_hashes(sandbox["source"])
    report["source_snapshot"] = {"files": len(before), "bytes": sum(v[0] for v in before.values())}

    service = _service(sandbox)
    try:
        result = _migrate(service, sandbox["source"])
        final_path = result["target_path"]
        evidence = _link_evidence(sandbox["source"])
        check("migration reported success", True, result.get("state", ""))
        check("source became a junction", evidence["is_junction"], evidence["target"])
        check(
            "junction points at the target copy",
            _norm(evidence["target"]) == _norm(final_path),
            "%s vs %s" % (evidence["target"], final_path),
        )
        check(
            "data reads through the junction",
            evidence["probe_sha256"] == before.get("index.bin", [0, ""])[1],
            evidence["probe_sha256"],
        )
        check(
            "target copy matches the source snapshot",
            tree_hashes(final_path) == before,
            "%d files" % len(tree_hashes(final_path)),
        )

        from ..platform import is_junction

        summary = _rollback(service, result["job_id"])
        check(
            "rollback restored the source",
            summary.get("source_restored") and summary.get("target_removed"),
            "%s / %s" % (summary.get("source_restored"), summary.get("target_removed")),
        )
        check("source is a real directory again", not is_junction(sandbox["source"]))
        check("source content survived the round trip", tree_hashes(sandbox["source"]) == before)
        check(
            "no tombstones or staging left behind",
            not _leftovers(sandbox, "cursor"),
            ", ".join(_leftovers(sandbox, "cursor")),
        )
    finally:
        service.close()


def _scenario_crash_prepare(report, sandbox, options):
    # type: (dict, dict, dict) -> None
    point = options.get("crash_point") or "after-rename"
    marker = options["marker"]
    if os.path.exists(marker):
        os.remove(marker)

    before = tree_hashes(sandbox["source"])
    _write_json(os.path.join(sandbox["base"], "source-hash.json"), before)

    # scan + plan first, so the freeze only covers the write phase
    service = _service(sandbox)
    job = service.runner.wait(service.scan_start()["job_id"], timeout=120.0)
    if job.get("state") != "done":
        raise DrillFailure("scan job ended as %r" % (job.get("state"),))
    item = _find_item(service, sandbox["source"])
    plan = service.plan_preview([item["item_id"]], [item["item_id"]])

    _install_crash(point, marker)
    started = service.migrate_start(
        plan["plan_id"], plan["confirm_token"], [item["item_id"]]
    )
    report["migration_job_id"] = started["job_id"]
    report["crash_point"] = point

    frozen = _wait_marker(marker, timeout=180.0)
    report["freeze"] = frozen
    # Give the harness time to run taskkill /F.  Still alive after that means
    # the force-kill did not happen, so record it and stop instead of letting
    # the sleep end on its own and look like a normal run.
    time.sleep(30.0)
    report["checks"].append(
        {
            "name": "process was force-killed by the harness",
            "ok": False,
            "detail": "still running 30s after the crash marker",
        }
    )
    report["error"] = "the harness did not force-kill the frozen process"
    _write_json(options["report"], report)
    os._exit(4)


def _scenario_crash_recover(report, sandbox, options):
    # type: (dict, dict, dict) -> None
    checks = report["checks"]

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise DrillFailure("%s: %s" % (name, detail))

    with open(os.path.join(sandbox["base"], "source-hash.json"), "r", encoding="utf-8") as handle:
        before = json.load(handle)

    service = _service(sandbox)
    try:
        pending = service.recovery_list()
        report["recovery_before"] = pending
        matches = [
            entry
            for entry in pending.get("items", [])
            if entry.get("type") == "migration"
            and _norm(entry.get("source_path") or "") == _norm(sandbox["source"])
        ]
        check("restart sees the interrupted migration", bool(matches), "%d pending" % len(matches))

        run = service.recovery_run(matches[0]["job_id"], "recover")
        finished = service.runner.wait(run["job_id"], timeout=240.0)
        summary = finished.get("summary")
        report["recovery_run"] = {"job": finished.get("state"), "summary": summary}
        check("recovery job finished", finished.get("state") == "done", str(finished.get("error")))

        from ..platform import is_junction

        check("source is a plain directory after recovery", os.path.isdir(sandbox["source"]) and not is_junction(sandbox["source"]))
        check("recovered source matches the pre-crash snapshot", tree_hashes(sandbox["source"]) == before)
        check("no tombstones or staging left behind", not _leftovers(sandbox, "cursor"), ", ".join(_leftovers(sandbox, "cursor")))

        after = service.recovery_list()
        report["recovery_after"] = after
        still = [
            entry
            for entry in after.get("items", [])
            if _norm(entry.get("source_path") or "") == _norm(sandbox["source"])
        ]
        check("nothing is left to recover", not still, "%d pending" % len(still))

        # The tool must be usable again: migrate, verify, roll back.
        result = _migrate(service, sandbox["source"])
        evidence = _link_evidence(sandbox["source"])
        check("migration works after recovery", evidence["is_junction"] and _norm(evidence["target"]) == _norm(result["target_path"]), evidence["target"])
        summary = _rollback(service, result["job_id"])
        check("rollback after recovery restores the source", bool(summary.get("ok")), summary.get("state"))
        check("source content survived recovery + migrate + rollback", tree_hashes(sandbox["source"]) == before)
    finally:
        service.close()


def _scenario_settings(report, sandbox, options):
    # type: (dict, dict, dict) -> None
    checks = report["checks"]

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise DrillFailure("%s: %s" % (name, detail))

    service = _service(sandbox)
    try:
        settings = service.settings_get()
        report["settings"] = {
            "app_version": settings.get("app_version"),
            "data_root": settings.get("data_root"),
            "logs_dir": (settings.get("paths") or {}).get("logs_dir"),
            "launch_at_startup_supported": settings.get("launch_at_startup_supported"),
            "launch_at_startup_command": settings.get("launch_at_startup_command"),
        }
        logs_dir = (settings.get("paths") or {}).get("logs_dir") or ""
        check("settings expose the log directory", bool(logs_dir) and os.path.isdir(logs_dir), logs_dir)
        check("AI analysis is off by default", settings.get("ai", {}).get("enabled") is False)
        check("settings never carry the plaintext key", "api_key" not in settings.get("ai", {}))

        from .main import _configure_logging

        _configure_logging(logs_dir)
        app_log = os.path.join(logs_dir, "app.log")
        check("app.log is created in the log directory", os.path.isfile(app_log), app_log)

        # Drive the real handler past 2 MB: the file must rotate to .1 and
        # stay capped, or a long-running install fills the disk.
        import logging

        logger = logging.getLogger("diskwarden.drill")
        for index in range(15000):
            logger.info("drill rotation line %d %s", index, "x" * 200)
        for handler in logging.getLogger().handlers:
            handler.flush()
        size = os.path.getsize(app_log)
        rotated = os.path.isfile(app_log + ".1")
        report["log"] = {"size": size, "rotated": rotated}
        check("oversized log rotates to app.log.1", rotated)
        check("active log stays under the 2 MB cap", size <= 2 * 1024 * 1024, str(size))
    finally:
        service.close()


def _scenario_autostart(report, sandbox, options):
    # type: (dict, dict, dict) -> None
    checks = report["checks"]
    created = False

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise DrillFailure("%s: %s" % (name, detail))

    service = _service(sandbox)
    try:
        initial = service.settings_get()
        report["autostart_command"] = initial.get("launch_at_startup_command")
        check("startup item is supported on Windows", bool(initial.get("launch_at_startup_supported")), str(initial.get("launch_at_startup_error")))
        check("startup command points at this exe", sys.executable.lower() in (initial.get("launch_at_startup_command") or "").lower(), initial.get("launch_at_startup_command") or "")

        enabled = service.settings_set({"launch_at_startup": True})
        created = True
        check("settings report the startup item as enabled", bool(enabled.get("launch_at_startup")), str(enabled.get("launch_at_startup_error")))

        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        try:
            value, kind = winreg.QueryValueEx(key, "DiskWarden")
        finally:
            key.Close()
        report["registry_value"] = {"data": value, "type": kind}
        check("HKCU Run value exists without elevation", bool(value), value)
        check("Run value starts the exe in tray mode", value.lower().endswith("--tray"), value)

        disabled = service.settings_set({"launch_at_startup": False})
        created = False
        check("settings report the startup item as disabled", not disabled.get("launch_at_startup"))
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            )
            try:
                leftover = winreg.QueryValueEx(key, "DiskWarden")[0]
            finally:
                key.Close()
        except FileNotFoundError:
            leftover = ""
        check("Run value is removed when disabled", not leftover, leftover)
    finally:
        if created:
            try:
                service.settings_set({"launch_at_startup": False})
            except Exception:  # pragma: no cover - cleanup best effort
                pass
        service.close()


_RUNNERS = {
    "migrate-rollback": (_scenario_migrate_rollback, True),
    "crash-prepare": (_scenario_crash_prepare, True),
    "crash-recover": (_scenario_crash_recover, False),
    "settings": (_scenario_settings, True),
    "autostart": (_scenario_autostart, True),
}


# -- entry point ---------------------------------------------------------


def _parse(argv):
    # type: (object) -> dict
    options = {
        "scenario": "",
        "sandbox": "",
        "report": "",
        "crash_point": "after-rename",
        "marker": "",
    }
    args = list(argv or ())
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--drill" and index + 1 < len(args):
            options["scenario"] = args[index + 1]
            index += 2
            continue
        for name in ("sandbox", "report", "crash-point", "marker"):
            flag = "--" + name
            if arg == flag and index + 1 < len(args):
                options[name.replace("-", "_")] = args[index + 1]
                index += 2
                break
        else:
            index += 1
    if options["scenario"] not in SCENARIOS:
        raise DrillFailure("unknown scenario %r (expected one of %s)" % (options["scenario"], ", ".join(SCENARIOS)))
    if not options["sandbox"]:
        raise DrillFailure("--sandbox is required")
    if options["crash_point"] not in CRASH_POINTS:
        raise DrillFailure("--crash-point must be one of %s" % (", ".join(CRASH_POINTS),))
    if not options["report"]:
        options["report"] = os.path.join(options["sandbox"], "drill-report-%s.json" % options["scenario"])
    if not options["marker"]:
        options["marker"] = os.path.join(options["sandbox"], "crash-marker.json")
    return options


def run_drill(argv):
    # type: (object) -> int
    """Run one drill and return the process exit code."""
    report = {
        "app_version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checks": [],
        "ok": False,
    }
    options = None
    try:
        options = _parse(argv)
        report["scenario"] = options["scenario"]
        report["sandbox"] = os.path.abspath(options["sandbox"])
        sandbox = (
            build_sandbox(options["sandbox"])
            if _RUNNERS[options["scenario"]][1]
            else _existing_sandbox(options["sandbox"])
        )
        runner = _RUNNERS[options["scenario"]][0]
        runner(report, sandbox, options)
        report["ok"] = all(item["ok"] for item in report["checks"])
    except DrillFailure as exc:
        report["error"] = str(exc)
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if options is not None:
            try:
                _write_json(options["report"], report)
            except Exception:
                pass
    return 0 if report.get("ok") else 1


__all__ = ["run_drill"]
