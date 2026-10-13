# PR #13 follow-up fixes

The seven findings from the review of `a2949e2` are addressed here. The original Flask backend remains available; optional template additions are guarded so its rendering still works.

| Finding | Result |
| --- | --- |
| Update queue bypassed database protection | Both update message kinds run inside the `updates` ApplicationService instance with a persisted Turso budget and circuit breaker. Exhausted requests make no provider calls. |
| Whole-book download silently stopped at 20 chapters | Missing/zero `max_chapters` means a whole book. A persisted source plan admits 20 chapters at a time, waits for the batch, and automatically continues. Deterministic child IDs tolerate redelivery; cancellation and errors stop continuation. Explicit first-N acceptance windows remain supported, up to the parser's 10,000-chapter directory limit. Updates fill missing chapters in order. |
| Novel-only groups could not be granted | Novel ACLs have their own table and controls in the existing user page. Saving comic grants does not remove novel grants or grant access to same-name novels. Saving novel grants does not grant comic access. |
| Update checks reported success before processing | Per-user checks and idempotent items persist state, leases and totals. Completion requires paging and all items to finish. Lost messages are recovered in bounded maintenance, failed items stop after three attempts, and checks expire after 24 hours. Results paginate by ID; transient history has seven-day retention. |
| Indexed chapter recounts still grew with the library | Chapter and task counters use incremental triggers, including moves/deletes. Runtime recounts are removed. Status-only polling reads counters rather than the shelf or candidate arrays. |
| Request-time EPUB decompression was still active | Missing indexes enqueue one cloud job and show a waiting view. Readers only load generated indexes and chapter data. Failed input does not create endless replacements; an explicit administrator compute-discovery run can retry. The cloud parser now has the imports its reused original functions require. |
| Ten-minute polling deadline hid later completion | Visible active tasks continue at low frequency beyond the executor wait. Completion stops polling; hidden pages stop requests; repeated failures show a retry notice. Retry-After takes precedence over ordinary polling delays. |

## Migration and rollback

Migration `0010_review_regressions.sql` is additive. It leaves books, objects and reading progress intact. It adds independent novel permissions, update-check state and cached task totals, and adjusts incremental triggers. Before the novel ACL feature, existing users could read all existing novels; this access is copied **only into novel permissions**, never into comic permissions. The import tool applies the same policy to legacy users/groups. New users require explicit novel grants. Administrators can revoke those grants in the user page.

Existing active duplicate EPUB index jobs are reduced to one per input and fenced off. Completed reading indexes remain usable. Backfill queries execute once during migration; they are not part of polling. The previous database migrations and their checksums are unchanged. Roll back application code against the additive schema if necessary; retain a database backup because code rollback does not reverse migrations or user grants.

## Automatic execution and deployment configuration

The automatic launcher supplies **staging and production** independently. It still checks every 15 minutes and processes at most five jobs per active run, with the existing deadlines and 24-run daily cap. Native downloads also remain subject to Queues and provider free allowances; batching does not increase those allowances.

1. Configure the existing Cloudflare, Turso and administrator CI Secrets for each environment. Production uses a separate Turso database.
2. Set `COMPUTE_WAKE_TOKEN` to an independently generated random value of at least 32 characters in GitHub Secrets. The deployment writes the **same supplied value** into the Worker. No inaccessible random token is generated at deployment. Missing/malformed credentials fail before schema or resource changes.
3. Production executes only code on `main` or its ancestors. Staging may also execute exactly the reviewed 40-character SHA in the `staging` environment variable `MANGADOCK_APPROVED_COMPUTE_SHA`. It accepts neither an arbitrary branch nor an arbitrary Worker-supplied SHA. The launcher must be on GitHub's default branch for cron execution; coordinate launcher, approved SHA, secret and Worker releases.
4. Enable production timers after its deployment and acceptance by setting the `production` environment variable `MANGADOCK_PRODUCTION_COMPUTE_ENABLED=true`. They are otherwise skipped before credential or network access. Manual execution selects one environment explicitly.
5. Existing database defaults stay at 10,000 requests / 50,000 SQL statements per instance per UTC day. Override using the existing deployment variables. `MANGADOCK_MAX_LIBRARY_BYTES` can explicitly set a larger library storage budget; staging defaults to 256 MB and production to 8 GB. R2 storage above its shared 10 GB free allowance may cost money. No plan upgrades are performed.

The deployment workflow has `preflight_only=true`: it reports **credential presence and format**, without contacting Cloudflare, Turso or the novel API. It does not prove token permissions, the approved-ref setting, or a live executor's authentication. `verify_only` remains read-only.

## Validation boundaries

Regression coverage includes actual libSQL protocol requests through the budget, 45 chapters processed as 20/20/5, cancellation, private novel grants/revocation, constant-cost counters with 500 ready chapters, idempotent update messages, maintenance recovery, original-paragraph cloud EPUB parsing, ref trust with temporary Git repositories, early credential refusal, and browsers advanced beyond fifteen minutes. The original desktop/mobile screenshot comparisons remain in the suite.

These are local isolated tests until a new deployment and a fresh webpage-submitted automatic job are accepted online. Old CPU measurements do not certify this new commit. The paused Fanqie API is not enabled or exercised by this change. Full 10–100 GB migration and formal traffic switching still require their own acceptance.
