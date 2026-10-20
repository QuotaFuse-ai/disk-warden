# -*- coding: utf-8 -*-
"""T26 PoC: reproduce R-01 (junction injection) and R-02 (size-only verify).

Run from the repository root:

    python docs/tasks/T26/poc_t26.py

On the baseline ``9118bf4`` the two lines that matter read:

    R01 target matches requested path: False        # junction made at ...\\targets\\a
    R01 target data reachable through link: False
    R02 same size, different bytes -> verify ok: True

On the fixed tree they read:

    R01 target matches requested path: True
    R01 target data reachable through link: True
    R02 same size, different bytes -> verify ok: False   # 1 file(s) differ in content

The script only touches a temporary directory and removes it afterwards.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from src.core.migrate.verify import verify_trees  # noqa: E402
from src.platform.win.link import (  # noqa: E402
    create_junction,
    read_junction_target,
    remove_junction,
)

root = tempfile.mkdtemp(prefix="dw-t26-poc-")
try:
    os.makedirs(os.path.join(root, "targets"))
    os.makedirs(os.path.join(root, "links"))

    # R-01: the requested target contains "&"; its "&"-truncated prefix also
    # exists, which is what ``cmd /c mklink`` silently linked to before.
    target = os.path.join(root, "targets", "a&ver&rem")
    os.makedirs(target)
    os.makedirs(os.path.join(root, "targets", "a"))
    with open(os.path.join(target, "data.bin"), "wb") as handle:
        handle.write(b"x" * 32)

    link = os.path.join(root, "links", "plain-link")
    try:
        create_junction(link, target)
    except Exception as exc:  # noqa: BLE001 - the PoC prints what happens
        print("R01 create_junction raised: %s: %s" % (type(exc).__name__, exc))
    else:
        actual = read_junction_target(link)
        matches = os.path.normcase(actual or "") == os.path.normcase(target)
        reachable = os.path.exists(os.path.join(link, "data.bin"))
        print("R01 create_junction returned without error")
        print("R01 requested link exists: %s" % os.path.lexists(link))
        print(
            "R01 entries in links dir: %r" % os.listdir(os.path.dirname(link))
        )
        print("R01 read_junction_target: %r" % actual)
        print("R01 target matches requested path: %s" % matches)
        print("R01 target data reachable through link: %s" % reachable)
        try:
            remove_junction(link, target)
        except Exception as exc:  # noqa: BLE001
            print("R01 remove_junction raised: %s" % exc)

    # R-02: identical size, different bytes.
    source = os.path.join(root, "src")
    other = os.path.join(root, "dst")
    os.makedirs(source)
    os.makedirs(other)
    with open(os.path.join(source, "a.bin"), "wb") as handle:
        handle.write(b"A" * 4096)
    with open(os.path.join(other, "a.bin"), "wb") as handle:
        handle.write(b"B" * 4096)
    result = verify_trees(source, other)
    print("R02 same size, different bytes -> verify ok: %s" % result.ok)
    print("R02 reason: %s" % result.reason)
finally:
    shutil.rmtree(root, ignore_errors=True)
