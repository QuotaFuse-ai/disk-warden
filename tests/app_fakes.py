# -*- coding: utf-8 -*-
"""Shared fixtures for the ``src.app`` tests (T12 batch 3).

``AppService`` is wired to a temporary data root and to injected process and
free-space providers, so no test touches real user directories or the real
process table.
"""

import os
import tempfile

from src.app.service import AppService

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(REPO_ROOT, "data", "ide-catalog.json")
RULES_PATH = os.path.join(REPO_ROOT, "data", "rules.json")

SOURCE_CACHE = r"C:\Users\alice\AppData\Roaming\Cursor\Cache"
FOUR_GIB = 4 * 1024 ** 3


class FakeProcess(object):
    """Minimal process row accepted by ``detect_processes``."""

    def __init__(self, pid, name, exe=""):
        self.pid = pid
        self.name = name
        self.exe = exe


def make_service(
    case,
    environ=None,
    process_provider=None,
    free_space_provider=None,
    auth_opener=None,
    auth_http_opener=None,
    auth_provider_loader=None,
):
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    service = AppService(
        data_root=temp.name,
        catalog_path=CATALOG_PATH,
        rules_path=RULES_PATH,
        environ=dict(environ or {}),
        process_provider=process_provider or (lambda: []),
        free_space_provider=free_space_provider or (lambda path: FOUR_GIB),
        auth_opener=auth_opener,
        auth_http_opener=auth_http_opener,
        auth_provider_loader=auth_provider_loader,
    )
    case.addCleanup(service.close)
    return service


def make_item(
    item_id="cursor-cache",
    tool_id="cursor",
    safety="green",
    source_path=SOURCE_CACHE,
    **overrides
):
    item = {
        "item_id": item_id,
        "tool_id": tool_id,
        "tool_name": tool_id,
        "source_path": source_path,
        "rel_path": os.path.basename(source_path),
        "kind": "dir",
        "bytes": 1024 * 1024,
        "file_count": 10,
        "is_reparse": False,
        "access_errors": 0,
        "mtime_max": "",
        "target_path": "",
        "target_exists": False,
        "safety": safety,
        "source": "catalog",
        "rule_id": "",
        "reason": "",
        "reason_key": "",
        "requires_confirm": False,
        "can_migrate": safety != "red",
        "blocked_code": "",
        "blocked_reason": "",
        "process": None,
        "migrated_job_id": "",
    }
    item.update(overrides)
    return item


def set_snapshot(service, items):
    service._snapshot = {"items": list(items), "tools": [], "stats": {}}
