# Review fixes and operational limits

The ten findings in the 2026-10-13 review are addressed on `fix/review-hardening`:

| Area | New behavior |
| --- | --- |
| Task polling / statistics | Two SQL calls for a task or task list; three for statistics, including permission filtering. Detail fetches only the selected task. |
| Refused login | DO-local rate guard runs before any metered database access. Login has its own application instance. |
| Cancellation / late callbacks | Claim tokens fence SQL mutations, ancestors are checked, and each compute execution has a separate output directory. |
| Cloud executor | Automatic Actions launcher checks a DO-local pending flag and the deployed commit SHA. Idle probes do not query Turso/D1. |
| Delete / storage recovery | Owned conversion, imported and comic objects enter a persistent, reference-checked cleanup plan. External files and shared imports are retained. |
| Failed uploads | Exclusive reservation owners, failed-write reconciliation and bounded expired multipart cleanup. |
| Reading progress | Confirmed server success is required; pending snapshots remain local; retries are bounded and obey Retry-After. Normal layouts are preserved. |
| Archive inflation | Compressed input is processed in small chunks and actual output is bounded before accepting a page. |
| WebDAV | Pause retains queued checkpoints; resume requeues them; stop cancels queued work. |
| Partial failures | Common parent aggregation preserves error/cancel states instead of declaring every terminal child successful. |

## Database budgets

Deployment defaults are **10,000 outbound Turso requests and 50,000 SQL statements per application instance per UTC day**. Login, interactive reading, download processing and conversion use separate instances. These are application safety limits, not Turso's provider quotas. A batch can contain several statements. Reaching a limit returns 503 and Retry-After; the UTC daily boundary is **08:00 Asia/Shanghai**. Provider quota errors trigger the longer circuit breaker.

Override the deployment defaults through GitHub Actions variables `MANGADOCK_DB_DAILY_REQUESTS` and `MANGADOCK_DB_DAILY_STATEMENTS`, then redeploy the same Worker. Changing these values does not buy additional provider quota. The libSQL client does not expose billed rows scanned, so the application cannot guarantee a fixed provider row usage from statement counts alone. Large libraries and unusually frequent use still require real usage monitoring.

## Automatic Python conversions

`workers-compute-auto.yml` must be present on GitHub's default branch because scheduled workflows only run there. The launcher targets **staging**, checks every 15 minutes, and only checks out / installs conversion dependencies when the local pending flag is set. It uses the exact commit SHA recorded by the deployed Worker, with existing Cloudflare and administrator CI secrets. No new personal token is required.

One run processes at most **five** jobs within an eight-minute processing deadline. An individual job is limited to four minutes; its subprocess group is stopped on timeout. The persisted scheduler allows at most **24 active runs per UTC day**. Failed jobs and expired claims cannot retry indefinitely. These caps can cause a large conversion backlog to take several days. A job requiring compatibility parsing or conversion can wait for a scheduled run; this is not the earlier CI-driven 77.6-second timing guarantee. GitHub can also delay scheduled runs.

This repository is public, so standard hosted Linux Actions runners are free under GitHub's public-repository offering. Forking privately changes Actions-minute billing. R2 storage above the shared free allowance and any provider usage above its free allowance remain potential paid components; this change does not subscribe to a paid plan.

Cron-based D1 polling remains disabled. The Fanqie novel API is not enabled or tested online by this release. The original Flask application and pre-fix commits remain available; migration 0008 adds columns, indexes and a garbage-collection table without deleting books or user progress. Older conversion executors are intentionally requeued and must use the deployed version of the new lease protocol.
