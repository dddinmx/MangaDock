# Turso 数据库迁移

分支：`migration/turso`，从完整迁移分支 `migration/cloudflare-full` 创建。原 Flask、模板、CSS 和前端脚本保留。HTTP Workers、SQLite Durable Objects、R2、Queues 和 Linux 云转换保持；业务 SQL 从 D1 改为 Turso 的 libSQL HTTP 驱动，不重新设计界面。

## 配置和部署

用户已在 GitHub Repository Secrets 配置：

- `TURSO_DATABASE_URL`：`libsql://mangadock-staging-dddinmx.aws-ap-northeast-1.turso.io`。
- `TURSO_TOKEN`：数据库 Token。CI 传给 Worker 时使用 `TURSO_AUTH_TOKEN`，无需改名或在聊天中提供 Token。
- 沿用 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 和 `MANGADOCK_ADMIN_PASSWORD`。

推送新分支运行本地隔离测试及 **Initialize Turso staging**。初始化以事务顺序应用 `0001` 至 `0007`，记录 SHA-256 检查点；重复执行不会重复建表或覆盖管理员密码。Token 仅经环境和 stdin 传递，不放进命令参数、日志、提交或产物。

通过 **Deploy Workers (free plan)** 手动选择该分支和 `staging` 发布。用户已暂停小说 API，本轮默认只验收漫画，`test_novels=false` 且番茄自动注册关闭；恢复后须显式选择该输入才访问番茄服务。`comic_chapters` 可选择 1 或 2，补测第二章时复用已有章节，限制真实下载规模。生成的配置使用 `DATABASE_PROVIDER=turso`，没有 D1 绑定，也不会执行 D1 预算查询、DDL 或 SQL。新 Worker、R2、Queue 使用 `mangadock-turso-staging`，已暂停的 `mangadock-full-staging` 不会被恢复。Cron 和定时 GitHub 转换继续关闭；下载仅在用户创建任务后运行。

正式发布使用独立 Turso 数据库，在 GitHub `production` Environment 配置同名 Secrets。部署脚本拒绝把名称含 staging 的数据库用于 production；新资源使用 `mangadock-turso-production`。未完成数据迁移及真实来源验收前，不修改现有 DNS。

## 防止持续消耗额度

- `0004`、`0005` 的任务父子、状态/租约、来源、清理、历史、章节文件名和云转换索引完整应用到 Turso。章节完成检查使用按状态索引的 `EXISTS/LIMIT 1`，不在每下载一张图片时重新统计所有兄弟任务。
- `0007` 以事务触发器维护存储总量，避免每张图片预留空间都重新 SUM 整个书库。一次性回填后按主键读取一行；保留作品文件与存储账本的重叠扣除、WebDAV 排除和作业预留计算，失败/取消释放作业预留。实际 libSQL 验证插入、更新、删除、重复文件引用和事务回滚后的总量与原公式完全一致；本地 D1 的 2,000 文件回归验证新增文件预留读取小于 20 行。
- 漫画书架只为当前页面最多 100 本作品批量查询一次进度，消除原来逐本发出数据库请求的 N+1 问题；50 本书的回归限制总 SQL 请求少于 6 次，并验证进度仍对应正确作品。
- 普通漫画下载失败最多 5 次，指数退避，租约期间的重复消息不会执行下载；耗尽后进入 error。
- 番茄小说/漫画等待生成的轮询单独限制最多 30 次、间隔 60/120/240/300 秒，并有总时间限制。轮询不会重置预算；漫画正常等待与失败重试分开计数。
- 云转换领取最多 3 次。进程崩溃、租约过期后也不能永久反复领取；超过上限进入 error。
- 队列基础设施重试仍最多 3 次，指数退避；Cron 关闭，不会不断复活已丢弃消息。
- 当前测试环境 `jobs` 和 `web` 分别限制每个 UTC 日 **200,000 个 HTTP 请求、200,000 条 SQL**；`login`、`compute`、`updates` 默认各 10,000 请求、50,000 SQL。这些是应用预算，不是 Turso 或 Cloudflare 赠送的服务额度。
- 控制状态存放在同一个私有 R2 桶的 `_system/control-v1/`，由原有 Durable Object 串行处理并通过 ETag 条件写入。首次切换只读旧 DO 状态，原样保留当天计数、队列计数、登录限速和已有服务端暂停；旧计数不可读时停止切换，不能从零继续。禁止把整个私有控制前缀作为公开资源。
- 数据库保护按最多 64 个请求/SQL 信用批量预留，批量 SQL 按实际条数扣减，所有重试也先扣减。预留后才发出数据库请求；对象重建丢弃未用信用而不退回，可保守高估消耗，但不能超预算。成功请求不反复保存无变化的错误计数；配额、鉴权、限流和连续网络异常仍立即持久化暂停。普通新图片路径使用 6 次 SQL，权限及父任务停止检查包含在原子领取中。
- R2 控制 JSON 上限 1 MB、迁移最多 2,000 个旧键、条件写冲突最多重试 5 次。控制数据也计入 R2 的实际存储和操作用量；免费标准存储包含每月 100 万次 Class A、1,000 万次 Class B 操作以及 10 GB 存储，超过后不能宣称免费。
- 旧 SQLite 免费写入额度耗尽时，原生 DO alarm 也可能不可写。恢复期限先持久化 R2，固定 Cron 和认证云执行器探测作为后备；这不绕过每日队列预算或取消状态。已有 SQLite 状态保留，可回退，R2 控制状态成为新版本权威状态，回退不能复用旧计数来放大当天额度。
- 达到本地预算会阻止后续出网并返回 503；存活对象缓存暂停状态，重复被拒请求不会持续改写计数。每日计数按 UTC 零点恢复。
- 一分钟内连续 3 次网络/5xx 故障暂停 10 分钟；401/403/429 暂停 1 小时；识别到提供方月额度错误时暂停至下个 UTC 月。暂停状态持久化，成功的并发响应也不会取消已触发的暂停。
- Turso URL/Token 缺失或错误时直接失败，不回退到 D1。

上述请求和 SQL 限制**不是实际计费行数的上限**：一条查询可能扫描多行。`@libsql/client` 没有提供扫描行数，不用返回行数或 `rowsAffected` 伪造计费数据。仍须在 Turso 控制台检查实际月用量，免费额度改变或书库增大时再调整。不能保证应用零消耗；已限制已知自动循环和每天出网总量。

## 查询速度

数据库地址位于东京。使用现有 Application DO 的 `apac` 位置提示，减少业务逻辑与数据库之间的长距离往返；Cloudflare 位置提示不是位置保证。R2 文件路径不变。`latency-report.mjs` 测量真实部署的登录、首页、漫画/小说书架及已有小说的进度读写，记录全部响应体接收的耗时、中位数、最大值和 Cloudflare 接入点。

GitHub runner 的耗时不等于中国用户耗时，也不是 CPU；不能直接断言与 D1 一样快。CPU 继续以 Cloudflare GraphQL 真实统计单独核验。

## 数据迁移与回退

本轮先初始化新数据库，不自动导出或读取线上 D1。旧 D1 与 R2 数据保留，旧测试站继续暂停。当前未取得实际 10–100 GB 书库备份，因此不声称已有数据迁移完成。

原 Flask 数据迁移沿用 `workers/scripts/import-library.py`：先对 SQLite/WAL 一致备份生成只读迁移规划，然后分批上传 R2 文件与用户、权限、作品、章节、进度和阅读时间；目标 Worker 的导入 API 自动使用 Turso 适配器。必须先确认目标 R2 预算，保存导入清单及校验摘要，抽样验证阅读和进度，再正式切流。转换任务历史与旧加密集成凭据仍需单独处理。

若要保留旧 D1 测试数据，先保持该站停机，等确认账号预算可用后另行取得一次离线导出，在离线副本中提取业务数据；不通过持续分页线上查询复制全库。Turso 和新 R2 使用独立资源，导入前核对 object_key 与对应的目标桶；本轮没有复制旧桶文件。

代码回退到原分支不等于数据回滚；保留导入前后的备份和 R2 对象，确认原库可用后才恢复旧部署。不要为回退直接启用此前耗尽额度的 D1 消费者。

## 免费额度与当前验证

Turso Free 官方页面本轮核对为 5 GB、每月 5 亿行读取、1,000 万行写入，无需信用卡；以账户控制台和 [官方价格页](https://turso.tech/pricing) 为准。R2 的免费存储是每账号共享 10 GB，100 GB 书库仅存储超额部分约 $1.35/月，另有请求额度。现有番茄服务器和超出平台免费额度的资源不能承诺免费，不自动升级套餐。

最新 [完整 CI](https://github.com/dddinmx/MangaDock/actions/runs/38363695055) 共 **89 项检查通过**：数据库适配和保护 18 项；workerd/D1/R2 42 项，包含小说/漫画轮询截止、云转换领取上限、存储预留行数回归、真实 workerd 下的 Turso HTTP 驱动/重定向以及 50 本漫画进度批量读取；原界面 Chromium 6 项（46 对截图）；Python 21 项；D1 部署预算 2 项。构建与 TypeScript 通过。真实 Turso [七个迁移已全部初始化](https://github.com/dddinmx/MangaDock/actions/runs/38359678956)。

[首次线上部署](https://github.com/dddinmx/MangaDock/actions/runs/38360021779) 创建了独立资源，但登录返回 500；定位为 workerd 不支持 `redirect:error`，已经改为 `manual` 并显式拒绝 3xx，避免携带数据库授权跟随重定向。补充了带实际驱动及预算的 workerd 回归、驱动包装错误后仍返回 503 的检查。该错误发生在数据库请求发出前，不能把这次登录失败当作 Turso 查询延时证据。修复后的真实部署、延时及新部署 CPU 结果以 CI 运行报告为准，不能用历史 D1 测试站的 CPU 结果替代。

## 漫画线上验收结果（2026-10-13）

测试站：[mangadock-turso-staging](https://mangadock-turso-staging.dddinmx.workers.dev)，应用代码 `291fd59`；[草稿 PR #9](https://github.com/dddinmx/MangaDock/pull/9)。后续 `41a5309` 只调整验收等待及提供 `verify_only`，没有重新部署应用。

- [首次真实漫画运行](https://github.com/dddinmx/MangaDock/actions/runs/38360993277)：包子漫画《大奉打更人》第一章实际下载、私有 R2 图片读取、保存进度通过。Linux 兼容来源解析得到 676 章目录，实际下载限制为一章，不能把目录解析章数说成已下载章数。封面超分用原 ONNX 模型实际成功：320×426 → 1442×1920，手机图 1352×1800，约 24.23 秒。
- [第二章增量下载](https://github.com/dddinmx/MangaDock/actions/runs/38362909509)：只补测至两章，`new_download=true`，第二章图片首张 390,918 字节；进度读回 `chapter_index=1,page_index=0`。该脚本从 07:32:16.889 UTC 到 07:33:34.472 UTC，共约 77.6 秒，包含登录、排队、云解析、下载、R2、读取及进度验证，不能当作纯下载时间。此运行的浏览器步骤因为登录导航超过原 5 秒等待而失败；已加明确导航等待，没有修改界面。
- [最终在线验收](https://github.com/dddinmx/MangaDock/actions/runs/38363696537) **全部通过**。使用 `verify_only=true`，复验已有两章，没有重新部署或创建新下载；电脑 1280px、手机 390px 均在原阅读器加载真实私有图片并在刷新后恢复滚动进度。小说/番茄注册均按用户暂停要求跳过。
- 最终 GitHub runner 测得：漫画书架 GET 中位数 569 ms（5 次）、首页 GET 927 ms（5 次）、漫画进度 GET 569 ms（3 次）、进度 POST 972 ms（3 次）；登录 3,939 ms（1 次）。样本少、客户端在 GitHub runner；没有同条件 D1 对照，不能量化 D1/Turso 性能差异或承诺中国用户耗时相同。
- 同一新 Worker 最近 30 分钟真实 CPU 统计：252 次请求（246 success、6 clientDisconnected），执行错误数 0，最大 CPU 2,141 微秒即 **2.141 ms**，低于普通 HTTP Worker Free 的 10 ms；包含多个本轮验收会话，不是独立的负载测试。DO 的 CPU 限额仍需单独看，不能由此声明所有后端场景都已验收。

本轮没有查询或恢复线上 D1，没有导入原 10–100 GB 私有书库。其余漫画来源、整本/大文件边界、自动云转换调度及正式数据迁移仍待逐项验收；定时任务继续关闭。
