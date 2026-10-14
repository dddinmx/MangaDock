# Task progress repair

The task page previously treated a string log as an array and failed during rendering. Waiting source/child conversions were also labelled as downloads, and task APIs omitted execution times.

Task APIs now return a nonempty log array and an explicit status label. Indexed checks distinguish queued cloud jobs, valid running cloud leases and native chapter downloads. Expired cloud leases remain waiting; paused jobs retain a paused label. The original layout is preserved, and the template also accepts legacy string logs.

Additive migration 0011 records actual execution and terminal times for comics and novels. Repeated state updates retain the first start and terminal time; an explicit retry clears them. Historical start times remain unknown rather than being inferred from submission times. Historical terminal times use the existing update timestamp. A task can still show an unknown start time after migration until its next execution.

Reusing a compute job also sends a scheduler wake, so a deduplicated job can recover a lost wake without inserting another job. This does not remove the scheduler's existing daily budget or guarantee prompt GitHub scheduled runs. Staging/production and credential requirements remain separate.

Regression checks cover queued/root/child tasks, expired leases, native downloads, comic/novel times, historical backfill and retries on workerd/libSQL. Browser checks cover desktop and mobile, string/array logs, literal HTML in logs, a seventeen-minute wait, execution times and stopping after completion. Deployment now validates a real task page in both viewports and saves screenshots and `live-task-results.json`. It prefers the reported comic when present. This page check does not claim that a new source chapter was downloaded.

## Queue quota protection

The reported account alert showed 90% of the daily free Queues operations in use. Both old and current staging queues were paused through the reversible CI control; no billing plan was changed. The alert alone does not identify which queue consumed the operations.

Native comic pages now persist individual outbox rows but send up to ten page IDs per message. A 21-page regression requires three messages rather than 21. Maintenance also batches stale page IDs. Existing single-page messages remain compatible. Queue consumers acknowledge database/rate pauses and recover from the durable outbox later, instead of paying for repeated delivery; other infrastructure errors retry at most once.

All runtime producers go through one persistent Durable Object admission counter per Worker. The default and maximum free configuration is 1,000 newly admitted messages per UTC day, with at most 16 KiB of JSON per message. Rejected sends do not call Queues or repeatedly write the counter. Failed or ambiguous sends still spend their reservation. The cap is conservative for retry costs; it is not a measurement of total account operations and cannot cover historical backlog or other applications. Deferred comic jobs retain their rows and display the queue-budget wait. Admission can be paused until a fixed UTC reset time.

`workers-deploy.yml` accepts `queue_control=pause|resume` to perform only delivery control, without tests, migration or downloads. Pausing covers MangaDock staging queues; resuming covers only the current Turso queue. Normal deployment with `pause_queue_until_reset=true` pauses new sends through the next UTC midnight. The default-branch timer reads the reset timestamp, verifies the deployed commit against the reviewed staging ref/main and resumes only the current queue after reset. GitHub timer delays still apply; old staging queues stay paused.

The deployment smoke test reads task pages while paused. It does not prove future scheduled resume or fresh chapter completion. Stored messages are subject to Cloudflare retention; durable outbox rows are the recovery source after expired messages.

The delivery-control helper also inspects obsolete staging Workers' schedules when pausing them. If present, it removes only those old schedules so they cannot keep producing messages while delivery is paused. Their data and readers are retained; the current Turso Worker timer is retained. This helper/fixture change does not change the deployed Worker JavaScript.

## Upstream comic waiting without Queues

Fanqie comic archives now wait on alarms in the existing SQLite `ApplicationService` Durable Object, using one `remote-polls` instance. Each sweep checks at most five due jobs, with at least 60 seconds between sweeps. Per-job delays remain 60, 120, 240 and then 300 seconds. The existing 30-check, two-hour and five-failed-attempt limits remain. This uses Durable Object requests/storage and Turso queries; it does not make all Cloudflare operations free of quota.

Additive migration 0012 stores `remote_poll_at` and a partial `(remote_poll_at,id)` index. It adopts only active, already-submitted Fanqie comic jobs. Pending polling rows are excluded from Queue recovery. Cancelled, failed and importing/completed jobs leave the polling index automatically; cancelled tasks are never restarted. Old delayed Queue messages remain compatible and use the same lease fence, so they cannot duplicate a simultaneous alarm execution or resubmit the saved upstream job.

The alarm stops when the index is empty. A watchdog, a serialized wake generation and the regular maintenance recovery handle interruptions and lost/racing wakes. Database pauses retain their retry time in DO storage; new wakes cannot shorten that pause. Other sweep errors back off to one hour rather than using immediate platform alarm retries.

Regression tests assert zero Queue sends during repeated upstream waits, including transient failures, and verify bounded batches, cancellation, lost-wake recovery, racing wakes, submission deduplication, R2 storage and exactly one archive conversion job. Actual libSQL query plans use the new partial index without a temporary sort. Novel source polling is unchanged and remains disabled in staging; this change makes no request to the live novel API and requires no change to the external Fanqie server.

## Manual update response contract

Selecting a comic and submitting the original update form could create a task successfully while displaying “请求失败”: the migrated API returned only `task_ids`, but the original frontend requires `data.task_id`. `POST /api/v1/updates` now restores the original singular task ID, task details and HTTP 201, while retaining `task_ids` for existing batch consumers. Empty selections are rejected before job creation, and a single comic name is retained verbatim (including commas). Duplicate active source submissions still return 409 without creating another task.

Workerd regression tests reproduce the old missing-field response and verify the corrected contract, validation and duplicate protection. Desktop/mobile tests submit through the actual local API and follow its task ID to the original progress page. The local test queue is paused so these form tests persist tasks without contacting real sources; their test tasks are removed afterward.

The staging deploy workflow's existing `verify_only` mode can read up to 50 recent update request logs and bounded, indexed task records without starting downloads, deploying or applying migrations. Diagnostic output includes only request metadata and task state/counters; headers, bodies, URL queries and raw provider errors are excluded. The script records unavailable log permissions explicitly instead of treating missing logs as proof of success.
