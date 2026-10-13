# MangaDock Cloudflare 迁移

完整迁移开发分支：`migration/cloudflare-full`。原 Flask 代码、原 `templates/` 和 `static/` 没有改动，小说试点分支与测试站继续保留。这里的实现已接入原界面和主要后端模块，仍需完成真实来源、边界文件和全库验收，不能把本地测试当作正式切流完成。

## 原版界面

构建时预编译全部 34 个原 Jinja 模板（包含公共模板与局部模板），请求时传入 D1 数据；直接使用原 CSS、字体、图标和前端脚本。保留首页、漫画/小说书架、详情、阅读器、登录、搜索、历史、统计、任务、用户/分组管理、设置等页面。转换层兼容 Jinja 的真假判断、字典 get、切片、日期、URL 和 JSON 转义。

`test/browser/parity.spec.ts` 使用未改动的原模板和 Python Jinja 生成参考页面，在同一 Chromium、同一数据、同一资源下对比 1280px 和 390px 截图。目前覆盖 23 个页面、46 组截图。测试等待图片解码，允许少量抗锯齿差异，不放宽缺失内容或布局差异。CI 保存实际、参考和失败差异截图。

## 本地开发与检查

```bash
cd workers
npm ci
python -m pip install Pillow==10.4.0 Jinja2==3.1.6
npx playwright install --with-deps chromium
npm run verify
```

`npm run verify` 包括构建、TypeScript、真实 workerd/D1/R2 接口测试、原模板截图对照、阅读器浏览器测试和 Python 迁移测试。模拟来源测试与线上真实来源验收分别记录。

```bash
npm run source:register
node scripts/seed-local.mjs
npm run dev
```

来源凭据保存在忽略提交的 `.dev.vars`。测试账号密码只用于本地合成书库，线上管理员使用 CI Secret。自定义 Python/Chromium 可设置 `MANGADOCK_REFERENCE_PYTHON` 和 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。

## 部署与云端超分

沿用 Repository Secrets：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`MANGADOCK_ADMIN_PASSWORD`。可选 `FANQIE_API_TOKEN` / `FANQIE_INSTANCE_ID` 一起配置；缺失时首次部署注册独立实例，后续保留 Worker Secrets。WebDAV/AniList 的加密密钥自动生成并保存到 Worker Secret，不提交仓库。

因 D1 配额事故，推送完整迁移分支只运行本地模拟测试；自动 staging 部署和定时云转换已暂停。远程发布前会通过 Analytics 检查账号当日行数预算，读取达 400 万或写入达 8 万即停止，无法读取统计也停止。资源使用 `mangadock-full-staging` / `mangadock-full-production`；不改旧小说试点资源，不修改现有 DNS，不升级 Workers Paid。

正式发布通过 **Deploy Workers (free plan)** 手动选择 `production` 和已验收提交。数据库采用追加迁移；代码回滚不等于数据回滚。

普通 HTTP Worker 负责转发，密码校验与应用逻辑分别运行在 SQLite Durable Object，D1 保存状态，R2 保存私有文件，Queues 按章节/图片拆分下载，Cron 恢复未完成消息。DO 单次 CPU 默认 30 秒，HTTP Worker Free 是 10 ms，必须分别核验，不能把 wall time 当 CPU。

ONNX、PDF/CBZ 转换、超大 EPUB 索引以及来源兼容解析由 `cloud-compute.py` 在 GitHub Actions Linux 执行。封面继续使用原 RealESRGAN 动漫模型；失败或超时时按原方案退回 Pillow。产物通过管理员认证接口写入 R2；任务有领取租约、文件大小检查和章节版本检查。

新增 **Cloud conversions and cover super-resolution** 工作流，当前只允许手动运行，定时触发已因配额事故关闭；恢复后需重新配置有额度监测的调度。开发分支部署时直接执行转换，不把未合并的 schedule 声称为已自动运行。公共仓库标准 Linux runner 当前免费；私有仓库超过 Actions 分钟额度、Cloudflare 超额资源以及现有番茄服务器不能承诺免费。详细费用见迁移文档。

## 数据迁移

```bash
python workers/scripts/import-library.py \
  --database /private/backup/app.db \
  --comics /private/backup/comic \
  --novels /private/backup/小说 \
  --covers /private/backup/cover
```

默认只规划，不上传。确认目标预算后，在环境中配置 `MANGADOCK_ADMIN_PASSWORD`，增加 `--url https://目标站 --apply` 执行。保留旧密码哈希、分组/权限、原作品和章节标识、进度锚点、阅读时长、封面变体；管理员现有密码默认保留，显式 `--restore-admin-password` 才覆盖。大文件使用 5 MB 分片、每片 SHA-256、长度和有序分片检查。整文件 SHA-256 来自备份端清单，不把分片校验说成服务端复算全文件。

先用 SQLite backup API 创建含 WAL 的一致快照。原备份不可提交公共仓库。当前没有用户实际数据库和 10–100 GB 文件，因此未执行全库迁移。导入器当前处理 CBZ/PDF 章节和 EPUB；直接图片目录、旧任务历史、旧加密 AniList/WebDAV 凭据迁移还需补齐/重新连接。详见验收与剩余工作。

- [迁移架构、费用和回退](../docs/cloudflare-migration.md)
- [真实验证记录](../docs/cloudflare-validation.md)
