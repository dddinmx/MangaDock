# Cloudflare 完整迁移实施状态

**当前线上暂停**：D1 当日读取行数被漫画任务反复扫描耗尽。测试站入口、Cron、Queue consumer 已关闭，自动部署/云计算定时触发已禁用。后续只进行本地验证，修复索引与预算检查尚未在线应用。详见验证记录的 D1 事故小节。

完整迁移分支 `migration/cloudflare-full` 从已验证的小说试点分支继续开发。原 `main`、旧部分迁移分支和小说试点保持可回退。原 34 个模板与全部原前端样式/脚本直接复用，当前 23 个页面的 46 组桌面/手机截图对照通过；这不等于所有交互和真实数据状态已验收。

| 模块 | 实现 | 验收/限制 |
| --- | --- | --- |
| 原界面与路由 | 构建时编译 Jinja；130 个原路由契约，原 CSS/字体/JS/PWA 资源 | 23 页截图、小说阅读交互通过；其余状态与漫画浏览器验收继续补充 |
| 登录、用户、权限 | D1 会话与限流，DO 保留 260k/600k 密码验证；用户 CRUD、分组、18+、隐藏书库 | 本地认证、越权和直接文件隔离测试通过 |
| 小说 | 原番茄 API，私有 R2，EPUB 章节索引、封面、更新、进度与时长 | 新完整站线上验收待运行；大于 32 MB 的 EPUB 交给 CI 索引，产物上限 2 GB、同时受书库预算限制 |
| 漫画 | 7 个来源适配、番茄漫画、逐图 Queue、CBZ/PDF 与原客户端 API | 模拟来源闭环通过；逐来源真实下载与防爬兼容仍需验收，不能宣称所有网站可用 |
| 历史、统计、分组、更新 | D1 进度、累计阅读会话去重、更新目录对比与自动更新 | 本地测试通过；完整旧数据对照尚无备份可运行 |
| WebDAV | HTTPS 远程挂载、加密密码、PROPFIND、CBZ Range 读取、远端 EPUB | Range 和 R2 前缀扫描测试通过；不支持 Range 的网盘和 PDF 页索引通过云转换降级，等待 CI，不能保证原本机缓存的即时速度 |
| AniList/元数据 | 加密账号、关联/解除、进度同步、AniList/MangaUpdates 资料 | 接口已实现；用户真实账号未授权，线上写入未验收 |
| ONNX 超分/大转换 | GitHub Actions Linux 原 RealESRGAN 模型、Pillow 回退、PDF/CBZ/大 EPUB | 本地真实 ONNX 447×626→1371×1920 成功；CI 定时仅默认分支生效，15 分钟节拍不保证即时 |
| 扫描/旧数据导入 | 私有上传、分片、旧用户/组/进度/时长/封面导入工具 | 真实库未提供；图片目录、任务历史与旧加密集成迁移尚需补齐 |

普通 Worker 转发动态请求给应用 Durable Object，避免将模板编译、解压、来源解析塞入 10 ms HTTP CPU。应用 DO 和密码 DO 默认 30 秒 CPU，仍需实际指标与压力验收；同实例共享 CPU/内存，大库同步索引可能阻塞其他请求，高并发需继续评估。Queues 每条图片/章节任务有 D1 租约和重试，Cron 恢复丢失消息；Workflows 可替代轮询但不会解决 ONNX 推理，因此本轮保留一个 Queues 状态机。

新 staging/production 使用独立 D1/R2/Queues/DO，名称 `mangadock-full-*`；不与小说试点共用新 schema。代码和 CI 凭据只用环境/Secrets；WebDAV/AniList 数据在 D1 加密。原域名仍不切流，直到全功能、真实数据与 CPU 验收通过。

费用口径：R2 Standard 共用免费 10 GB-month；100 GB 常驻且无其他用量约 $1.35/月存储，备份和重复变体也占额度；Class A/B 超额另计，公网出站免费。应用 staging 256 MB / production 8 GB 预算只是本应用上限，扩大到 10–100 GB 必须明确设置预算并接受可能产生的费用。Queues Free 每日 10,000 operations，逐图发送/读取/删除通常至少 3 次，多章节漫画可能触及额度。D1/DO/Worker 均需遵守账号免费日额度。公开仓库标准 Actions Linux 计算当前免费；私有仓库分钟超额和已有番茄服务器另计。脚本不会主动升级付费套餐。

导入默认 dry-run，先创建含 WAL 的一致 SQLite 快照，再上传每文件内容寻址对象并检查存在/长度，最后写可见元数据。先恢复小批用户/权限/书籍/进度并抽样再扩全库；不复制 Flask 会话。管理员密码默认保留 CI 初始化值。切换前停止旧库写入或记录最终增量；保留备份，切换后进度需反向导出，代码回滚不能自动还原新数据。最终回退实例也应在云服务器运行，才能彻底关闭 Mac mini。

以下保留小说试点阶段原始评估和官方额度链接，标为历史，不能当作完整迁移状态：

---

# 小说试点历史评估

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

小说试点已部署至 https://mangadock-novel-staging.dddinmx.workers.dev ，35 项自动测试与线上真实番茄下载/R2/阅读/进度恢复通过。Cloudflare 实测窗口 264 次请求中 HTTP Worker CPU 最大 9.181 ms，低于 Free 10 ms；仍需扩展压力、边界文件和 Durable Object CPU 采样。完整功能、权限和全库导入尚未完成，不正式切换旧站域名。详见 [验证记录](cloudflare-validation.md)。
