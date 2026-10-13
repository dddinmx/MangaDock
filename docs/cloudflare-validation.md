# 完整迁移验证记录（2026-10-13）

分支 `migration/cloudflare-full`，本轮提交/独立 staging 线上验收正在执行。原模板与静态资源没有修改。

- TypeScript 和 workerd/D1/R2：35 项通过，包含旧/新密码、CSRF/权限、漫画模拟真实协议→R2→读取、WebDAV Range CBZ、EPUB 原章节标题/正文、进度/累计时长去重、加密集成、云计算租约与过期封面、分片校验、旧元数据导入。
- Chromium：6 项通过，其中 23 个页面分别在 1280px 与 390px 比较原 Python Jinja 和 Workers 页面，共 46 组截图；另外验证小说翻页、保存、刷新恢复、脚本隔离、手机登录，以及桌面/手机漫画加载与滚动恢复。
- Python：21 项检查通过，新增验证旧 EPUB 元数据/作品标识/进度锚点恢复、只读备份规划及 ONNX 超时的 Pillow 回退。没有实际备份，因此不能声称 10–100 GB 全库已迁移。
- 本地原 ONNX 动漫模型超分实跑成功：447×626→1371×1920，手机变体 1285×1800，约 13 秒。这是云执行环境测量，不是 HTTP Worker CPU。
- 新增 2 项部署预算检查测试通过，验证跨数据库累加日用量和缺少 Analytics 时阻止远程 SQL。
- 新完整站的真实来源、Cloudflare R2、CPU 验收因 D1 配额与番茄 1033 故障暂停。下方小说试点的 9.181 ms 不能复用为新实现通过证明。

未验收/剩余：逐来源真实漫画与番茄漫画、用户真实 AniList/WebDAV、超大文件与高并发、旧任务历史/图片目录/加密凭据导入、整个账号 R2 账单用量和完整 DO CPU 指标。未正式切换旧域名。

GitHub CI：代码提交 `22810f8` 的[推送检查](https://github.com/dddinmx/MangaDock/actions/runs/38343181004)与 [PR 检查](https://github.com/dddinmx/MangaDock/actions/runs/38343188575)均通过，64 项检查包括 35 个运行时测试、6 个浏览器测试、21 个 Python 检查和 2 个预算检查。全部使用本地 Miniflare/测试数据，不访问线上 D1 执行 SQL。

## D1 配额事故与暂停（2026-10-13，北京时间）

Cloudflare 部署日志错误 code 7500 明确为 **daily row read limit**。管理/Analytics API 读取到完整迁移库当日 `rowsRead=13,107,900`、`rowsWritten=16,799`，并非写入 100,000 行已用尽。遗漏漫画任务父子索引、重复查重/统计和重试放大读取，说明上线前缺少行数预算验收；不能把只通过 CPU 和小库功能测试视为 Free 配额验收。

[暂停控制运行](https://github.com/dddinmx/MangaDock/actions/runs/38342486792) 已核对 success：完整迁移测试站 workers.dev/预览入口关闭、Cron 清空、Queue consumer 移除。数据和 R2 文件保留；没有执行 D1 SQL，没有升级付费套餐。已关闭推送自动部署和定时云转换，后续代码提交只运行本地 Miniflare 测试，不能自动恢复线上任务。

已同步远程分支的 `0004_job_indexes.sql` 与退避/死信修复；新增 `0005_query_indexes.sql` 覆盖限流清理、登录历史、用户任务、旧章节文件名与云转换查询。将每次完成的兄弟任务全量统计改为对未完成状态做索引存在性检查，章节读取改为逐个主键/别名索引查找。索引写入也消耗额度，补充了部署前账号级 Analytics 预算检查：当日读取达到 400 万或写入达到 8 万，或无法读取统计，就在任何远程 SQL 前停止。

本地 2,000 条任务数据验证索引查询、退避、达到最大重试后停止，以及空闲 Cron 行数；新索引与修复 **尚未应用到线上**，因当天额度已不足，测试站继续暂停。D1 免费日额度按 UTC 零点重置（北京时间次日 08:00），不安排届时自动重启；恢复前应按批应用索引并采集实际 D1 rows_read/rows_written。


---

# 小说试点历史验证证据

分支 `migration/cloudflare-workers`；草稿 [PR #7](https://github.com/dddinmx/MangaDock/pull/7)。原 `main` 和 `cf-workers-migration` 均保留。

## 已验证

- 最新本地 TypeScript 检查与 Workers 运行时接口测试：14 项通过，包括原 260k / 新 600k 密码在 SQLite Durable Object 中验证、认证/CSRF/退出、共享 D1 登录频率限制、权限、流式 R2 校验、Range 读取、进度隔离和旧快照防覆盖、大小/存储预算拦截。
- Chromium 浏览器：2 项通过；原版登录、阅读器翻页、进度写入/刷新恢复、EPUB 脚本隔离、手机登录布局。
- Python 迁移：19 项通过；包含复用的 18 项漫画/SQLite 迁移测试以及新的 WAL 一致备份/小说清单测试。
- GitHub CI：[首个测试运行](https://github.com/dddinmx/MangaDock/actions/runs/38276989040) 32 项测试通过；后续增加兼容哈希测试后 [33 项测试运行](https://github.com/dddinmx/MangaDock/actions/runs/38279001621) 通过。
- [最新 CI](https://github.com/dddinmx/MangaDock/actions/runs/38284821000)：`45d1b27` 的构建、TypeScript 和全部 35 项测试通过。
- 真实番茄云服务：注册、搜索《三国演义》（罗贯中）、解析 124 个章节成功；通过本地 workerd + 配置的出网代理适配，登录 → 创建来源作业 → 流式存储到本地 R2 → 读取 → D1 保存进度已通过。EPUB 大小 `3,345,550` bytes，SHA-256 `097aa09c3f9bde6efc453e6542b189d4d253397efd4fe67d2cd0675c8a095c1b`。来源返回的 EPUB 以实际产物为准，不能仅凭请求的 chapter_ids 声称只下载了一章。

上述“本地 R2”是 Miniflare 的存储实现；它验证协议和行为，**不能作为真实 Cloudflare R2 部署或 CPU 限额的证明**。测试登录耗时约 875 ms 是 wall time；600k 密码计算已移入单独 Durable Object，不是 HTTP Worker CPU 指标。

## Cloudflare 线上闭环已通过

测试地址：https://mangadock-novel-staging.dddinmx.workers.dev ，管理员 `admin`，密码仅由 CI Secret 提供。

- [成功部署与真实闭环运行](https://github.com/dddinmx/MangaDock/actions/runs/38281279808)：2026-10-12 17:13:58 UTC 发布完成；17:15:29 UTC 真实来源下载、R2 读取、D1 进度写入通过；17:15:37 UTC Chromium 真实书籍阅读与刷新后进度恢复通过。
- [后续发布](https://github.com/dddinmx/MangaDock/actions/runs/38283342282)：`b6b13fa` 再次部署成功，说明现有来源 Worker Secrets 与已下载书籍可在发布之间保留。
- [最新发布与浏览器验证](https://github.com/dddinmx/MangaDock/actions/runs/38284815734)：`45d1b27` 全部通过；复用已下载书籍验证 R2 读取与进度恢复，不把重复发布说成又下载了一本新书。
- 线上 Artifact `deployment-staging` 包含 deployment.json、live-results.json、live-browser-results.json、CPU 报告和阅读器截图；不含 Token、密码或 Cookie。
- HTTP `/api/health` 已从当前环境核对 200；未携带用户名/密码的登录请求返回正常校验错误 400，说明 Worker 路由可用。

### 免费 CPU 实测

上述最新发布的 GitHub Check Run `114930524958` 的 CPU notice 已通过 API 核对，报告 `verified:true`。Cloudflare GraphQL 统计窗口为 2026-10-12 17:03:12.772–17:33:12 UTC，Worker 为 `mangadock-novel-staging`：

| outcome | 请求数 | errors | CPU p50 | CPU p90 | CPU p99 | CPU max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| success | 260 | 0 | 1.488 ms | 3.287 ms | 8.908 ms | 9.181 ms |
| clientDisconnected | 4 | 0 | 0.682 ms | 0.841 ms | 0.841 ms | 0.841 ms |

264 次记录中未出现 CPU 超限 outcome；最高 9.181 ms，低于 HTTP Worker Free 10 ms。此窗口覆盖已通过的来源/R2 闭环与后续浏览器验证，但 GraphQL 按脚本/outcome 聚合，不能单独归因到每个 API 路由。4 次客户端断开不能记为成功请求。9.181 ms 的余量较小，新增功能、32 MB 极限文件和压力测试仍需重测；本次不是完整功能或所有负载的免费承诺。

GraphQL 原始单位为 microseconds；上述表已换算成 ms。密码计算所在 Durable Object 的 CPU 不包含在这份 HTTP Worker 报告中；线上登录成功，独立 30 s CPU 的分位数/峰值尚未采集。CI 步骤成功本身也不等于 `verified:true`：查询权限不足、缺数据或 API schema 不符时会输出 `verified:false`。完整正式切流前继续执行架构文档中的扩展验收。

首轮发布因新增密码长度检查停止，已恢复原项目规则。来源曾因服务宕机返回 HTTP 530；恢复后重新部署通过。新 Worker 首次发布的路由曾短暂返回 404，已增加就绪检查。上述故障已修复；不把故障期间测试计入成功闭环。

## 小说试点当时尚未迁移

完整漫画来源/阅读 UI、封面、分组和 18+ 权限、用户管理、统计/历史、AniList、第三方客户端 API、自动更新、WebDAV/本地上传、ONNX 超分，以及 10–100 GB 全量库导入。当前试点不依赖 Mac mini；旧版全部功能不等于已经迁移完成。
