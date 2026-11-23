# 参与贡献

这个工具就是几百行 PowerShell，改起来不难。欢迎 fork、提 issue、提 PR。

## 提 issue

- **Bug**：请写清 calibre 版本、Windows 版本、你做了什么、期望什么、实际什么。表单相关的 bug 请把服务所在终端的报错贴上来（服务在控制台里会打印请求日志）。
- **功能建议**：说清使用场景比说清实现方式更有用，比如「我想在笔记里同时记下页码和章节」比「加个字段」更有帮助。

## 提 PR

1. Fork 本仓库，从 `main` 切一个分支：`git checkout -b fix/xxx`
2. 改完在本地跑一遍自检：`powershell -NoProfile -ExecutionPolicy Bypass -File quote_server.ps1 -Test`
3. 表单或页面有改动的话，顺手截张图放在 PR 描述里
4. 提交 PR，描述里写清楚动机和验证方式

## 代码约定

- 只用 Windows 自带能力：PowerShell 5.1、.NET Framework、不用第三方模块、不引入 Node/Python
- **所有 `.ps1` 必须存成 UTF-8 with BOM**——PowerShell 5.1 会把无 BOM 的中文按 GBK 解析，直接报语法错误
- `.bat` / `.vbs` 保持 CRLF 换行（`.gitattributes` 已处理）
- 输出只写 UTF-8 无 BOM 的 Markdown，不改动 calibre 书库数据库
- 不往笔记里写任何标记性元数据（保持纯文本可读、可被其他工具处理）
- 配置一律走 `quote_settings.json`，不要把路径或 Key 硬编码进脚本

## 最需要帮忙的地方

- **阅读位置**：书名已能自动带入（读 `%APPDATA%\calibre\viewer-webengine.json` 的 `session_data.standalone_recently_opened[0]`）。位置还缺，可用的线索：
  - `%APPDATA%\calibre\viewer\annots\<sha256>.json`，文件名是 `sha256(书文件绝对路径的 UTF-8 字节)`，内容里有 `{"type":"last-read","pos":"epubcfi(...)","timestamp":...}`，还有高亮/书签记录（带文字片段）。
  - `%LOCALAPPDATA%\calibre-cache\viewer-reading-rates.json` 里按同一 hash 记了 `chapter_times`。
  - 难点是把 `epubcfi` 换算成「第几章 / 百分之几」：需要解析转换后的正文（azw3/mobi 是 calibre 临时转成 epub 的，缓存在 `%LOCALAPPDATA%\calibre-cache\ev2`，目录 ACL 不对外开放），而且很多书转换后整个正文是一个 html 文件，spine 索引没有区分度。谁有更好的思路欢迎开 issue 讨论。
- **多 AI 提供方**：现在只对接了 DeepSeek 的 `/chat/completions`。任何 OpenAI 兼容端点应该都能用（改 `ai_base_url`），但没逐个验证过。
- **笔记格式**：目前是「一条一个 `---` 分隔块」。如果你有更好的 Markdown 组织方式（比如按书分文件、加书名作为二级标题），欢迎讨论。
- **跨平台**：Windows 专属（PowerShell + VBS）。macOS/Linux 上大概要重写成 Python 小服务，如果谁愿意做，我可以把协议和表单结构整理出来。
