#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One command to run the whole end-to-end suite (T13).

    python tools/run_e2e.py                  # auto scale (real when the disk allows)
    python tools/run_e2e.py --scale=small    # quick loop
    python tools/run_e2e.py --case test_rollback
    python tools/run_e2e.py --keep           # leave the sandboxes for inspection

Every test builds its own throw-away profile below a temporary directory; the
real ``%APPDATA%``/``%LOCALAPPDATA%``/``%USERPROFILE%`` are never touched.

The full console output of the run is also written to ``tests/e2e/logs`` so a
failing case can be reproduced and handed over with its log:

    python tools/run_e2e.py            # -> tests/e2e/logs/run-<stamp>.log
    DISKWARDEN_E2E_KEEP=1 python -m unittest tests.e2e.test_rollback -v

Exit code 0 means every case passed, 1 means at least one failed.
"""

import argparse
import os
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PACKAGE = "tests.e2e"
LOG_DIR = os.path.join(ROOT, "tests", "e2e", "logs")


class Tee(object):
    """Duplicate unittest output to the console and to the run log."""

    def __init__(self, handle):
        self.handle = handle

    def write(self, text):
        sys.stdout.write(text)
        self.handle.write(text)
        return len(text)

    def flush(self):
        sys.stdout.flush()
        self.handle.flush()


def _open_log(scale):
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOG_DIR, "run-%s-%s.log" % (scale or "auto", stamp))
    return path, open(path, "w", encoding="utf-8")


def _discover(case):
    loader = unittest.TestLoader()
    if case:
        name = case if case.startswith(PACKAGE) else "%s.%s" % (PACKAGE, case)
        try:
            return loader.loadTestsFromName(name)
        except (ImportError, AttributeError) as exc:
            sys.stderr.write("cannot load %s: %s\n" % (name, exc))
            raise SystemExit(2)
    return loader.discover(
        os.path.join(ROOT, "tests", "e2e"),
        pattern="test_*.py",
        top_level_dir=ROOT,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the disk-warden end-to-end suite.")
    parser.add_argument(
        "--scale",
        choices=("real", "small"),
        help="fixture size; real uses multi-GB blobs when the disk allows",
    )
    parser.add_argument("--case", help="one module or test, e.g. test_rollback")
    parser.add_argument("--keep", action="store_true", help="keep sandboxes on disk")
    parser.add_argument(
        "--root", help="directory under which sandboxes are created (needs room)"
    )
    parser.add_argument("--failfast", action="store_true")
    args = parser.parse_args(argv)

    if args.scale:
        os.environ["DISKWARDEN_E2E_SCALE"] = args.scale
    if args.keep:
        os.environ["DISKWARDEN_E2E_KEEP"] = "1"
    if args.root:
        os.environ["DISKWARDEN_E2E_ROOT"] = args.root

    from tests.e2e import support  # imported late: it reads the environment

    scale = support.choose_scale()
    log_path, log_handle = _open_log(scale)
    os.environ["DISKWARDEN_E2E_LOGS"] = os.path.dirname(log_path)

    suite = _discover(args.case)
    runner = unittest.TextTestRunner(
        stream=Tee(log_handle), verbosity=2, failfast=args.failfast
    )
    started = time.time()
    try:
        result = runner.run(suite)
    finally:
        duration = time.time() - started
        summary = (
            "\nscale=%s tests=%d failures=%d errors=%d skipped=%d duration=%.1fs\n"
            "log=%s\n"
            % (
                scale,
                result.testsRun,
                len(result.failures),
                len(result.errors),
                len(result.skipped),
                duration,
                log_path,
            )
        )
        sys.stdout.write(summary)
        log_handle.write(summary)
        log_handle.flush()
        log_handle.close()

    # Point the reader at the deepest useful artefact: the per-test sandbox log.
    if not result.wasSuccessful():
        sys.stdout.write(
            "reproduce with: DISKWARDEN_E2E_SCALE=%s DISKWARDEN_E2E_KEEP=1 "
            "python -m unittest <failing.case> -v\n" % scale
        )
        for case, _tb in (result.failures + result.errors)[:3]:
            pattern = re.search(r"\(([\w.]+)\)", str(case))
            if pattern:
                sys.stdout.write("  -> %s\n" % pattern.group(1))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
