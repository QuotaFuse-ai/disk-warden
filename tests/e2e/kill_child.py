# -*- coding: utf-8 -*-
"""Hard-interrupt driver for the crash-recovery matrix (T13).

The parent test starts a *real* migration in this process and this process
kills itself with ``os._exit`` at a chosen stage boundary, exactly like a user
closing the window or pulling the plug: no ``finally`` blocks, no flushed
buffers, no graceful shutdown.  What survives is whatever already reached the
journal file and the filesystem.

Stages (all inside ``complete_migration``):

``swap``
    immediately before the S6 rename of the source to its tombstone, so the
    promoted target and the untouched source both exist;
``link``
    right after S6 (source is a tombstone, no junction yet);
``link_verify``
    right after S7 (junction exists, not yet verified);
``cleanup``
    right after S8 (junction verified, tombstone still present).

The migration runs through the same ``AppService`` path the UI uses; only
``complete_migration`` is wrapped to pass a kill-aware ``MigrationOps`` and a
kill-aware progress callback.  Run standalone::

    python -m tests.e2e.kill_child --root <sandbox-root> --stage link_verify
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.e2e import support  # noqa: E402

KILL_EXIT = support.KILL_EXIT

STAGES = ("swap", "link", "link_verify", "cleanup")

# Boundary that must be observed in the progress stream before this stage is
# interrupted: the process dies on the *done* event of the stage before it.
_BOUNDARIES = {
    "link": ("swap", "done"),
    "link_verify": ("link", "done"),
    "cleanup": ("link_verify", "done"),
}


def _die(stage, marker):
    # type: (str, str) -> None
    """Terminate without unwinding: this is the simulated power loss."""
    message = "kill-child: hard exit at %s (%s)" % (stage, marker)
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(KILL_EXIT)


def _killing_ops(stage, job_id):
    # type: (str, str) -> object
    from src.core.migrate.ops import MigrationOps

    class KillingOps(MigrationOps):
        """Interrupts between S5 and S6, i.e. before the source is renamed."""

        def rename(self, source, destination):
            if stage == "swap" and _looks_like_tombstone(destination, job_id):
                _die(stage, "before S6 rename")
            return MigrationOps.rename(self, source, destination)

    return KillingOps()


def _looks_like_tombstone(path, job_id):
    # type: (str, str) -> bool
    name = os.path.basename(os.path.normpath(path))
    if not name.startswith(".dwtombstone-"):
        return False
    prefix = job_id.replace("-", "")[:8]
    return not prefix or name.endswith(prefix)


def _killing_callback(stage, callback):
    # type: (str, object) -> object
    if stage not in _BOUNDARIES:
        return callback
    wanted_stage, wanted_status = _BOUNDARIES[stage]

    def wrapped(payload):
        if callback is not None:
            callback(payload)
        data = dict(payload or {})
        if data.get("stage") == wanted_stage and data.get("status") == wanted_status:
            _die(stage, "%s/%s" % (wanted_stage, wanted_status))

    return wrapped


def _patch_complete_migration(stage):
    # type: (str) -> None
    """Compose the killing seam on top of the sandbox's provider seam."""
    previous = support.completion_hook()

    def hook(result, kwargs):
        if previous is not None:
            previous(result, kwargs)
        kwargs["ops"] = _killing_ops(stage, getattr(result, "job_id", "") or "")
        kwargs["progress_cb"] = _killing_callback(stage, kwargs.get("progress_cb"))

    support.set_completion_hook(hook)


def main(argv=None):
    # type: (object) -> int
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="existing sandbox root")
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--scale", default="small", choices=sorted(support.PROFILES))
    parser.add_argument("--state-file", default="", help="where to record the plan")
    args = parser.parse_args(argv)

    sandbox = support.Sandbox(scale=args.scale, build=False, root=args.root)
    os.environ.update(sandbox.environ())
    state_file = args.state_file or os.path.join(sandbox.root, "kill-child-state.json")

    service = sandbox.new_service()
    try:
        snapshot = support.run_scan(_TestShim(), service, timeout=900.0)
        item = support.find_item(snapshot, sandbox.cache_path)
        plan_payload = service.plan_preview([item["item_id"]])
        with open(state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "stage": args.stage,
                    "item_id": item["item_id"],
                    "source_path": sandbox.cache_path,
                    "plan_id": plan_payload["plan_id"],
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())

        _patch_complete_migration(args.stage)
        started = service.migrate_start(
            plan_payload["plan_id"],
            plan_payload["confirm_token"],
            list(plan_payload.get("yellow_item_ids") or []),
        )
        job = service.runner.wait(started["job_id"], timeout=1800.0)
        # Reaching this line means the kill point was never hit: report the
        # failure loudly so the test does not silently pass on a full run.
        sys.stdout.write(
            "kill-child: no kill for stage %s; migration ended as %r\n"
            % (args.stage, job.get("state"))
        )
        sys.stdout.flush()
        return 3
    finally:
        service.close()


class _TestShim(object):
    """Minimal ``unittest.TestCase`` stand-in for the support helpers."""

    def assertEqual(self, left, right, message=None):
        if left != right:
            raise AssertionError(message or "%r != %r" % (left, right))

    def assertTrue(self, value, message=None):
        if not value:
            raise AssertionError(message or "expected a truthy value")

    def fail(self, message=None):
        raise AssertionError(message or "failure")


if __name__ == "__main__":
    sys.exit(main())
