# Turso 数据库迁移

分支：`migration/turso`，从完整迁移分支 `migration/cloudflare-full` 创建。原 Flask、模板、CSS 和前端脚本保留。HTTP Workers、SQLite Durable Objects、R2、Queues 和 Linux 云转换保持；业务 SQL 从 D1 改为 Turso 的 libSQL HTTP 驱动，不重新设计界面。

## 配置和部署

用户已在 GitHub Repository Secrets 配置：

- `TURSO_DATABASE_URL`：`libsql://mangadock-staging-dddinmx.aws-ap-northeast-1.turso.io`。
- `TURSO_TOKEN`：数据库 Token。CI 传给 Worker 时使用 `TURSO_AUTH_TOKEN`，无需改名或在聊天中提供 Token。
- 沿用 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 和 `MANGADOCK_ADMIN_PASSWORD`。

推送新分支运行本地隔离测试及 **Initialize Turso staging**。初始化以事务顺序应用 `0001` 至 `0007`，记录 SHA-256 检查点；重复执行不会重复建表或覆盖管理员密码。Token 仅经环境和 stdin 传递，不放进命令参数、日志、提交或产物。

通过 **Deploy Workers (free plan)** 手动选择该分支和 `staging` 发布。生成的配置使用 `DATABASE_PROVIDER=turso`，没有 D1 绑定，也不会执行 D1 预算查询、DDL 或 SQL。新 Worker、R2、Queue 使用 `mangadock-turso-staging`，已暂停的 `mangadock-full-staging` 不会被恢复。Cron 和定时 GitHub 转换继续关闭；下载仅在用户创建任务后运行。

正式发布使用独立 Turso 数据库，在 GitHub `production` Environment 配置同名 Secrets。部署脚本拒绝把名称含 staging 的数据库用于 production；新资源使用 `mangadock-turso-production`。未完成数据迁移及真实来源验收前，不修改现有 DNS。

## 防止持续消耗额度

- `0004`、`0005` 的任务父子、状态/租约、来源、清理、历史、章节文件名和云转换索引完整应用到 Turso。章节完成检查使用按状态索引的 `EXISTS/LIMIT 1`，不在每下载一张图片时重新统计所有兄弟任务。
- `0007` 以事务触发器维护存储总量，避免每张图片预留空间都重新 SUM 整个书库。一次性回填后按主键读取一行；保留作品文件与存储账本的重叠扣除、WebDAV 排除和作业预留计算，失败/取消释放作业预留。实际 libSQL 验证插入、更新、删除、重复文件引用和事务回滚后的总量与原公式完全一致；本地 D1 的 2,000 文件回归验证新增文件预留读取小于 20 行。
- 普通漫画下载失败最多 5 次，指数退避，租约期间的重复消息不会执行下载；耗尽后进入 error。
- 番茄小说/漫画等待生成的轮询单独限制最多 30 次、间隔 60/120/240/300 秒，并有总时间限制。轮询不会重置预算；漫画正常等待与失败重试分开计数。
- 云转换领取最多 3 次。进程崩溃、租约过期后也不能永久反复领取；超过上限进入 error。
- 队列基础设施重试仍最多 3 次，指数退避；Cron 关闭，不会不断复活已丢弃消息。
- 每个 Application Durable Object 每个 UTC 日最多向 Turso 发出 **2,000 个 HTTP 请求、10,000 条 SQL**。计数在实际网络调用前以 DO 本地事务预留，包括批量 SQL、驱动发出的重试和失败请求。当前只使用 `web`、`jobs` 两个对象，因此每个部署合计至多 4,000 次请求、20,000 条 SQL/日。计数保存在 DO 本地 SQLite，不在 Turso 上为保护逻辑额外查询；对象重启不清零。
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

本地共 88 项检查：数据库适配和保护 18 项；workerd/D1/R2 41 项，包含新的小说/漫画轮询截止、云转换领取上限、存储预留行数回归及真实 workerd 下的 Turso HTTP 驱动/重定向检查；原界面 Chromium 6 项（46 对截图）；Python 21 项；D1 部署预算 2 项。构建与 TypeScript 通过。真实 Turso [七个迁移已全部初始化](https://github.com/dddinmx/MangaDock/actions/runs/38359678956)；[首次完整 CI](https://github.com/dddinmx/MangaDock/actions/runs/38359678825) 通过 86 项。

[首次线上部署](https://github.com/dddinmx/MangaDock/actions/runs/38360021779) 创建了独立资源，但登录返回 500；定位为 workerd 不支持 `redirect:error`，已经改为 `manual` 并显式拒绝 3xx，避免携带数据库授权跟随重定向。补充了带实际驱动及预算的 workerd 回归、驱动包装错误后仍返回 503 的检查。该错误发生在数据库请求发出前，不能把这次登录失败当作 Turso 查询延时证据。修复后的真实部署、延时及新部署 CPU 结果以 CI 运行报告为准，不能用历史 D1 测试站的 CPU 结果替代。
