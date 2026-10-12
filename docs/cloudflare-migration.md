# MangaDock → Cloudflare Workers migration

Status: **closed loop working** (login → download from a real source → R2 → read pages → reading progress in D1).
Branch `cf-workers-migration`. `main` is untouched and tagged `pre-cf-migration` for rollback;
the Flask/Docker deployment keeps working as before until cut-over.

Limits/prices below were checked on 2026-10-12 against the Cloudflare docs linked in [Sources](#sources).

## 1. Architecture mapping

| Flask app (today, Mac mini) | Cloudflare (this branch) | Notes |
|---|---|---|
| Flask + Gunicorn routes | Worker `cloudflare/src/index.ts` | Same URLs where ported (`/login`, `/save_progress`, `/get_progress/<name>`, `/api/chapters/<name>`, `/api/comic-pages/<key>`, `/reader/...`). |
| SQLite `instance/download_tasks.db` | **D1** | `migrations/0001_existing_schema.sql` is the real schema dumped from production SQLite, column-for-column, so data imports unchanged. `0002_cf_library.sql` adds `cf_comic` / `cf_chapter` (library index that used to come from scanning the filesystem). |
| `COMIC_ROOT` files (CBZ/PDF/images) | **R2** bucket, key `comics/<comic>/<chapter_base>/<NNN>.<ext>` | Pages are stored as individual objects (CBZ unpacked at migration time) so the reader is a plain R2 GET — no zip parsing in the Worker (CPU). |
| Background subprocess workers (`services/workers.py`, `BackgroundCommand`) | **Queues** (`mangadock-downloads-*`), consumer in the same Worker | Fan-out: `source` → `chapter` → `pages` (≤20 images/message) so each invocation stays under the free plan's 50 external subrequests. Idempotent retries (HEAD before PUT). |
| `static/` | **Workers Static Assets** (`scripts/prepare-assets.sh` copies `../static` → `public/static`) | Same `/static/...` URLs; asset requests are free and don't invoke the Worker. 100 files, 33 MB. |
| Jinja templates | Minimal server-rendered HTML in `src/html.ts` that loads the existing CSS | **Not full parity.** See §4. |
| Flask session cookie | HMAC-signed cookie (`SESSION_SECRET` secret), honours `user.session_version` | CSRF: same-origin `Origin`/`Referer` check on every POST + `SameSite=Lax`. |
| fanqie.dddinmx.cn | unchanged, call over HTTPS `fetch()` | Not yet ported (see §4). |

Workflows were considered instead of Queues. Queues was chosen for the download pipeline because each unit
of work is small and independent; Workflows (free plan: 3,000 steps/day, 1 GB state) is the better fit for
long multi-step jobs such as "update all subscribed comics" and can be added later.

## 2. Free-tier analysis

| Product | Free plan (verified) | MangaDock need | Free? |
|---|---|---|---|
| Workers requests | 100,000/day | personal use: a few thousand/day (each page image is one Worker request) | ✅ |
| Workers CPU | **10 ms / HTTP request**, 10 ms / cron | measured below: everything except login is 1–6 ms | ⚠️ login is 23–50 ms (see §3) |
| Workers subrequests | 50 external, 1,000 to CF services / invocation | download fan-out keeps ≤ ~21 external fetches per message | ✅ |
| Queues | 10,000 operations/day (≈3 ops/message), 24 h retention | 1 chapter of 120 pages ≈ 1 + 6 messages ≈ 21 ops | ✅ for ~400 chapters/day |
| Workflows | 100k req/day shared, 3,000 steps/day, 1 GB-month | not used yet | ✅ |
| D1 | 5 M rows read/day, 100k rows written/day, 5 GB total | current DB ≈ 4.5k rows, <1 MB | ✅ |
| Static Assets | 20,000 files/version, 25 MiB/file | 100 files | ✅ |
| **R2 storage** | **10 GB-month free**, then **$0.015/GB-month** (Standard) | **library is 10–100 GB** | ❌ above 10 GB |
| R2 ops | 1 M Class A, 10 M Class B / month free; egress free | migration of 50 GB ≈ 250k PUTs; reading ≈ 1 GET/page | ✅ |

**What cannot be free:** R2 storage beyond 10 GB. Estimated monthly cost (Standard storage, ops within free tier):

| Library size | Billable | Cost/month |
|---|---|---|
| 10 GB | 0 GB | $0 |
| 25 GB | 15 GB | ≈ $0.23 |
| 50 GB | 40 GB | ≈ $0.60 |
| 100 GB | 90 GB | ≈ $1.35 |

R2 has to be enabled once in the dashboard (it asks for a payment method even for the free tier) — the API returned
`Please enable R2 through the Cloudflare Dashboard. [code: 10042]` for this account on 2026-10-12.
Infrequent Access ($0.01/GB) is *not* covered by the free tier and adds retrieval fees, so it only pays off for
cold archives.

Optional paid upgrade: **Workers Paid ($5/month)** removes the 10 ms CPU cap (default 30 s) and raises subrequests
to 10,000. It is only needed if the ported heavy features (novel EPUB building, search across many providers,
in-Worker image processing) exceed 10 ms; the closed loop does not need it.

## 3. Measured CPU time (real Cloudflare, staging)

Deployed `mangadock-staging` to `https://mangadock-staging.dddinmx.workers.dev` (2026-10-12 ~23:45 UTC+8),
**without the R2 binding** (R2 not enabled on the account), and drove 66 requests with curl. Per-invocation
`cpuTimeMs` from Workers Observability (telemetry query API):

| Route | n | min | median | max |
|---|---|---|---|---|
| `POST /login` (PBKDF2-SHA256, 100k iterations) | 6 | 23 ms | 25 ms | 50 ms |
| `GET /` (library, D1 query + HTML) | 19 | 1 ms | 2 ms | 6 ms |
| `POST /save_progress` (D1 upsert) | 20 | 1 ms | 3 ms | 6 ms |
| `GET /get_progress/*` (D1 read) | 20 | 1 ms | 2 ms | 4 ms |
| `GET /healthz` | 1 | 0 ms | 0 ms | 0 ms |

GraphQL `workersInvocationsAdaptive` for the same window: 62 requests, 0 errors, cpuTime p50 3.1 ms, p90 7.5 ms, max 50.4 ms.
Worker startup time 2 ms, bundle 29 KiB.

**Not measured on Cloudflare:** R2 page serving and the queue consumer (needs R2). They were exercised only in local
`wrangler dev` (workerd), which does not report CPU time. Their work is I/O (fetch → `arrayBuffer` → R2 put, R2 get →
stream), so they are expected to be in the low-ms range, but that is an expectation, not a measurement.

**Login exceeds 10 ms.** Cloudflare tolerates occasional overruns but does not guarantee them. Options:
(a) accept it (logins are rare; sessions last 30 days); (b) Workers Paid; (c) lower `PBKDF2_ITERATIONS` (weaker);
(d) move login to a passkey/Cloudflare Access front door. Default: (a), re-evaluate with production metrics.

### Existing password hashes
Flask hashes are `pbkdf2:sha256:260000$…`. Workers' WebCrypto **refuses PBKDF2 > 100,000 iterations** (it throws),
so these cannot be verified in a Worker. The Worker detects this and shows a reset message. Reset with
`MD_PASSWORD=… node cloudflare/scripts/set-password.mjs <user> > reset.sql && wrangler d1 execute … --file reset.sql`.
The new hash (100k iterations) is werkzeug-compatible in both directions (tested), so rolling back to Flask keeps working.

## 4. Not ported yet / cannot run in Workers, and alternatives

| Feature | Why | Proposed alternative |
|---|---|---|
| ONNX cover super-resolution (`utils/cover_enhance.py`) | no native ONNX runtime, 128 MB memory, CPU cap | (1) run in GitHub Actions on new covers (cron job: list R2 `cover*` without `enhanced` metadata → onnxruntime → put back); (2) Workers AI image models (paid beyond free neurons); (3) client-side WebGPU/onnxruntime-web in the browser. Recommended: (1). |
| Subprocess workers / `BackgroundCommand` | no processes | Queues (done for downloads); Workflows for long jobs; Cron Triggers for periodic update checks. |
| CBZ/PDF/EPUB **generation** on download | zip/PDF building is CPU-heavy | store pages as objects (done); build CBZ on demand client-side (fflate in browser) or in a GitHub Action for exports. |
| Other comic providers (mxs, baozimhcn, hipmh, guazimanhua, kalamanhua, manhuagui) | not ported; baozimhcn needs a SHA-256 proof-of-work (CPU-heavy, would break 10 ms) | port HTML/JSON providers one by one with fetch + regex/HTMLRewriter; PoW-protected sites → Workers Paid or an external helper. |
| AList → Quark drive source / WebDAV library (`services/webdav*.py`) | AList currently runs on the Mac mini | point the Worker at an AList instance on the cloud server (same box as fanqie) over HTTPS, or move those files into R2 with the migration script. |
| Novels / fanqie (`services/fanqie*.py`, `novels.py`) | not ported | plain HTTPS `fetch()` to fanqie.dddinmx.cn; chapter text in R2 or D1. |
| AniList progress sync | not ported | `fetch()` to AniList GraphQL from `save_progress` via `ctx.waitUntil`, rate-limit state in D1. |
| Full UI (`templates/*.html`, 11.7k lines Jinja) | Jinja can't run in Workers | port to Nunjucks (Jinja-compatible, precompiled at build time) or render client-side against the JSON API; current pages are minimal and reuse the existing CSS. |
| Group permissions, adult-content gating, admin/user pages, statistics | not ported | straight D1 queries; schema already present. |
| MangaUpdates/AniList metadata, update checks | not ported | Cron Trigger → Queue. |

## 5. Data migration plan

Tooling: `tools/cf/migrate_to_cloudflare.py` (stdlib only, unit tested).

1. **Create resources** (once): `wrangler d1 create mangadock-prod`, enable R2 and `wrangler r2 bucket create mangadock-library`,
   `wrangler queues create mangadock-downloads`; put the D1 id in `wrangler.jsonc`; `wrangler secret put SESSION_SECRET --env production`.
2. **Schema:** `wrangler d1 migrations apply DB --remote --env production`.
3. **DB rows:** `python tools/cf/migrate_to_cloudflare.py export-d1 --db instance/download_tasks.db --out export.sql`
   then `wrangler d1 execute DB --remote --env production --file export.sql`. (`INSERT OR REPLACE`, re-runnable; skips login_log/login_failure/background_command by default.)
4. **Files:** `python tools/cf/migrate_to_cloudflare.py stage-r2 --comic-root <COMIC_ROOT> --out stage --sql-out library.sql --state stage-state.json`
   unpacks CBZ/image dirs into the R2 key layout and writes `cf_comic`/`cf_chapter` rows. Upload with
   `rclone copy stage r2:mangadock-library --checksum --transfers 16` (R2 S3 API token in env, see `print-upload-commands`), then
   `wrangler d1 execute … --file library.sql`.
5. **Incremental:** re-run steps 3–4 any time; `--state` skips unchanged source files and rclone only uploads changed objects.
   Final cut-over: stop the Flask app, run 3–4 one last time, reset passwords, switch DNS/route.
6. **Rollback:** keep the Mac mini/Docker deployment and `main` (`pre-cf-migration` tag) until the Worker has run cleanly for a while.

Tested locally on a copy of the real data: export → local D1 import (3,888 reading_time, 17 reading_progress, 1 user rows; 19 statements OK);
stage-r2 on the real 4-comic library: 484 chapters, 23,648 pages, 5.37 GB in 35 s; second run 0.5 s (incremental); `library.sql` imported, R2 keys exist.
PDF/EPUB chapters are skipped with a warning (not supported in the page reader yet).

## 6. CI/CD (`.github/workflows/cloudflare.yml`)

- PR: `npm ci`, `wrangler types`, `tsc`, `vitest` (Workers runtime via `@cloudflare/vitest-plugin`), migration-script unittests.
- Push to `cf-workers-migration`: test → apply D1 migrations → deploy `--env staging`.
- Tag `cf-v*` or manual dispatch → `production` GitHub environment (configure *Required reviewers* for manual approval).
- Secrets: `CLOUDFLARE_API_TOKEN` (Workers Scripts:Edit, D1:Edit, Queues:Edit, R2:Edit, Account Settings:Read), `CLOUDFLARE_ACCOUNT_ID`.
  `SESSION_SECRET` is a Worker secret (`wrangler secret put`), not a GitHub secret.
- Codex Cloud: `cd cloudflare && npm ci && npx vitest run` needs no Cloudflare credentials.

## 7. Local development

```
cd cloudflare && npm ci && ./scripts/prepare-assets.sh
echo "SESSION_SECRET=$(openssl rand -hex 32)" > .dev.vars
npx wrangler d1 migrations apply DB --local
MD_PASSWORD=… node scripts/set-password.mjs admin --create-admin > admin.local.sql && npx wrangler d1 execute DB --local --file admin.local.sql
npx wrangler dev
```

## Sources
- Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- R2 pricing: https://developers.cloudflare.com/r2/pricing/
- D1 pricing: https://developers.cloudflare.com/d1/platform/pricing/
- Queues pricing: https://developers.cloudflare.com/queues/platform/pricing/
- Workflows pricing: https://developers.cloudflare.com/workflows/reference/pricing/
- PBKDF2 100k cap: https://github.com/cloudflare/workerd/issues/1346
