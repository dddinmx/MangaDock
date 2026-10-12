# MangaDock Workers migration

本目录是独立的 Cloudflare 迁移实现；仓库根目录的 Flask/Gunicorn 版本仍可运行。当前是首阶段小说闭环，不能替代全部旧功能，也没有迁移个人书库。

## 本地开发

```bash
cd workers
npm ci
npm run build
npm run check
npm test
npx playwright install --with-deps chromium
npm run test:e2e
```

测试在 workerd 中使用真实 D1/R2 实现，来源 HTTP 在接口测试中模拟。浏览器测试使用合成 EPUB，验证原版登录模板、原版小说阅读器、翻页、跨刷新进度恢复及 EPUB 脚本隔离。真实来源测试必须单独执行，不能把模拟测试当作真实下载成功。

首次本地接入现有番茄云服务：

```bash
npm run source:register
node scripts/seed-local.mjs
npm run dev
```

`source:register` 复用原服务 `/v1/register` 协议，把令牌保存到忽略提交的 `.dev.vars`（0600），不会打印令牌。测试用管理员密码只在本地 seed 中使用，生产账号从 CI Secret 初始化。受代理管理的 Node 24 环境执行来源脚本时设置 `NODE_USE_ENV_PROXY=1`。

## CI 和部署

Repository Secrets：

| 名称 | 用途 |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | 目标账号的 Workers Scripts、D1、R2、Queues 编辑权限及 Account Settings 读取权限 |
| `CLOUDFLARE_ACCOUNT_ID` | 目标账号 ID |
| `MANGADOCK_ADMIN_PASSWORD` | 首次初始化 `admin`，至少 6 字符（沿用原项目规则）。后续部署不会重设密码 |
| `FANQIE_API_TOKEN` / `FANQIE_INSTANCE_ID` | 可选，复用来源服务现有凭据；两者应一起设置 |

没有来源凭据时，首次部署使用随机实例 ID 和随机 256-bit 注册密钥向现有来源服务注册；把 Token / Instance ID 写入 Worker Secrets，后续部署保留这些 Secrets，不重复注册，也不从用户密码派生服务密钥。注册密钥不进入仓库或日志。若 Worker Secrets 丢失，可重新注册独立实例或提供显式来源凭据。

推送迁移分支自动运行测试、部署 staging。正式发布：在 GitHub Actions 中手动运行 **Deploy Workers (free plan)**，选择 `production`，选择已验证的提交。可给 GitHub `production` environment 设置 required reviewers。staging/production 使用不同 D1、R2 和 Queue。不要在公开 PR/分叉工作流中注入 Secrets。

部署脚本会创建资源、执行 D1 migrations、初始化账号、发布 Worker、写入来源 Secrets，输出 `workers.dev` 地址。它不会开通 Workers Paid、更改套餐或配置现有自定义域名。R2 必须由账号所有者先开通，可能需要支付方式；资源 API 失败时不会自动购买套餐。

若只运行 `wrangler deploy --env ...` 而未生成资源配置，会因缺少绑定失败。请运行 `npm run bootstrap -- staging` / `production`，或使用提供的工作流。

`MANGADOCK_URL=... MANGADOCK_ADMIN_PASSWORD=... SOURCE_BOOK_ID=... SOURCE_CHAPTER_IDS=... npm run test:live` 执行真实登录、来源下载、R2 文件读取、进度保存。`SOURCE_CHAPTER_IDS` 是逗号分隔的真实来源章节 ID；省略会下载全书（仍受 32 MB 限制）。测试写入该管理员的进度，仅对测试作品运行。返回的 `wall_ms` 是耗时，**不是 Cloudflare CPU 用量**。线上 CPU 验收见架构文档。

## 第一阶段限制

- 仅接入番茄小说 EPUB；每本最多 32 MB、单章解压后最多 2 MB，浏览器解压及正文纯文本呈现。
- 小说阅读器从原模板生成，保留颜色、字体、横向翻页、纵向滚动；目录暂显示序号。书架是使用原 CSS 的首阶段页面，封面和旧首页完整布局待迁移。
- 仅管理员下载；书库对已初始化的账号共享。旧版分组权限、18+ 权限、用户管理尚未迁移，**不得直接导入旧账号/受限书库并公开访问**。
- 首次下载完成后重复下载被拒绝；更新、取消、重试 UI 待迁移。来源提交响应丢失时不自动重发，避免创建重复来源作业；D1 标记错误以供人工核对。
- staging 书库预算 256 MB，production 8 GB；这只限制此实现的已入库对象及传输预留，不能保证整个账号其他 R2 bucket 没有费用。没有完成整个账号用量审计时，不能承诺零账单。
- 密码验证交给免费 SQLite Durable Object（30 秒默认 CPU 限额），HTTP Worker 只等待结果。新账号 PBKDF2 SHA-256 600,000 次，支持旧 Werkzeug 260,000 次哈希，避免普通 Worker 10 ms CPU 限额以及 WebCrypto 100,000 次上限。身份验证及来源下载的线上 CPU 仍需实际验收。

详细功能差异、费用、迁移和回退方案：[`docs/cloudflare-migration.md`](../docs/cloudflare-migration.md)。
