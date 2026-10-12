# 当前迁移验证证据

分支 `migration/cloudflare-workers`；草稿 [PR #7](https://github.com/dddinmx/MangaDock/pull/7)。原 `main` 和 `cf-workers-migration` 均保留。

## 已验证

- 最新本地 TypeScript 检查与 Workers 运行时接口测试：14 项通过，包括原 260k / 新 600k 密码在 SQLite Durable Object 中验证、认证/CSRF/退出、共享 D1 登录频率限制、权限、流式 R2 校验、Range 读取、进度隔离和旧快照防覆盖、大小/存储预算拦截。
- Chromium 浏览器：2 项通过；原版登录、阅读器翻页、进度写入/刷新恢复、EPUB 脚本隔离、手机登录布局。
- Python 迁移：19 项通过；包含复用的 18 项漫画/SQLite 迁移测试以及新的 WAL 一致备份/小说清单测试。
- GitHub CI：[首个测试运行](https://github.com/dddinmx/MangaDock/actions/runs/38276989040) 32 项测试通过；后续增加兼容哈希测试后 [33 项测试运行](https://github.com/dddinmx/MangaDock/actions/runs/38279001621) 通过。
- 真实番茄云服务：注册、搜索《三国演义》（罗贯中）、解析 124 个章节成功；通过本地 workerd + 配置的出网代理适配，登录 → 创建来源作业 → 流式存储到本地 R2 → 读取 → D1 保存进度已通过。EPUB 大小 `3,345,550` bytes，SHA-256 `097aa09c3f9bde6efc453e6542b189d4d253397efd4fe67d2cd0675c8a095c1b`。来源返回的 EPUB 以实际产物为准，不能仅凭请求的 chapter_ids 声称只下载了一章。

上述“本地 R2”是 Miniflare 的存储实现；它验证协议和行为，**不能作为真实 Cloudflare R2 部署或 CPU 限额的证明**。测试登录耗时约 875 ms 是 wall time；600k 密码计算已移入单独 Durable Object，不是 HTTP Worker CPU 指标。

## 尚待验证

Cloudflare staging 完整部署、真实云端 R2 + Queue、线上真实书籍浏览器阅读、普通 Worker CPU ≤10 ms 和 Durable Object CPU ≤30 s。现有 workflow 会在部署后执行来源 smoke、浏览器进度恢复、CPU 报告，并上传不含密码/Token 的结果。CPU 查询权限不足、缺数据或 API schema 不符时返回 `verified:false`，不得视为通过。

首轮发布因新增的 16 字符密码检查提前停止；已恢复原项目 6 字符规则，保留强哈希和频率限制。之后来源注册遇到 HTTP 530，用户确认番茄服务当时宕机，并已要求服务恢复后重试；正在重新部署。最终实际部署结果需结合最新 GitHub Actions 运行更新本记录。

## 仍未迁移

完整漫画来源/阅读 UI、封面、分组和 18+ 权限、用户管理、统计/历史、AniList、第三方客户端 API、自动更新、WebDAV/本地上传、ONNX 超分，以及 10–100 GB 全量库导入。当前试点不依赖 Mac mini；旧版全部功能不等于已经迁移完成。
