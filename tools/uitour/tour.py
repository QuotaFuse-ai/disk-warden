# -*- coding: utf-8 -*-
"""Screenshot tour: every page, at every width / theme / language.

The tour never touches product code.  Each shot is a fresh page load of the
*real* ``ui/index.html`` against a local static server, with two things
injected from the outside:

* the fake RPC bridge (``fake_bridge.py``), installed via
  ``add_init_script`` so it exists before the SPA's own script runs;
* for dark shots, ``dark.css``, injected after load (the product ships light
  only — see the caveat in that file's header).

Page state that the product only reaches through user clicks (scan in flight,
migration preview, rollback) is driven by assigning the SPA's own ``state``
from ``page.evaluate`` and calling ``render()``.  Action handlers are never
invoked: they start polling timers and would make the tour non-deterministic.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import fixtures
from .fake_bridge import bridge_script
from .server import StaticSite

TOOL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TOOL_ROOT.parent.parent
INDEX_PAGE = "ui/index.html"
DARK_CSS = TOOL_ROOT / "dark.css"

WIDTHS = (980, 1280, 1600)
HEIGHT = 900
THEMES = ("light", "dark")
LANGS = ("zh-CN", "en")

#: Console messages the tour treats as noise.  Keep this list *empty* unless a
#: message is demonstrably produced by the harness rather than the product.
CONSOLE_IGNORE = ()


class TourError(RuntimeError):
    pass


def _js(value):
    """Serialise a fixture fragment as a JS literal."""
    return json.dumps(value, ensure_ascii=False)


def _pages():
    """The page list.  Order = gallery order.

    ``setup`` runs once per shot, after boot, and must be side-effect free
    apart from touching the SPA's own globals.  ``hash`` is applied inside the
    setup so state is configured *before* the first render of that route.
    """
    gb = fixtures.GB
    plan = fixtures.plan_preview()
    rollback = fixtures.rollback_preview()
    scan_job = fixtures.active_scan_job()

    success_job = fixtures.finished_job()
    success_summary = {
        "ok": True,
        "failed": 0,
        "cancelled": False,
        "bytes": int(4.5 * gb),
        "items": [
            {
                "source_path": fixtures.HOME + "\\.cursor\\extensions",
                "target_path": "D:\\DiskWarden\\cursor\\extensions",
                "job_id": "jb_20261021_09",
                "ok": True,
            },
            {
                "source_path": fixtures.HOME + "\\AppData\\Roaming\\Cursor\\Cache",
                "target_path": "D:\\DiskWarden\\cursor\\Cache",
                "job_id": "jb_20261021_09",
                "ok": True,
            },
        ],
    }

    def migrate_flow(phase, **extra):
        flow = {
            "kind": "migrate",
            "phase": phase,
            "toolId": "cursor",
            "plan": plan,
            "yellowConfirmed": {"cursor-logs": True},
            "error": None,
            "errorScope": "",
            "jobId": "jb_20261021_09",
            "runnerJobId": "jb_20261021_09",
            "job": None,
            "summary": None,
        }
        flow.update(extra)
        return flow

    def rollback_flow(phase, **extra):
        flow = {
            "kind": "rollback",
            "phase": phase,
            "toolId": "vscode",
            "jobId": "jb_20261018_02",
            "preview": rollback,
            "error": None,
            "noticeKey": "",
            "job": None,
            "summary": None,
        }
        flow.update(extra)
        return flow

    pages = [
        {
            "name": "welcome",
            "title": "欢迎 / 登录",
            "group": "onboarding",
            "hash": "#projects",
            "note": "首次启动、未登录（welcome_seen=false）",
            "setup": "applyAuth(%s); render();"
            % _js(fixtures.auth(welcome_seen=False, signed_in=False)),
        },
        {
            "name": "welcome-device-flow",
            "title": "GitHub 设备码登录中",
            "group": "onboarding",
            "hash": "#projects",
            "note": "设备码 + 倒计时（时钟已冻结，截图可复现）",
            "setup": "applyAuth(%s); render();"
            % _js(
                fixtures.auth(
                    welcome_seen=False, signed_in=False, flow=fixtures.device_flow()
                )
            ),
        },
        {
            "name": "star-overlay",
            "title": "Star 提示浮层",
            "group": "onboarding",
            "hash": "#ide",
            "note": "半透明浮层，可随时关闭",
            "setup": "applyAuth(%s); location.hash = '#ide'; render();"
            % _js(fixtures.auth(star_pending=True)),
        },
        {
            "name": "projects",
            "title": "项目目录",
            "group": "main",
            "hash": "#projects",
            "note": "占位页（产品尚未实现）",
            "setup": "location.hash = '#projects'; render();",
        },
        {
            "name": "ide",
            "title": "IDE 数据",
            "group": "main",
            "hash": "#ide",
            "note": "扫描完成，绿/黄/红混排，含已迁移与超长路径",
            "setup": "location.hash = '#ide'; render();",
        },
        {
            "name": "ide-empty",
            "title": "IDE 数据 · 空状态",
            "group": "main",
            "hash": "#ide/empty",
            "note": "未发现可回收数据",
            "setup": "location.hash = '#ide/empty'; render();",
        },
        {
            "name": "ide-no-tools",
            "title": "IDE 数据 · 无已安装工具",
            "group": "edge",
            "hash": "#ide",
            "note": "扫描结果为空（tools/items 都是空数组）",
            "setup": "setSnapshot(%s); location.hash = '#ide'; render();"
            % _js(fixtures.empty_snapshot()),
        },
        {
            "name": "ide-scanning",
            "title": "扫描进行中",
            "group": "main",
            "hash": "#ide/scanning",
            "note": "阶段进度 + 骨架屏",
            "setup": "state.data.scanJob = %s; state.scanning = true;"
            " state.scanPct = scanPercent(state.data.scanJob);"
            " location.hash = '#ide/scanning'; render();" % _js(scan_job),
        },
        {
            "name": "ide-process-blocked",
            "title": "进程占用 · 阻断",
            "group": "edge",
            "hash": "#ide/process-blocked",
            "note": "Windsurf 正在运行，迁移被阻断",
            "setup": "state.selectedIde = 'windsurf';"
            " location.hash = '#ide/process-blocked'; render();",
        },
        {
            "name": "ide-yellow-confirm",
            "title": "黄色待确认",
            "group": "edge",
            "hash": "#ide/yellow-confirm",
            "note": "96 MB 日志目录，需人工确认",
            "setup": "(function () {"
            " var ide = getIde('cursor');"
            " var idx = (ide.directories || []).findIndex(function (d) {"
            " return String(d.path).indexOf('\\\\logs') >= 0; });"
            " state.pendingYellow = { ide: 'cursor', idx: idx < 0 ? 0 : idx };"
            " location.hash = '#ide/yellow-confirm'; render(); })();",
        },
        {
            "name": "disk",
            "title": "磁盘体检",
            "group": "main",
            "hash": "#disk",
            "note": "C: 438/512 GB，目标盘 D: 612 GB 可用",
            "setup": "location.hash = '#disk'; render();",
        },
        {
            "name": "rules",
            "title": "规则学习",
            "group": "main",
            "hash": "#rules",
            "note": "已学规则 + 需确认项 + 读取告警",
            "setup": "location.hash = '#rules'; render();",
        },
        {
            "name": "history",
            "title": "操作历史",
            "group": "main",
            "hash": "#history",
            "note": "成功 / 已回滚 / 失败（EACCES）三种结果",
            "setup": "location.hash = '#history'; render();",
        },
        {
            "name": "migrate-confirm",
            "title": "迁移 · 确认",
            "group": "flow",
            "hash": "#ide/migrate-preview",
            "note": "4 项计划，含 1 项黄色已确认",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/migrate-preview'; render();"
            % _js(migrate_flow("confirm")),
        },
        {
            "name": "migrate-progress",
            "title": "迁移 · 执行中",
            "group": "flow",
            "hash": "#ide/migrate-preview",
            "note": "第 2/4 项，copy 阶段",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/migrate-preview'; render();"
            % _js(
                migrate_flow(
                    "progress",
                    job={
                        "job_id": "jb_20261021_09",
                        "state": "running",
                        "started_at": "2026-10-21T10:02:11Z",
                        "finished_at": "2026-10-21T10:02:20Z",
                        "progress": {
                            "stage": "copy",
                            "items_total": 4,
                            "items_done": 2,
                            "index": 2,
                            "status": "running",
                            "done": False,
                        },
                    },
                )
            ),
        },
        {
            "name": "migrate-success",
            "title": "迁移 · 完成",
            "group": "flow",
            "hash": "#ide/migrate-preview",
            "note": "4.5 GB 已释放，可回滚",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/migrate-preview'; render();"
            % _js(
                migrate_flow(
                    "success", job=success_job, summary=success_summary
                )
            ),
        },
        {
            "name": "rollback-confirm",
            "title": "回滚 · 确认",
            "group": "flow",
            "hash": "#ide/rollback-confirm",
            "note": "VS Code 扩展 2.1 GB 迁回",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/rollback-confirm'; render();"
            % _js(rollback_flow("confirm")),
        },
        {
            "name": "rollback-progress",
            "title": "回滚 · 执行中",
            "group": "flow",
            "hash": "#ide/rollback-confirm",
            "note": "copy 阶段",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/rollback-confirm'; render();"
            % _js(
                rollback_flow(
                    "progress",
                    job={
                        "job_id": "jb_20261021_10",
                        "state": "running",
                        "started_at": "2026-10-21T10:06:00Z",
                        "finished_at": "2026-10-21T10:06:12Z",
                        "progress": {"stage": "copy", "done": False},
                    },
                )
            ),
        },
        {
            "name": "rollback-success",
            "title": "回滚 · 完成",
            "group": "flow",
            "hash": "#ide/rollback-confirm",
            "note": "原路径已恢复",
            "setup": "state.data.flow = %s;"
            " location.hash = '#ide/rollback-confirm'; render();"
            % _js(
                rollback_flow(
                    "success",
                    job={
                        "job_id": "jb_20261021_10",
                        "state": "done",
                        "started_at": "2026-10-21T10:06:00Z",
                        "finished_at": "2026-10-21T10:06:27Z",
                        "progress": {"stage": "done", "done": True},
                    },
                    summary={
                        "source_path": fixtures.HOME + "\\.vscode\\extensions",
                        "final_path": "D:\\DiskWarden\\vscode\\extensions",
                        "job_id": "jb_20261021_10",
                    },
                )
            ),
        },
        {
            "name": "settings",
            "title": "设置",
            "group": "modal",
            "hash": "#settings",
            "note": "目标盘、开机自启、AI 配置",
            "setup": "location.hash = '#settings'; render();",
        },
        {
            "name": "ai",
            "title": "AI 助手设置",
            "group": "modal",
            "hash": "#ai",
            "note": "DeepSeek 兼容端点，密钥已保存",
            "setup": "location.hash = '#ai'; render();",
        },
        {
            "name": "feedback",
            "title": "反馈",
            "group": "modal",
            "hash": "#feedback",
            "note": "附带诊断信息",
            "setup": "location.hash = '#feedback'; render();",
        },
    ]
    return pages


PAGES = _pages()


def _shot_name(page_name, width, theme, lang):
    return "%s__%d__%s__%s.png" % (page_name, width, theme, lang)


_BOOT_JS = (
    "() => { try { return !!(typeof state !== 'undefined' && state.connected"
    " && !state.booting && state.data && !state.data.historyLoading); }"
    " catch (e) { return false; } }"
)


def _settle(page):
    """Let layout, fonts and the after-render rAF pass finish."""
    page.evaluate(
        "() => new Promise(function (done) {"
        "  var f = (document.fonts && document.fonts.ready) || null;"
        "  var go = function () { requestAnimationFrame(function () {"
        "    requestAnimationFrame(done); }); };"
        "  if (f && f.then) { f.then(go, go); } else { go(); }"
        "});"
    )


def run(
    out_dir,
    widths=WIDTHS,
    themes=THEMES,
    langs=LANGS,
    only=None,
    fail_on_console_error=True,
    echo=None,
):
    """Render the matrix and return ``(shots, problems)``.

    ``shots`` is a list of plain dicts (see ``gallery.py`` for the fields).
    ``problems`` is a list of human-readable failure strings; it is empty only
    when every shot rendered cleanly.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - dev/CI only dependency
        raise TourError(
            "Playwright is not installed.  It is a CI/dev-only dependency:\n"
            "    pip install -r packaging/requirements-ci.txt\n"
            "    python -m playwright install chromium"
        )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    pages = [p for p in PAGES if not only or p["name"] in only]
    if not pages:
        raise TourError("no pages selected; check --only")

    shots = []
    problems = []
    browser_script = bridge_script()
    started = time.time()

    with StaticSite() as site:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for lang in langs:
                    for theme in themes:
                        for width in widths:
                            ctx = browser.new_context(
                                viewport={"width": width, "height": HEIGHT},
                                device_scale_factor=1,
                                reduced_motion="reduce",
                                color_scheme="light",
                                locale="zh-CN" if lang == "zh-CN" else "en-US",
                            )
                            ctx.add_init_script(script=browser_script)
                            try:
                                for spec in pages:
                                    shot, issues = _one(
                                        ctx,
                                        site.base_url,
                                        spec,
                                        width,
                                        theme,
                                        lang,
                                        out,
                                    )
                                    shots.append(shot)
                                    problems.extend(issues)
                                    if echo:
                                        echo(
                                            "  %-58s %s"
                                            % (
                                                shot["file"],
                                                "OK" if not issues else "FAIL",
                                            )
                                        )
                            finally:
                                ctx.close()
            finally:
                browser.close()

    elapsed = time.time() - started
    if echo:
        echo(
            "\n%d shots in %.1fs -> %s"
            % (len(shots), elapsed, out)
        )

    if fail_on_console_error:
        noisy = [s for s in shots if s["console_errors"]]
        if noisy:
            problems.append(
                "%d shot(s) logged console errors; see uitour/index.html"
                % len(noisy)
            )
    else:
        problems = [p for p in problems if "console" not in p]

    return shots, problems


def _one(ctx, base_url, spec, width, theme, lang, out):
    """Load one page at one matrix point, screenshot it, report problems."""
    console_errors = []
    file_name = _shot_name(spec["name"], width, theme, lang)
    page = ctx.new_page()
    try:
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text)
            if msg.type == "error" and not _ignored(msg.text)
            else None,
        )
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        url = "%s/%s?lang=%s#projects" % (base_url, INDEX_PAGE, lang)
        page.goto(url, wait_until="load")
        try:
            page.wait_for_function(_BOOT_JS, timeout=15000)
        except Exception:
            console_errors.append("boot did not complete (state.connected never true)")

        if theme == "dark":
            page.add_style_tag(path=str(DARK_CSS))

        page.evaluate(spec["setup"])
        _settle(page)

        missing = list(
            dict.fromkeys(page.evaluate("() => (window.__uitour.missing || []).slice()"))
        )
        page.screenshot(path=str(out / file_name), full_page=False)
    finally:
        page.close()

    issues = []
    if missing:
        issues.append(
            "%s: rpc method(s) without a fixture: %s"
            % (file_name, ", ".join(missing))
        )
    for text in console_errors:
        issues.append("%s: console error: %s" % (file_name, text[:200]))

    return (
        {
            "page": spec["name"],
            "title": spec["title"],
            "group": spec["group"],
            "note": spec["note"],
            "width": width,
            "theme": theme,
            "lang": lang,
            "height": HEIGHT,
            "file": file_name,
            "console_errors": len(console_errors),
            "missing": missing,
        },
        issues,
    )


def _ignored(text):
    for needle in CONSOLE_IGNORE:
        if needle in text:
            return True
    return False


def main(out_dir="dist/uitour", **kwargs):  # pragma: no cover - convenience
    shots, problems = run(out_dir, echo=lambda s: print(s, flush=True), **kwargs)
    return shots, problems


if __name__ == "__main__":  # pragma: no cover
    shots, problems = main(sys.argv[1] if len(sys.argv) > 1 else "dist/uitour")
    for p in problems:
        print("PROBLEM: " + p)
    raise SystemExit(1 if problems else 0)
