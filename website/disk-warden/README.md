# Disk Warden 产品单页 — 本地原型

纯静态原型，无构建步骤、无 JavaScript、无外部字体 / CDN / 统计脚本。双击 `index.html` 即可打开；离线可用（GitHub 等外链需要联网才会跳转）。

```
website/disk-warden/
├── index.html                       英文（默认）
├── zh-Hans/index.html               中文
├── assets/style.css                 全站唯一样式表
├── assets/ide-overview-en.png       真实界面截图（英文，2880×1800）
├── assets/ide-overview-zh.png       真实界面截图（中文，2880×1800）
└── README.md
```

目标 URL 约定（与官网现有结构一致）：`https://www.quotafuse.com/disk-warden/`，中文为 `.../disk-warden/zh-Hans/`。语言切换是普通相对链接，不依赖服务器改写。

## 如何嵌入官网

1. 将整个 `disk-warden/` 目录放到站点根目录，保持 `index.html`、`zh-Hans/index.html`、`assets/` 的相对位置不变。
2. 静态托管（Nginx / OSS / Cloudflare Pages 等）无需任何配置；若官网是 Next.js / Nuxt / Astro，推荐把这三个文件原样放进 `public/disk-warden/`，或按现有页面组件重写（样式类名依赖 `assets/style.css`，需整表引入）。
3. 在 QuotaFuse 首页加入口链接，并把两个 URL 加进 sitemap。
4. 正式域名确定后，在两页 `<head>` 补绝对地址的 canonical 与 hreflang（原型刻意没写死域名）：

```html
<link rel="canonical" href="https://www.quotafuse.com/disk-warden/">
<link rel="alternate" hreflang="en" href="https://www.quotafuse.com/disk-warden/">
<link rel="alternate" hreflang="zh-Hans" href="https://www.quotafuse.com/disk-warden/zh-Hans/">
<link rel="alternate" hreflang="x-default" href="https://www.quotafuse.com/disk-warden/">
```

## 真实界面截图

`#interface` 一节用的是产品真实界面的截图，不是 HTML/CSS 画的示意图。

- **截的是什么**：`main` 上 `ui/index.html`（0.9.0-beta.10 随包发布的同一份单文件界面）里的「IDE 数据」页 —— 左侧 5 个工具、右侧 Cursor 的 7 个目录（4 可迁 / 1 未知 / 2 不可动）与迁移前后的空间数字。右上角版本号徽标为 `v0.9.0-beta.10`。
- **怎么截的**：无头 Chrome（`--headless=new --force-device-scale-factor=2 --window-size=1440,900`）加载 `ui/index.html` 的一份副本，副本在主 `<script>` 前插入一个 mock 的 pywebview bridge（`window.pywebview.api.rpc` 返回 Promise，结构与真实 RPC 同构：`app.hello` / `scan.result` / `disk.info` / `history.list` 等），再按 `?lang=en#ide` / `?lang=zh-CN#ide` 分别截图。`ui/`、`src/`、`main.py` 一行都没改，mock 脚本留在仓库外。
- **为什么不是 `python -m src.app.main`**：本构建机 Python 3.8.6、无 pywebview / WebView2 依赖，桌面应用起不来。改为直接渲染随包发布的界面文件，UI 与真实运行时是同一份。
- **规格**：截图为 2× DPR（2880×1800），量化 256 色后 en 154 KB / zh 154 KB，均在 300 KB 以内；页面里以 `width="1440" height="900"`、`loading="lazy"`、`decoding="async"` 引用，并写了描述性 `alt`。
- **隐私清洗**：目录路径统一为 `C:\Users\you\...`、`D:\DiskWarden\...`，不含任何真实用户名、真实路径或真实项目名；数字为示例数据，图注已写明「Folder names and sizes are sample data / 目录名与大小为示例数据」。
- **换成真机截图**：若之后要在真机上重截，覆盖同名文件即可，保持 1440×900 比例（2× 更佳）、压到 300 KB 以内，中英文各自对应；`alt` 与图注里的界面描述需随之核对。

## 与产品同步（2026-11-01 重校）

页面文案以 `README.md`、`docs/release-notes/Release-Notes-0.9.0-beta.10.md`、`docs/specs/产品规格-v1.0.md` 与 `src/app/auth/config.py` 为准。发版时需要同步改的位置：

| 位置 | 当前内容 | 发版时改成 |
| --- | --- | --- |
| `status-card` × 2、`download-sub`、图注 | `0.9.0-beta.10` | 新版本号（共 4 处） |
| 包名与字节数 | `DiskWarden-0.9.0-beta.10-{win-x64,macos-arm64}.zip` | 新 release notes 的表格 |
| SHA-256（两条） | 取自 release notes | 新包的同名 `.sha256` |
| 页头 logo、favicon | 内联 SVG 盾牌占位 | 官方品牌 SVG；同步替换 `index.html`、`zh-Hans/index.html` 的 favicon data URI |
| OG / 社交预览 | 仅有标题与描述 | 补 `og:image`（1200×630）、`og:url` 与最终绝对 URL |
| 问题区数字 | 三份公开报告（Claude Code #16093、#28235，Cursor 论坛帖） | 保留出处链接；上线前复查链接是否仍可访问 |

下载 CTA 指向 `https://github.com/QuotaFuse-ai/disk-warden/releases/latest`，与 `README.md` 的口径一致；macOS 卡片必须保留「未在真机验证」与「目录自动发现仍只支持 Windows」两条限制，不要在后续改版时被顺手删掉。

## 验证记录（2026-11-01）

- 两页 `<script>` 数为 0；外链域名只有 `github.com`（源码 / Releases / 许可证 / 引用的 issue）、`forum.cursor.com`（数据来源）、`www.quotafuse.com`（页脚品牌，实测 200）与 SVG 命名空间的 `www.w3.org`；无 CDN、无字体、无统计脚本。
- 无 `canonical` / `og:url` 硬编码域名，正式域名确定后按上面「如何嵌入官网」第 4 条补齐。
- 两张截图 154 KB / 154 KB（≤ 300 KB），2880×1800，页面内以 1440×900 引用，`alt` 与图注齐全。
- 页面内不含真实用户名、真实路径、内部主机名或 token；示例数据统一 `C:\Users\you\...`、`D:\DiskWarden\...`。
- 无头 Chrome 渲染：1440×900 与 1280×700 两页均 `scrollWidth == clientWidth`（1409/1409、1249/1249），0 条 console 错误、0 个资源加载失败，截图 `<img>` 解码为 2880×1800。390px 一档受 Chrome 最小窗口宽度限制，改用同源 iframe（390px 视口）复测：两页 `scrollWidth == clientWidth == 375`（差值为纵向滚动条），且没有任何元素的 `scrollWidth` 超出自身宽度。
- `python tools/repo_safety_check.py` 通过。

## 验证记录（2026-10-21）

- 本地静态服务（`python -m http.server`）+ 无头 Chrome 155，英文与中文页在 1440×900、1280×700、1080×900 三档均无横向滚动（`scrollWidth == clientWidth`，1440/1440、1280/1280、1080/1080）；另测 390×844 移动端同样无横向滚动。
- 截图 `<img>` 自然尺寸 2880×1800，滚动到视口后正常解码（`complete && naturalWidth > 0`），无 404、无请求失败。
- 0 个 `<script>`、0 条 console / JS 错误、0 个资源加载失败。
- 文案核对：无「Gitee 登录」或任何 Gitee 提法；无 `0.1.0` 旧版本占位；无「100% 安全」「永久免费」类绝对化表述；GitHub 入口为 `https://github.com/QuotaFuse-ai/disk-warden`。
- **本次运行无法查看图片内容**：多宽度无横向滚动、元素几何、图片解码与文案由脚本验证，截图的最终视觉观感仍需人工过目一遍。

## 验证记录（2026-10-19，首版）

- 无头 Chrome（`file://`）渲染英文与中文页：1440×900、1280×700、1080×900 三档均无横向滚动，另测 390×844 移动端；断点扫描覆盖 320 / 390 / 640 / 641 / 768 / 860 / 861 / 1000 / 1001 / 1080 / 1280 / 1440。
- 0 个 `<script>`、0 条 console / JS 错误、0 个资源加载失败。
- 自动化 WCAG AA 对比度检查（24 组页面 × 宽度，普通文字 4.5:1、大字号 3:1）：0 项不达标。
- 页头无重叠，FAQ 全部展开状态下无溢出，无文本被意外裁切。

## 说明

- 页面里的空间数字来自公开 bug 报告与社区帖，不是本产品实测；文案中已明确标注。截图里的目录大小同为示例数据。
- 许可文案与仓库 `LICENSE` 一致：PolyForm Noncommercial 1.0.0，版权人 huangtao，商业授权联系 44062290+QuotaFuse-ai@users.noreply.github.com。
- 为满足离线和 0 JS 要求，FAQ 使用原生 `<details>`，语言切换使用独立静态页；两页内容需同步维护。
- 原型未部署、未推送线上站点、未创建 Release，未改动 `ui/`、`src/` 或 `main.py`。
