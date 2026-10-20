# T27 单页内容同步：真实截图、版本身份、登录与安全文案

对应交付：`website/disk-warden/`（英文 `index.html` + 中文 `zh-Hans/index.html` + `assets/`）。

## 改了什么

1. **真实截图替换示意图**：`#interface` 一节改为 `<img>`，引用 `assets/ide-overview-en.png` / `assets/ide-overview-zh.png`（各 2880×1800，153 / 154 KB）；删除「界面示意 · 不是截图」徽标与 `.mock*` 样式。
2. **版本身份**：新增「Status and download / 状态与下载」双卡（Windows 当前 `0.9.0-beta.7` 未签名、`0.9.0-beta.8` 目录联接校验修复即将发布、macOS 开发中且不承诺时间）；下载按钮保持 `disabled`「Coming soon / 即将发布」，安装包与 SHA-256 为 `TODO`。
3. **默认行为小节**（`#defaults`）：欢迎页可跳过登录、Star 提示不打扰、AI 分析默认关闭且 Key 用 Windows DPAPI 本机加密。
4. **FAQ**：新增「登录 GitHub 是必须的吗」「会上传我的数据吗」，改写「macOS 版什么时候有」；原有 5 条保留。

## 截图怎么来的

- 用无头 Chrome 155 加载仓库内的 `ui/index.html`（0.9.0-beta.7 随包发布的同一份界面），注入与真实 RPC 同结构的 mock pywebview bridge（`app.hello` / `scan.result` / `disk.info`），再 CDP 截「IDE 数据」页。
- 未能 `python -m src.app.main`：构建机 Python 3.8.6、无外网，pywebview / WebView2 装不上。界面文件与真实运行时同一份，且**未改动** `ui/`、`src/`、`main.py`。
- 隐私清洗：路径统一 `C:\Users\you\...`、`D:\DiskWarden\...`；无真实用户名 / 真实路径 / 真实项目名；图注注明「目录名与大小为示例数据」。
- 页面内数字（C 盘释放 38.6 GB、需要 40.6 GB、目标盘剩余 228.0 GB）为样例数据，与截图一致。

## 验证结果

本地静态服务 + 无头 Chrome，英文 / 中文两页：

| 宽度 | scrollWidth == clientWidth | 横向溢出元素 | 截图解码 |
| --- | --- | --- | --- |
| 1440×900 | 1440 == 1440 | 0 | 2880×1800 OK |
| 1280×700 | 1280 == 1280 | 0 | 2880×1800 OK |
| 1080×900 | 1080 == 1080 | 0 | 2880×1800 OK |
| 390×844 | 390 == 390 | 0 | 2880×1800 OK |

- 0 个 `<script>`、0 条 console / JS 错误、0 个资源加载失败。
- 文案审计：无 Gitee 提法、无 `0.1.0` 残留、无「100% 安全 / 永久免费」、无假下载链接。

`screenshots/` 为本次整页与关键小节截图（900px 宽 / 移动端原宽，PNG 量化压缩）。**执行本次任务的 agent 无法查看图片内容**，几何与文案由脚本断言，最终视觉观感需人工过目。
