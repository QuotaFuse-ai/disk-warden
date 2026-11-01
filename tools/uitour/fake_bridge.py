# -*- coding: utf-8 -*-
"""Fake pywebview bridge, injected before the page's own scripts run.

``ui/index.html`` resolves ``window.pywebview.api.rpc`` at call time
(``function rpc()``) and ``hostReady()`` returns immediately when that object
is already present.  So a plain object installed by ``page.add_init_script``
is enough to drive the real UI in Chromium -- no product code change, no
packaged build.

The bridge must return a *Promise*: ``rpc()`` chains ``.then()`` on it.

Unknown methods never fall back to an empty result.  They resolve with an
``INTERNAL / fixture missing`` error, log the method name to the console, and
are collected on ``window.__uitour.missing`` so the tour can fail loudly
instead of producing a silent white page.
"""

import json

from . import fixtures


def fixtures_json():
    data = fixtures.fixtures()
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    # Keep a literal "</script>" inside data from closing the tag early.
    return text.replace("</", "<\\/")


#: Wall clock the page sees, pinned so countdowns / durations / file sizes in
#: screenshots stay byte-identical between runs.  2026-10-21T10:00:00Z.
FROZEN_NOW_MS = 1792576800000


def bridge_script():
    """Return the JavaScript source for the fake bridge."""
    return r"""
(function () {
  'use strict';
  /* 1. Freeze the clock.  ui/index.html only ever reads Date.now() (the auth
     countdown) and new Date() (a download filename), so pinning Date.now is
     enough to make the tour deterministic. */
  var FROZEN = __FROZEN__;
  Date.now = function () { return FROZEN; };

  var FIX = __FIXTURES__;
  var missing = [];
  var calls = [];
  window.__uitour = {
    version: '1.0',
    fixtures: FIX,
    missing: missing,
    calls: calls,
    node: window.__uitour ? window.__uitour.node : null
  };

  function nowIso(seconds) {
    var t = Date.now() + Math.round(seconds * 1000);
    return new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
  }

  /* Fixtures may carry "@now+600" placeholders; they are resolved per call so
     countdown UIs show a live but stable-looking value. */
  function resolve(value) {
    if (typeof value === 'string') {
      var m = /^@now([+-]\d+)?$/.exec(value);
      if (m) { return nowIso(m[1] ? Number(m[1]) : 0); }
      return value;
    }
    if (Array.isArray(value)) {
      var arr = [];
      for (var i = 0; i < value.length; i++) { arr.push(resolve(value[i])); }
      return arr;
    }
    if (value && typeof value === 'object') {
      var out = {};
      for (var k in value) {
        if (Object.prototype.hasOwnProperty.call(value, k)) { out[k] = resolve(value[k]); }
      }
      return out;
    }
    return value;
  }

  function envelope(id, body) {
    return JSON.stringify({ v: 1, id: id, ok: body.ok, result: body.result, error: body.error });
  }

  window.pywebview = {
    api: {
      rpc: function (raw) {
        var req = null;
        try { req = JSON.parse(raw); } catch (e) { req = null; }
        var id = req && req.id !== undefined ? req.id : null;
        var method = req && req.method ? String(req.method) : '';
        calls.push(method);
        if (!method || !Object.prototype.hasOwnProperty.call(FIX, method)) {
          var label = method || '(none)';
          missing.push(label);
          try {
            console.error('[uitour] fixture missing for rpc method: ' + label);
          } catch (e) { /* console may be unavailable, the array still records it */ }
          return Promise.resolve(envelope(id, {
            ok: false,
            error: {
              code: 'INTERNAL',
              message_key: 'error.INTERNAL',
              detail: { reason: 'fixture missing', method: label }
            }
          }));
        }
        return Promise.resolve(envelope(id, { ok: true, result: resolve(FIX[method]) }));
      }
    }
  };
})();
""".replace("__FIXTURES__", fixtures_json()).replace(
        "__FROZEN__", str(FROZEN_NOW_MS)
    )
