# 与已有 Cloudflare 分支的交叉对比

比较对象：`origin/cf-workers-migration` 的 `4ecf5914bb09164bf8a20b6129ab2f08943d91c7`（2026-10-12）。本分支保留原 main `dca45d0`，没有覆盖原迁移分支或其 `pre-cf-migration` 回退标签。

| 方面 | 已有分支 | 本分支处理 |
| --- | --- | --- |
| 首个来源 | baozimh.org 漫画，逐图 R2 | 补番茄云服务小说 EPUB 闭环；两条来源管线不能当作全部来源已移植 |
| 原版界面 | 最小 HTML，加载部分原 CSS | 构建原登录模板与原小说阅读器，保留翻页、字体、主题和进度恢复 |
| 旧数据迁移 | 完整 SQLite 同构 schema，CBZ 拆页、文件夹章、增量清单与 SQL 导出 | 复用 `tools/cf/` 原脚本及 18 项测试；小说清单与 WAL 安全快照由新脚本补充。旧脚本输出面向原 `cf_*` schema，不可直接灌入新小说库 |
| 密码安全与免费 CPU | 文档记录 100k PBKDF2 登录 CPU 23–50 ms，超过 HTTP Worker 10 ms；默认接受偶尔超限 | 不接受超限作为免费方案验收；改为免费 SQLite Durable Object 验证，新账号 600k，兼容旧 260k |
| 下载幂等与失败恢复 | R2 HEAD 避免重复对象，Queue retry | 新小说管线用 D1 状态、租约、唯一活跃来源索引、Cron outbox；来源不支持幂等 POST 时不盲目重试不明提交 |
| 费用保护 | 记录 R2 10 GB 免费与超额估算 | staging/production 分离预算，原子传输预留；失败预留需清理后释放。账号其他资源仍需审计 |
| R2 实测证据 | 文档说部署过 staging，但没有 R2 binding；R2/Queue 只本地测试 | 不把既有“closed loop working”标题当成 Cloudflare R2 已通过的证据。用户已确认现在开通 R2，需用 CI 实测 |
| 现有站点 | `mangadock-staging.dddinmx.workers.dev` | 新小说试点使用 `mangadock-novel-staging`，正式发布名 `mangadock-cloud`；保留旧漫画 staging |
| 完整权限/功能 | 分组、18+、统计、其他来源未移植 | 仍为迁移阻塞；不可跳过权限直接导入全库 |

复用来源：[原提交](https://github.com/dddinmx/MangaDock/commit/4ecf5914bb09164bf8a20b6129ab2f08943d91c7)。上述 CPU、原数据规模及旧站部署结果来自已有分支文档，本次没有把它们重新测量或当成自己的验收结果。新实现的真实结果另行记录。
