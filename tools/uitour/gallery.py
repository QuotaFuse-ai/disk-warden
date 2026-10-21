# -*- coding: utf-8 -*-
"""Static gallery + CI step summary for the uitour screenshots.

Two consumers:

* ``write_gallery`` — ``dist/uitour/index.html``, a filterable thumbnail grid.
  Reviewers open it after a local run; it also gets uploaded as the CI
  artifact, so a PR reviewer can download once and skim everything.
* ``summary_markdown`` — a handful of images inlined as base64 for
  ``$GITHUB_STEP_SUMMARY``.  GitHub renders the job summary without needing to
  download the artifact, which is the whole point of the exercise.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

_GROUPS = {
    "onboarding": "登录 / 欢迎",
    "main": "主流程",
    "edge": "边界状态",
    "flow": "迁移 / 回滚",
    "modal": "弹窗",
}


def _size_label(shot):
    return "%d×%d" % (shot["width"], shot["height"])


def write_gallery(out_dir, shots, meta=None):
    """Write ``<out_dir>/index.html`` and return its path."""
    out = Path(out_dir)
    meta = meta or {}
    pages = list(dict.fromkeys(s["page"] for s in shots))
    widths = sorted({s["width"] for s in shots})
    themes = [t for t in ("light", "dark") if any(s["theme"] == t for s in shots)]
    langs = [l for l in ("zh-CN", "en") if any(s["lang"] == l for s in shots)]
    groups = [g for g in _GROUPS if any(s["group"] == g for s in shots)]

    cards = []
    for s in shots:
        bad = s["console_errors"] or s["missing"]
        badge = (
            '<span class="badge bad">%d 错误</span>' % s["console_errors"]
            if s["console_errors"]
            else ""
        )
        if s["missing"]:
            badge += ' <span class="badge bad">fixture 缺失</span>'
        cards.append(
            '<figure class="card" data-page="{page}" data-theme="{theme}"'
            ' data-lang="{lang}" data-width="{width}">'
            '<a href="{file}" target="_blank" rel="noopener">'
            '<img loading="lazy" src="{file}" alt="{alt}"></a>'
            '<figcaption><div class="cap-top"><b>{title}</b>{badge}</div>'
            '<div class="cap-meta"><code>{page}</code>'
            " · {size} · {theme} · {lang}</div>"
            '<div class="cap-note">{note}</div></figcaption></figure>'.format(
                page=html.escape(s["page"]),
                theme=s["theme"],
                lang=s["lang"],
                width=s["width"],
                file=html.escape(s["file"]),
                alt=html.escape("%s %s %s" % (s["title"], s["theme"], s["lang"])),
                title=html.escape(s["title"]),
                badge=badge,
                size=_size_label(s),
                note=html.escape(s.get("note", "")),
            )
        )

    def chips(name, values):
        out_html = [
            '<button type="button" class="chip on" data-k="%s" data-v="">全部</button>'
            % name
        ]
        for v in values:
            out_html.append(
                '<button type="button" class="chip" data-k="%s" data-v="%s">%s</button>'
                % (name, v, html.escape(str(v)))
            )
        return '<div class="chips" data-group="%s">%s</div>' % (name, "".join(out_html))

    failed = [s for s in shots if s["console_errors"] or s["missing"]]
    summary_line = (
        '<span class="ok">全部 %d 张截图，控制台错误 0</span>' % len(shots)
        if not failed
        else '<span class="bad">%d 张截图有问题</span>' % len(failed)
    )

    doc = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disk Warden · UI 截图巡检</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 28px 32px 64px; background: #f4f6f7; color: #2f3439;
  font: 14px/1.55 "Microsoft YaHei UI", "Microsoft YaHei", -apple-system, "Segoe UI", sans-serif; }}
header {{ max-width: 1560px; margin: 0 auto 18px; }}
h1 {{ margin: 0 0 6px; font-size: 22px; }}
.sub {{ color: #5b646d; }}
.sub code {{ background: #e9edf0; padding: 1px 5px; border-radius: 3px; }}
.ok {{ color: #2f8e35; font-weight: 600; }}
.bad {{ color: #c92f36; font-weight: 600; }}
.controls {{ max-width: 1560px; margin: 0 auto 22px; display: flex; flex-wrap: wrap; gap: 14px;
  align-items: center; }}
.chips {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.chip {{ border: 1px solid #d7dee4; background: #fff; color: #4a545e; border-radius: 999px;
  padding: 4px 12px; font-size: 12px; cursor: pointer; }}
.chip.on {{ background: #3fbf44; border-color: #3fbf44; color: #fff; }}
.grid {{ max-width: 1560px; margin: 0 auto; display: grid; gap: 20px;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }}
.card {{ margin: 0; background: #fff; border: 1px solid #e3e7eb; border-radius: 6px; overflow: hidden;
  display: flex; flex-direction: column; }}
.card img {{ display: block; width: 100%; height: auto; background: #eef2f5; }}
figcaption {{ padding: 10px 12px 12px; }}
.cap-top {{ display: flex; align-items: center; gap: 8px; }}
.cap-meta {{ color: #7d8791; font-size: 12px; margin-top: 2px; }}
.cap-note {{ color: #5b646d; font-size: 12px; margin-top: 4px; }}
.badge {{ font-size: 11px; padding: 1px 6px; border-radius: 3px; }}
.badge.bad {{ background: #fff0ef; color: #d9363e; border: 1px solid #f2c9c6; }}
.hidden {{ display: none !important; }}
footer {{ max-width: 1560px; margin: 28px auto 0; color: #7d8791; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <h1>Disk Warden · UI 截图巡检</h1>
  <div class="sub">由 <code>python -m tools.uitour</code> 生成 · {count} 张 · {pages} 个页面 ·
  {widths} 个宽度 × {themes} 个主题 × {langs} 个语言 · {status}</div>
</header>
<div class="controls">
  {page_chips}
  {theme_chips}
  {lang_chips}
  {width_chips}
</div>
<div class="grid">
{cards}
</div>
<footer>
  截图来自未打包的 <code>ui/index.html</code> + 假 RPC 桥，不含真实磁盘数据。
  深色主题是预览皮肤（产品当前只发行浅色），仅用于视觉对比。
  {meta}
</footer>
<script>
(function () {{
  var filters = {{}};
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  document.querySelectorAll('.chips').forEach(function (group) {{
    var key = group.getAttribute('data-group');
    group.addEventListener('click', function (ev) {{
      var btn = ev.target.closest('.chip');
      if (!btn) return;
      group.querySelectorAll('.chip').forEach(function (b) {{ b.classList.remove('on'); }});
      btn.classList.add('on');
      filters[key] = btn.getAttribute('data-v');
      apply();
    }});
  }});
  function apply() {{
    cards.forEach(function (card) {{
      var show = true;
      Object.keys(filters).forEach(function (key) {{
        var want = filters[key];
        if (want && card.getAttribute('data-' + key) !== want) show = false;
      }});
      card.classList.toggle('hidden', !show);
    }});
  }}
}})();
</script>
</body>
</html>
""".format(
        count=len(shots),
        pages=len(pages),
        widths=len(widths),
        themes=len(themes),
        langs=len(langs),
        status=summary_line,
        page_chips=chips("page", pages),
        theme_chips=chips("theme", themes),
        lang_chips=chips("lang", langs),
        width_chips=chips("width", widths),
        cards="\n".join(cards),
        meta=html.escape(meta.get("note", "")),
    )
    target = out / "index.html"
    target.write_text(doc, encoding="utf-8")
    return target


def summary_markdown(shots, out_dir, picks, title="UI 截图巡检"):
    """Markdown for ``$GITHUB_STEP_SUMMARY`` with a few images inlined.

    ``picks`` is a list of ``(page_name, width, theme, lang)``; missing combos
    are skipped silently so the summary still renders if the matrix narrows.
    """
    out = Path(out_dir)
    index = {(s["page"], s["width"], s["theme"], s["lang"]): s for s in shots}
    lines = ["## " + title, ""]
    shown = 0
    for page, width, theme, lang in picks:
        shot = index.get((page, width, theme, lang))
        if not shot:
            continue
        path = out / shot["file"]
        if not path.exists():
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        lines.append("**%s** · %dpx · %s · %s" % (shot["title"], width, theme, lang))
        lines.append("")
        lines.append('<img src="data:image/png;base64,%s" width="%d" alt="%s">' % (
            data, width, html.escape(shot["title"])))
        lines.append("")
        shown += 1
    lines.append("")
    lines.append(
        "共 %d 张截图（%d 个页面 × %d 个宽度 × 亮/暗 × 中/英）。"
        "完整矩阵见构建产物 <code>ui-tour</code> 中的 <code>index.html</code>。"
        % (len(shots), len({s["page"] for s in shots}), len({s["width"] for s in shots}))
    )
    lines.append("")
    if not shown:
        lines.append("_没有可内嵌的截图，请查看构建产物。_")
    return "\n".join(lines)


def write_manifest(out_dir, shots, problems):
    """Machine-readable run record, handy for diffing two runs in review."""
    out = Path(out_dir)
    payload = {
        "tool": "uitour",
        "shots": shots,
        "problems": problems,
    }
    target = out / "manifest.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
