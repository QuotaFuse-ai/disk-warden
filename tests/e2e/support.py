# -*- coding: utf-8 -*-
"""Sandbox helpers for the end-to-end suite (T13).

Hard rules enforced here:

* every path a test touches lives below ``Sandbox.root`` (a fresh temporary
  directory), so the real ``%APPDATA%``/``%LOCALAPPDATA%``/``%USERPROFILE%``
  trees are never read or written;
* fixtures use real multi-gigabyte synthetic files and a realistic IDE
  directory hierarchy, not a handful of tiny files;
* verification compares file counts, total bytes and per-file SHA-256, and
  treats junctions as links instead of walking into them.

Python 3.8 compatible (decision 0005).
"""

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.app.service import AppService  # noqa: E402
from src.core.store.layout import DataLayout  # noqa: E402
from src.platform import is_junction, read_junction_target  # noqa: E402
import src.app.service as _service_module  # noqa: E402

CATALOG_PATH = os.path.join(REPO_ROOT, "data", "ide-catalog.json")
RULES_PATH = os.path.join(REPO_ROOT, "data", "rules.json")
TOOL_ID = "cursor"
KILL_EXIT = 137
MiB = 1024 * 1024

# Two profiles: ``real`` is the acceptance profile (multi-GB); ``small`` keeps
# a quick local loop possible on a nearly full disk.  ``tools/run_e2e.py
# --scale=...`` selects one explicitly.
PROFILES = {
    "real": {
        "blobs": [(1200 * MiB, "cache-blob-1.bin"), (900 * MiB, "cache-blob-2.bin")],
        "bulk_files": 260,
        "bulk_size": 32 * 1024,
        "medium_size": 16 * MiB,
        "log_files": 40,
    },
    "small": {
        "blobs": [(6 * MiB, "cache-blob-1.bin"), (2 * MiB, "cache-blob-2.bin")],
        "bulk_files": 120,
        "bulk_size": 16 * 1024,
        "medium_size": 512 * 1024,
        "log_files": 20,
    },
}


# -- environment -----------------------------------------------------------


def real_appdata():
    # type: () -> str
    """The real roaming profile path; only ever used for negative assertions."""
    return os.path.abspath(os.environ.get("APPDATA") or "")


def log_dir():
    # type: () -> str
    path = os.environ.get("DISKWARDEN_E2E_LOGS") or os.path.join(
        REPO_ROOT, "tests", "e2e", "logs"
    )
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def _free_bytes(path):
    # type: (str) -> int
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def choose_scale():
    # type: () -> str
    explicit = (os.environ.get("DISKWARDEN_E2E_SCALE") or "").strip().lower()
    if explicit in PROFILES:
        return explicit
    # Auto: real scale needs room for source + staging + final + tombstone.
    for candidate in _candidate_bases():
        if _free_bytes(candidate) >= 9 * 1024 ** 3:
            return "real"
    return "small"


def _candidate_bases():
    # type: () -> list
    values = []
    override = os.environ.get("DISKWARDEN_E2E_ROOT")
    if override:
        values.append(override)
    for drive in ("D:\\", "F:\\", "E:\\"):
        if os.path.isdir(drive):
            values.append(drive)
    values.append(tempfile.gettempdir())
    return [value for value in values if os.path.isdir(value)]


def sandbox_base():
    # type: () -> str
    """Pick a base directory with room for the fixture."""
    override = os.environ.get("DISKWARDEN_E2E_ROOT")
    if override:
        if not os.path.isdir(override):
            os.makedirs(override, exist_ok=True)
        return override
    for candidate in _candidate_bases():
        if _free_bytes(candidate) >= 2 * 1024 ** 3:
            return candidate
    return tempfile.gettempdir()


# -- fixture writing -------------------------------------------------------


def _pattern_block(seed):
    # type: (str) -> bytes
    raw = hashlib.sha256(seed.encode("utf-8")).digest()
    return raw * ((MiB // len(raw)) + 1)


def write_pattern(path, size, seed):
    # type: (str, int, str) -> None
    """Write ``size`` deterministic bytes without burning entropy."""
    block = _pattern_block(seed)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    remaining = int(size)
    with open(path, "wb") as handle:
        while remaining > 0:
            take = min(len(block), remaining)
            handle.write(block if take == len(block) else block[:take])
            remaining -= take


def write_text(path, text):
    # type: (str, str) -> None
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path, payload):
    # type: (str, object) -> None
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


# -- snapshots -------------------------------------------------------------


def _sha256(path):
    # type: (str) -> str
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * MiB)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path):
    # type: (str) -> bool
    try:
        if is_junction(path):
            return True
    except OSError:
        pass
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def tree_state(root, skip_owner_markers=True):
    # type: (str, bool) -> dict
    """Return ``{relative posix path: {"size": n, "sha256": hex}}``.

    Only regular files are hashed; junctions and symlinks are skipped so a
    migrated source never makes the snapshot run away into the target.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {}
    found = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if _is_reparse(entry.path):
                        continue
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if skip_owner_markers and entry.name.startswith(".dw-owner-"):
                continue
            relative = os.path.relpath(entry.path, root).replace(os.sep, "/")
            found.append((relative, entry.path))

    state = {}

    def hash_one(pair):
        relative, path = pair
        try:
            size = os.path.getsize(path)
            return relative, {"size": size, "sha256": _sha256(path)}
        except OSError as exc:
            return relative, {"size": -1, "sha256": "error:%s" % exc}

    # hashlib releases the GIL for large buffers, so a small pool makes the
    # multi-GB fixtures cheap without weakening the full-hash guarantee.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for relative, payload in pool.map(hash_one, found):
            state[relative] = payload
    return state


def tree_totals(state):
    # type: (dict) -> dict
    return {
        "files": len(state),
        "bytes": sum(int(item["size"]) for item in state.values()),
    }


def tree_depth(root):
    # type: (str) -> int
    root = os.path.abspath(root)
    deepest = 0
    for current, dirnames, filenames in os.walk(root):
        if filenames or not dirnames:
            relative = os.path.relpath(current, root)
            if relative == ".":
                continue
            deepest = max(deepest, len(relative.split(os.sep)))
    return deepest


# -- sandbox ---------------------------------------------------------------


# ``AppService`` forwards its injected ``process_provider`` to the plan and the
# rollback engine, but *not* to ``complete_migration`` (S5-S9): that call falls
# back to the real process table.  On a developer machine running the IDE this
# makes every migration end in ``PROCESS_BUSY`` regardless of the sandbox, so
# the suite installs one proxy that forwards the sandbox provider (and lets the
# hard-interrupt driver inject its killing ``MigrationOps``).
_COMPLETION_HOOK = None
_ORIGINAL_COMPLETE = _service_module.complete_migration


def _completion_proxy(result, **kwargs):
    # type: (object, object) -> object
    hook = _COMPLETION_HOOK
    if hook is not None:
        hook(result, kwargs)
    return _ORIGINAL_COMPLETE(result, **kwargs)


_service_module.complete_migration = _completion_proxy


def set_completion_hook(hook):
    # type: (object) -> object
    """Install ``hook(result, kwargs)`` before S5-S9; returns the previous one."""
    global _COMPLETION_HOOK
    previous = _COMPLETION_HOOK
    _COMPLETION_HOOK = hook
    return previous


def completion_hook():
    # type: () -> object
    return _COMPLETION_HOOK


def provider_hook(process_provider):
    # type: (object) -> object
    """Hook that forwards a sandbox process provider into the swap half."""

    def hook(result, kwargs):
        kwargs.setdefault("process_provider", process_provider)

    return hook



class Sandbox(object):
    """One throwaway tree: fake profile, IDE data and DiskWarden data root."""

    def __init__(self, scale=None, build=True, root=None):
        self.scale = scale or choose_scale()
        if self.scale not in PROFILES:
            raise AssertionError("unknown scale %r" % (self.scale,))
        self.profile = PROFILES[self.scale]
        self.owns_root = not root
        if root:
            # Used by the kill-child process: re-attach to a sandbox the test
            # already built instead of creating a new tree.
            self.root = os.path.abspath(root)
            if not os.path.isdir(self.root):
                raise AssertionError("sandbox root is missing: %s" % self.root)
        else:
            self.root = tempfile.mkdtemp(prefix="dw-e2e-", dir=sandbox_base())
        self.appdata = os.path.join(self.root, "appdata", "Roaming")
        self.localappdata = os.path.join(self.root, "appdata", "Local")
        self.userprofile = os.path.join(self.root, "user")
        self.tool_root = os.path.join(self.appdata, "Cursor")
        self.cache_path = os.path.join(self.tool_root, "Cache")
        self.settings_path = os.path.join(self.tool_root, "User", "settings.json")
        self.exe_path = os.path.join(self.tool_root, "Cursor.exe")
        self.data_root = os.path.join(self.root, "DiskWarden")
        self.target_root = os.path.join(self.data_root, "data")
        self.log_path = os.path.join(log_dir(), "sandbox-%d.log" % os.getpid())
        for path in (
            self.appdata,
            self.localappdata,
            self.userprofile,
            self.tool_root,
            self.data_root,
        ):
            os.makedirs(path, exist_ok=True)
        DataLayout(self.data_root).ensure()
        if build:
            self.build_ide_tree()

    # -- fixture ---------------------------------------------------------

    def build_ide_tree(self):
        # type: () -> "Sandbox"
        profile = self.profile
        write_pattern(self.exe_path, 4096, "cursor-exe")

        # User profile: red settings file, global storage and workspace state.
        write_json(
            self.settings_path,
            {"editor.fontSize": 14, "workbench.colorTheme": "Dark+", "dw": "sentinel"},
        )
        write_pattern(
            os.path.join(self.tool_root, "User", "globalStorage", "state.vscdb"),
            256 * 1024,
            "global-storage",
        )
        for index in range(3):
            write_json(
                os.path.join(
                    self.tool_root, "User", "workspaceStorage", "ws-%d" % index,
                    "workspace.json",
                ),
                {"folder": "file:///C:/work/project-%d" % index},
            )
            write_pattern(
                os.path.join(
                    self.tool_root, "User", "workspaceStorage", "ws-%d" % index,
                    "state.vscdb",
                ),
                64 * 1024,
                "ws-state-%d" % index,
            )
        write_pattern(
            os.path.join(self.tool_root, "User", "workspaceStorage", "ws-0", "cache.pack"),
            128 * 1024,
            "pack-0",
        )

        # Other green directories that the catalog also reports but that this
        # suite does not migrate; they make the scan realistic.
        write_pattern(
            os.path.join(self.tool_root, "Code Cache", "js", "index.js"),
            512 * 1024,
            "code-cache",
        )
        write_pattern(
            os.path.join(self.tool_root, "GPUCache", "data_0"),
            2 * MiB,
            "gpu-cache",
        )
        write_pattern(
            os.path.join(self.tool_root, "CachedData", "abc123", "CachedData", "0"),
            1 * MiB,
            "cached-data",
        )
        for index in range(profile["log_files"]):
            write_text(
                os.path.join(self.tool_root, "logs", "main-%03d.log" % index),
                "2026-10-20T00:00:%02dZ renderer ready\n" % (index % 60),
            )

        # The migrated tree: a realistic IDE cache hierarchy with multi-GB
        # blobs, deep nesting and hundreds of small files.
        cache = self.cache_path
        for size, name in profile["blobs"]:
            write_pattern(os.path.join(cache, name), size, name)
        write_pattern(
            os.path.join(cache, "Code Cache", "js", "index-dir", "the-real-index"),
            profile["medium_size"],
            "real-index",
        )
        data_dir = os.path.join(cache, "Cache_Data")
        for index in range(profile["bulk_files"]):
            write_pattern(
                os.path.join(data_dir, "f_%06d" % index),
                profile["bulk_size"],
                "bulk-%d" % index,
            )
        deep = os.path.join(cache, "Cache_Data", "level1", "level2", "level3", "level4")
        for index in range(4):
            write_text(
                os.path.join(deep, "page-%d.json" % index),
                json.dumps({"page": index, "entries": list(range(20))}),
            )
        write_text(
            os.path.join(cache, "Cache_Data", "level1", "sentinel.txt"),
            "cache-sentinel-v1",
        )
        write_text(
            os.path.join(cache, "index-dir", "VERSION"),
            "2026.10.20",
        )
        empty = os.path.join(cache, "empty-dir")
        if not os.path.isdir(empty):
            os.makedirs(empty)
        return self

    # -- runtime ---------------------------------------------------------

    def environ(self):
        # type: () -> dict
        return {
            "APPDATA": self.appdata,
            "LOCALAPPDATA": self.localappdata,
            "USERPROFILE": self.userprofile,
            "HOME": self.userprofile,
            "TEMP": self.userprofile,
            "TMP": self.userprofile,
            "DISKWARDEN_DATA_ROOT": self.data_root,
        }

    def free_space(self, path=None):
        # type: (object) -> int
        return _free_bytes(path or self.root)

    def new_service(self, **overrides):
        # type: (object) -> AppService
        options = {
            "data_root": self.data_root,
            "catalog_path": CATALOG_PATH,
            "rules_path": RULES_PATH,
            "environ": self.environ(),
            "process_provider": lambda: [],
            "free_space_provider": self.free_space,
        }
        options.update(overrides)
        service = AppService(**options)
        set_completion_hook(provider_hook(options["process_provider"]))
        return service

    def layout(self):
        # type: () -> DataLayout
        return DataLayout(self.data_root)

    def log(self, message, **fields):
        # type: (str, object) -> None
        payload = {"at": datetime.now().isoformat(timespec="milliseconds"),
                   "scale": self.scale, "sandbox": self.root,
                   "message": message}
        payload.update(fields)
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def close(self):
        # type: () -> None
        if not self.owns_root:
            return
        if os.environ.get("DISKWARDEN_E2E_KEEP"):
            self.log("sandbox kept")
            return
        remove_tree(self.root)


def remove_tree(path):
    # type: (str) -> None
    """Delete a tree without following junctions into their targets."""
    if not os.path.exists(path):
        return
    for current, dirnames, filenames in os.walk(path, topdown=False):
        for name in dirnames:
            full = os.path.join(current, name)
            if _is_reparse(full):
                try:
                    os.rmdir(full)
                except OSError:
                    pass
    def onerror(function, target, info):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


# -- assertions ------------------------------------------------------------


def assert_inside_sandbox(sandbox, path, label="path"):
    # type: (Sandbox, str, str) -> None
    root = os.path.normcase(os.path.abspath(sandbox.root))
    candidate = os.path.normcase(os.path.abspath(path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise AssertionError("%s escapes the sandbox: %s" % (label, path))


def assert_data_equal(case, before, after, label="tree"):
    # type: (unittest.TestCase, dict, dict, str) -> None
    missing = sorted(set(before) - set(after))
    extra = sorted(set(after) - set(before))
    changed = sorted(
        path for path in set(before) & set(after) if before[path] != after[path]
    )
    if missing or extra or changed:
        detail = {
            "missing": missing[:10],
            "extra": extra[:10],
            "changed": changed[:10],
            "before": tree_totals(before),
            "after": tree_totals(after),
        }
        case.fail("%s differs after the operation: %r" % (label, detail))
    case.assertEqual(tree_totals(before), tree_totals(after))
    case.assertTrue(after, "%s is empty; the fixture did not survive" % label)


# -- app helpers -----------------------------------------------------------


def run_scan(case, service, timeout=300.0):
    # type: (unittest.TestCase, AppService, float) -> dict
    started = service.scan_start()
    job = service.runner.wait(started["job_id"], timeout=timeout)
    case.assertEqual(job.get("state"), "done", "scan job did not finish: %r" % (job,))
    return service.scan_result()


def find_item(snapshot, source_path):
    # type: (dict, str) -> dict
    wanted = os.path.normcase(os.path.abspath(source_path))
    for item in snapshot.get("items", []):
        if os.path.normcase(os.path.abspath(item["source_path"])) == wanted:
            return item
    raise AssertionError("no scan item for %s" % source_path)


def plan(case, service, item_ids):
    # type: (unittest.TestCase, AppService, list) -> dict
    payload = service.plan_preview(item_ids)
    case.assertTrue(payload.get("plan_id"), "plan was not created: %r" % (payload,))
    return payload


def start_migration(case, service, plan_payload, timeout=1800.0):
    # type: (unittest.TestCase, AppService, dict, float) -> dict
    confirmed = list(plan_payload.get("yellow_item_ids") or [])
    started = service.migrate_start(
        plan_payload["plan_id"], plan_payload["confirm_token"], confirmed
    )
    job = service.runner.wait(started["job_id"], timeout=timeout)
    return job


def start_rollback(case, service, job_id, timeout=1800.0, confirm_residual=False):
    # type: (unittest.TestCase, AppService, str, float, bool) -> dict
    preview = service.rollback_preview(job_id, confirm_residual_delete=confirm_residual)
    case.assertTrue(preview.get("can_proceed"), "rollback was blocked: %r" % (preview,))
    case.assertTrue(preview.get("plan_id"), "rollback plan missing: %r" % (preview,))
    started = service.rollback_start(preview["plan_id"], preview["confirm_token"])
    return service.runner.wait(started["job_id"], timeout=timeout)


def residual_rollback(case, service, job_id):
    # type: (unittest.TestCase, AppService, str) -> object
    """Finish a ``RESIDUAL_COPY`` rollback one layer below the bridge.

    DEF-01 (T13 report): ``AppService.rollback_preview`` assigns to
    ``RollbackRequest.confirm_residual_delete`` although ``RollbackRequest`` is
    a frozen dataclass, so the confirmed residual path raises
    ``FrozenInstanceError`` whenever it is reached through ``rollback.preview``
    — i.e. the user can never clean up the copy left by a crash that happened
    between "target ready" and "source renamed".  Until that is fixed this
    helper drives the engine directly with the same arguments the service
    worker passes, so the *engine* behaviour stays covered.
    """
    from dataclasses import replace

    from src.core.migrate.ops import MigrationOps
    from src.core.rollback import engine as rollback_engine

    _, request = service._rollback_request_for(job_id)
    request = replace(request, confirm_residual_delete=True)
    return rollback_engine.rollback(
        request,
        transfer=service._transfer,
        ops=MigrationOps(),
        process_provider=service._process_provider,
        free_space_provider=service._free_space_provider,
        jobs_dir=service.layout.jobs_dir,
        history_path=service.layout.history_path,
        learned_path=service.layout.learned_path,
    )


def migration_job_id(service, source_path):
    # type: (AppService, str) -> str
    wanted = os.path.normcase(os.path.abspath(source_path))
    for entry in service.journals.list_all():
        if entry.type == "migration" and os.path.normcase(
            os.path.abspath(entry.source)
        ) == wanted:
            return entry.job_id
    raise AssertionError("no migration journal for %s" % source_path)


def probe_read(path):
    # type: (str) -> bytes
    with open(path, "rb") as handle:
        return handle.read(64)


# -- invariants shared by every scenario -----------------------------------

RESIDUE_PREFIXES = (".dw-partial-", ".dwtombstone-", ".dw-restore-")


def residue_entries(*roots):
    # type: (str) -> list
    """Leftovers that must never survive a finished job (or a rollback)."""
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for current, dirnames, filenames in os.walk(root):
            for name in list(dirnames) + list(filenames):
                if name.startswith(RESIDUE_PREFIXES):
                    found.append(os.path.join(current, name))
    return found


def assert_no_residue(case, *roots):
    # type: (unittest.TestCase, str) -> None
    leftovers = residue_entries(*roots)
    case.assertFalse(leftovers, "temporary leftovers survived: %r" % (leftovers[:5],))


def assert_junction_to(case, link_path, target_path):
    # type: (unittest.TestCase, str, str) -> None
    case.assertTrue(is_junction(link_path), "%s is not a junction" % link_path)
    actual = read_junction_target(link_path)
    case.assertTrue(actual, "junction target could not be read: %s" % link_path)
    case.assertEqual(
        os.path.normcase(os.path.abspath(actual)),
        os.path.normcase(os.path.abspath(target_path)),
        "junction points somewhere else",
    )


def assert_real_dir(case, path, label="path"):
    # type: (unittest.TestCase, str, str) -> None
    case.assertTrue(os.path.isdir(path), "%s is not a directory: %s" % (label, path))
    case.assertFalse(_is_reparse(path), "%s is still a link: %s" % (label, path))


def history_record(service, job_id):
    # type: (AppService, str) -> dict
    for record in service.history_list(limit=500)["items"]:
        if record.get("job_id") == job_id:
            return record
    raise AssertionError("no history record for %s" % job_id)


def ide_still_usable(case, sandbox):
    # type: (unittest.TestCase, Sandbox) -> None
    """The profile must stay loadable after every scenario."""
    case.assertTrue(probe_read(sandbox.exe_path), "Cursor.exe vanished")
    with open(sandbox.settings_path, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
    case.assertEqual(settings.get("dw"), "sentinel", "settings.json was rewritten")
    case.assertGreaterEqual(tree_depth(sandbox.tool_root), 4, "profile tree collapsed")


# -- fault injection -------------------------------------------------------


class FakeProcess(object):
    """Minimal process record accepted by ``detect_processes``."""

    def __init__(self, pid, name, exe=""):
        # type: (int, str, str) -> None
        self.pid = int(pid)
        self.name = name
        self.exe = exe


def busy_process_provider(name="Cursor.exe"):
    # type: (str) -> object
    """Report the IDE as running, mimicking a user who left Cursor open."""
    return lambda: [FakeProcess(4242, name, r"C:\Program Files\Cursor\Cursor.exe")]


@contextlib.contextmanager
def deny_scandir(path):
    # type: (str) -> object
    """Make ``os.scandir`` raise ``PermissionError`` for exactly one directory.

    Windows ACL tricks do not stop an elevated process, so the "permission
    denied" scenario is injected at the syscall boundary the scanner uses.
    """
    import src.core.scan.size as size_module

    wanted = os.path.normcase(os.path.abspath(path))
    original = size_module.os.scandir

    def guarded(target="."):
        if os.path.normcase(os.path.abspath(target)) == wanted:
            raise PermissionError(13, "Access is denied", target)
        return original(target)

    size_module.os.scandir = guarded
    try:
        yield
    finally:
        size_module.os.scandir = original


def run_kill_child(case, sandbox, stage, timeout=3600.0):
    # type: (unittest.TestCase, Sandbox, str, float) -> None
    """Run one migration in a child process that dies at ``stage``.

    A real ``os._exit`` (not an exception) is what distinguishes this from a
    normal failure: the journal is left exactly as a power cut would leave it.
    """
    import subprocess

    state_file = os.path.join(sandbox.root, "kill-child-%s.json" % stage)
    command = [
        sys.executable,
        "-u",
        "-m",
        "tests.e2e.kill_child",
        "--root",
        sandbox.root,
        "--stage",
        stage,
        "--scale",
        sandbox.scale,
        "--state-file",
        state_file,
    ]
    environ = dict(os.environ)
    environ.update(sandbox.environ())
    environ["DISKWARDEN_E2E_SCALE"] = sandbox.scale
    sandbox.log("spawn kill-child", stage=stage, command=command)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    output = completed.stdout.decode("utf-8", "replace")
    sandbox.log("kill-child exited", stage=stage, code=completed.returncode,
                output=output[-4000:])
    case.assertEqual(
        completed.returncode,
        KILL_EXIT,
        "kill-child did not die at the %s boundary (exit %r): %s"
        % (stage, completed.returncode, output[-2000:]),
    )
    case.assertTrue(
        os.path.exists(state_file) and os.path.getsize(state_file) > 0,
        "kill-child did not record its plan",
    )


def robocopy_like_transfer(corrupt=None):
    # type: (object) -> object
    """A deterministic stand-in for robocopy (copy tree, optional corruption).

    ``corrupt`` receives ``(source_path, staging_path)`` after the copy so a
    scenario can sabotage the staging tree and exercise the verifier.
    """

    def transfer(source_path, staging_path, log_path, cancel_token=None):
        parent = os.path.dirname(staging_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.isdir(staging_path):
            shutil.rmtree(staging_path, ignore_errors=True)
        shutil.copytree(source_path, staging_path)
        with open(log_path or os.path.join(parent, "copy.log"), "w", encoding="utf-8") as handle:
            handle.write("stand-in transfer copied %s\n" % source_path)
        if corrupt is not None:
            corrupt(source_path, staging_path)
        return 1

    return transfer


# -- test base -------------------------------------------------------------


class SandboxTestCase(unittest.TestCase):
    """Base class: one fresh sandbox (and its log) per test."""

    scale = None

    def setUp(self):
        self.sandbox = Sandbox(scale=self.scale)
        self.addCleanup(self.sandbox.close)
        self.sandbox.log("%s.%s start" % (type(self).__name__, self._testMethodName))

    def service_for(self, **overrides):
        service = self.sandbox.new_service(**overrides)
        self.addCleanup(service.close)
        return service

    def log(self, message, **fields):
        self.sandbox.log(message, test=self._testMethodName, **fields)
