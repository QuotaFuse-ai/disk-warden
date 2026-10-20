# -*- coding: utf-8 -*-
"""End-to-end and regression suite (T13).

Every case runs inside a throwaway sandbox tree created under a temporary
directory; the real ``%APPDATA%``/``%LOCALAPPDATA%`` trees are never read or
written.  Run everything with::

    python tools/run_e2e.py

or a single module with::

    python -m unittest tests.e2e.test_scan -v
"""
