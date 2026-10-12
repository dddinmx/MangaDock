# Cloudflare Workers 迁移记录与验收

基线：`origin/main` 提交 `dca45d0`（Delete tests directory）。开发分支 `migration/cloudflare-workers`。原 Flask/Gunicorn 和 Docker 文件未改动；切回原提交即可回退代码。

## 架构和功能替代

| 原功能/依赖 | Workers 方案 | 当前状态 |
| --- | --- | --- |
| Flask/Gunicorn/Jinja 请求处理 | TypeScript Worker；构建时渲染原登录与阅读模板 | 已实现首阶段，不运行 Python 服务 |
| SQLite 用户、任务、进度 | D1；数据库事务/原子 UPSERT、每用户进度 | 首阶段已实现，旧库待迁移 |
| 本地 EPUB/漫画/封面 | 私有 R2，认证 Worker 返回流 | 已实现 EPUB，不公开 bucket |
| 番茄小说来源 | 继续调用 `fanqie.dddinmx.cn` 注册、搜索、任务、产物 API | 已实现适配；服务端保持现有云服务器，不依赖 Mac mini |
| 后台进程、线程轮询 | Queues 每次只做有限工作；D1 状态、租约和 outbox；Cron 恢复丢失消息 | 已实现；提交状态不明不盲目重发 |
| Workflows | 可替代长时间任务轮询，使用 step/sleep/retry 持久化状态 | 已评估；首阶段选 Queues+D1，避免同时维护两种执行系统 |
| EPUB 解压/章节读取 | 浏览器 JSZip；延迟解压章节；正文以 textContent 渲染 | 已实现，32 MB EPUB / 2 MB 单章限制 |
| 小说阅读 UI | 直接构建原 `templates/novel_reader.html` 和交互脚本 | 保留翻页、滚动、字体/颜色；目录名称暂降级为序号 |
| 登录 UI | 构建原 `templates/login.html`，改接 Worker 登录 API | 已实现；不复制原会话或登录密码哈希 |
| 所有旧首页/书架/封面 | 逐页适配原模板和 API | 未完成，首阶段书架复用 CSS，封面占位 |
| 包子/漫画柜/hipmh/mxs/瓜子/卡拉 | TypeScript 来源适配，逐图拉取进 Queue/R2；解混淆在免费 CPU 内逐项验收 | 未迁移，不能宣称所有来源可用 |
| 番茄漫画 | 原服务生成漫画产物；浏览器或独立云作业拆包 | 未迁移；不在 Worker 内解压大 CBZ |
| CBZ/PDF 生成与转换 | 浏览器导出、小文件客户端处理，或按需云作业 | 无法原样运行子进程；大量/复杂转换可能付费 |
| 本地扫描、导入、WebDAV | 上传清单/文件到 R2；WebDAV 适配为远程流 | 未迁移；本地扫描只能一次性在持有备份的机器运行，持续运行不依赖 Mac mini |
| ONNX RealESRGAN 超分 | 一次性预处理已有封面，上传结果；新封面默认原图 | Workers Free 10 ms CPU 无法承担 ONNX 推理；自动云端超分需要独立计算服务，无法保证免费 |
| 用户/分组权限、18+、隐藏书库 | 迁移 D1 权限模型并在所有元数据/文件路由统一检查 | 未迁移，完成前不能导入受限旧书库 |
| AniList、统计、历史、客户端扩展、更新/PWA | 保留协议逐项迁移与契约测试 | 未迁移；原版继续提供这些功能用于回退 |

密码验证由免费 SQLite Durable Object 承担，HTTP Worker 通过内部 binding 请求并等待结果；新账号 PBKDF2 SHA-256 600,000 次，旧版 260,000 次可验证，不降低原哈希强度。使用 `@noble/hashes` 实现，不受 WebCrypto 100,000 次限制。

## 免费额度和不能免费的部分

2026-10-12 获取并核对的 Cloudflare 官方页面：

- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)：Free 每日 100,000 请求，每次 CPU 10 ms。网络等待时间与 CPU 不同；本地 workerd 不提供生产套餐限额的证明。
- [Durable Objects limits](https://developers.cloudflare.com/durable-objects/platform/limits/) / [pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)：免费 SQLite backend 每日 100,000 requests、13,000 GB-s duration，默认每次 CPU 30 秒；此处仅用于有频率限制的密码验证。
- [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/)：Free 每日读 5,000,000 行、写 100,000 行，总存储 5 GB；单库限制还需参考 [D1 limits](https://developers.cloudflare.com/d1/platform/limits/)。进度和任务不放二进制文件。
- [R2 pricing](https://developers.cloudflare.com/r2/pricing/)：Standard 每月免费 10 GB-month、100 万 Class A、1000 万 Class B；超出存储 $0.015/GB-month，公网出站免费，操作超过免费额度另计费。10 GB 库接近上限，没有足够 staging/其他 bucket 余量；100 GB 常驻约 `(100-10)*0.015 = $1.35/月` 存储费（不含其他费用）。这是估算，不是永久免费承诺。
- [Queues pricing](https://developers.cloudflare.com/queues/platform/pricing/)：Free 每日 10,000 operations；消息发送、读取、删除计数，超过 64 KB 分段计费。此实现消息仅一个任务 ID，不携带 EPUB。轮询重试消耗额度，超额会失败，需要观察用量并减少轮询/并发。
- [Workflows pricing](https://developers.cloudflare.com/workflows/reference/pricing/)：Free 配额与 Workers CPU/请求约束需核对实际账号，工作流持久化状态也有存储限制。可用于第二阶段替换轮询，但不解决 ONNX、大压缩包或整书解析的 CPU 问题。
- 番茄现有云服务器继续使用，成本来自现有服务器，不计入 Cloudflare 免费额度。后续独立转换/超分计算也不能承诺免费。

应用 staging 存储预算 256,000,000 bytes、production 8,000,000,000 bytes，避免立即上传 10–100 GB。预算依据 D1 已入库记录及传输前原子预留，不是账号级账单硬上限；失败任务保留预留量，避免孤立 R2 对象被漏算；人工核对并清理后才能释放。手工上传、备份和其他 bucket 都需清理/审计。完整书库超过额度时，必须选择付费 R2 或保留已有云存储并迁移访问层；不使用 Mac mini 作持续服务。

## 免费 CPU 验收门槛

1. 先发布到实际 Workers Free 账号的独立 staging 地址；不启用 Paid，不切现有域名。
2. 登录至少 30 次分散执行，覆盖成功/失败/频率限制；来源真实下载一章，再测试完整小 EPUB、接近 32 MB 产物与慢速来源。
3. 登录、任务创建/轮询、R2 存储/读取、进度写入分别观察 Cloudflare Workers Metrics/Logs 的 CPU 数据和 outcome。10 ms 限额下不得出现 Error 1102 / exceededCpu。验收记录写入 CPU p50/p95/max、运行次数、文件大小、错误数量与时间段。
4. `live-results.json` 的 HTTP wall_ms 只反映延迟，不能作为 CPU 证明。stream→FixedLengthStream→R2 SHA-256 校验旨在减少 JS CPU，但必须线上实测。PBKDF2 已从普通 Worker 移至 Durable Object，需分别检查 HTTP Worker 10 ms 和 Durable Object 30 s 限额；若超预算，使用 Cloudflare Access 身份验证（检查当前免费席位）或其他外部身份提供方，不能靠降低密码哈希成本掩盖问题。
5. 验收失败时维持 staging，不正式切流；修正身份/任务设计后重复受影响测试。浏览器端 EPUB 限制、手机内存压力和首读整书传输也需记录。

## 数据迁移与回退

1. 导出原机/Docker 卷中的 EPUB、漫画目录、cover、comic.json 和 instance SQLite。停写或明确备份时间点。用 SQLite backup API 处理 WAL；不要只复制 .db 主文件。
2. 在持有备份的 Cloud 开发环境运行：

   ```bash
   python workers/scripts/export-library.py \
     --database /path/to/instance/app.db \
     --novels /path/to/小说 \
     --comics /path/to/comic \
     --covers /path/to/static/cover \
     --output workers/migration-output/snapshot-001
   ```

   输出私有 SQLite 快照和每文件 SHA-256、bytes、R2 key、旧小说 ID/进度键清单。该工具只读原数据，不上传、不删旧文件；目录 0700、文件 0600。含账号或来源设置的快照不可提交或公开。
3. 先完成旧权限模型。映射旧用户名/user_id、comic_name、`novel:<文件名 stem>`、分组/授权、comic.json 来源映射到 D1；新的番茄书以 source_id 为 ID，旧作品需建立显式 old→new ID 表。不要只按同名作品合并，也不要只迁移管理员进度。进度保留 chapter/page/anchor/client_progress_ms 和原用户归属。当前首阶段 schema 还不支持全部字段，**不能直接导入全量旧库**。
4. 原 Werkzeug PBKDF2 超过 WebCrypto 上限，但 Durable Object 中的纯 JS 验证兼容 100,000–1,000,000 次 SHA-256 哈希。保留原哈希和私有备份，完成账户/权限映射后再导入；不要降低原哈希强度，也不要复制 Flask session。
5. 预算确定后使用 R2 S3 API/rclone 从备份向内容寻址 key 上传，可重复上传/断点续传。先比较 size 和 SHA-256，确认对象存在后才写 D1 可见记录。10 GB 免费总额度包含 staging、production 和同账号其他 bucket，备份不能无限保留在 R2 免费额度内。
6. 小批量抽样：登录旧账号、检查组隔离、小说章节/进度、漫画页序、封面、更新来源。确认一批再迁移下一批；每批保存失败清单和核对结果。
7. 通过完整功能和免费 CPU 验收后才修改域名路由。旧代码、数据库快照、文件备份保留；出现问题将流量切回旧服务并回放切换后进度。若要求最终完全关闭 Mac mini，回退服务也必须部署到现有云服务器/独立云容器，而不是依赖 Mac mini。
8. CI 正式发布按已测试 SHA 手动触发，数据库 migration 采用追加方式，避免删除式变更。代码 rollback 不等于数据 rollback；切换后先导出 D1/R2 清单再恢复。

## 当前验收状态

真实结果请以本分支测试记录与最终报告为准。部署授权、GitHub 连接或免费 CPU 数据不足时明确标为未验证；不得把本地 R2 模拟环境称为已部署到真实 Cloudflare R2。
