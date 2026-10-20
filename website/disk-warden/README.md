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

`#interface` 一节用的是产品真实界面的截图，不再是 HTML/CSS 画的示意图（「界面示意 · 不是截图」徽标已删除）。

- **截的是什么**：仓库 `ui/index.html`（0.9.0-beta.7 随包发布的同一份单文件界面）里的「IDE 数据」页 —— 左侧 5 个工具、右侧 Cursor 的 7 个目录（4 可迁 / 1 未知 / 2 受保护）与迁移前后的空间数字。
- **怎么截的**：无头 Chrome 155 加载该文件，注入一个 mock 的 pywebview bridge（`window.pywebview.api.rpc`）返回与真实 RPC 同结构（`app.hello` / `scan.result` / `disk.info` 等）的样例数据，然后 CDP 截图。没改 `ui/`、`src/`、`main.py` 一行代码。
- **为什么不是 `python -m src.app.main`**：本构建机 Python 3.8.6、无外网，pywebview / WebView2 依赖装不上，应用起不来。改为直接渲染随包发布的界面文件，UI 与真实运行时是同一份。
- **规格**：截图为 2× DPR（2880×1800），量化 256 色后 en 153 KB / zh 154 KB，均在 300 KB 以内；页面里以 `width="1440" height="900"`、`loading="lazy"`、`decoding="async"` 引用，并写了描述性 `alt`。
- **隐私清洗**：目录路径统一为 `C:\Users\you\...`、`D:\DiskWarden\...`，不含任何真实用户名、真实路径或真实项目名；数字为示例数据，图注已写明「Folder names and sizes are sample data / 目录名与大小为示例数据」。
- **换成真机截图**：若之后要在真机上重截，覆盖同名文件即可，保持 1440×900 比例（2× 更佳）、压到 300 KB 以内，中英文各自对应；`alt` 与图注里的界面描述需随之核对。

## 占位内容（上线前必须替换）

| 位置 | 当前内容 | 替换成 |
| --- | --- | --- |
| 页头 logo、favicon | 内联 SVG 盾牌占位 | 官方品牌 SVG；同步替换 `index.html`、`zh-Hans/index.html` 的 favicon data URI |
| `#download` 主按钮 | `disabled` 的「Coming soon / 即将发布」 | 首个公开版本发到 GitHub Releases 后，改成 `<a class="btn btn-primary" href="Release 附件地址">Download for Windows</a>`，并同步修改 `aria-describedby` 说明 |
| 安装包 / SHA-256 | `TODO` 占位（`meta-list` 两行） | 公开安装包地址与同名哈希 |
| 版本号 | `0.9.0-beta.7（未签名）`、`Next: 0.9.0-beta.8 —— 目录联接校验修复` | 发版时同步 `status-card` 的 Current / Next、`download-sub` 与图注里的版本号三处 |
| 更新日志 | 整条链接未出现（没有就不显示） | 有 changelog 时再加链接 |
| OG / 社交预览 | 仅有标题与描述 | 补 `og:image`（1200×630）、`og:url` 与最终绝对 URL |
| 问题区数字 | 三份公开报告（Claude Code #16093、#28235，Cursor 论坛帖） | 保留出处链接；上线前复查链接是否仍可访问 |

仓库目前**没有 GitHub Release**，Windows 只存在未签名的测试构建，所以页面上**没有任何下载链接**：主 CTA 的禁用态与「即将发布」说明是刻意设计，不要为了「看起来完整」而改成假链接。macOS 版仍在开发中，页面只写「开发中 / In development」，不承诺任何时间点。

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
- 许可文案与仓库 `LICENSE` 一致：PolyForm Noncommercial 1.0.0，版权人 huangtao，商业授权联系 13439731205@163.com / +86 134 3973 1205。
- 为满足离线和 0 JS 要求，FAQ 使用原生 `<details>`，语言切换使用独立静态页；两页内容需同步维护。
- 原型未部署、未推送线上站点、未创建 Release，未改动 `ui/`、`src/` 或 `main.py`。
