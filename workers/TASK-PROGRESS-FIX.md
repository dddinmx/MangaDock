# Task progress repair

The task page previously treated a string log as an array and failed during rendering. Waiting source/child conversions were also labelled as downloads, and task APIs omitted execution times.

Task APIs now return a nonempty log array and an explicit status label. Indexed checks distinguish queued cloud jobs, valid running cloud leases and native chapter downloads. Expired cloud leases remain waiting; paused jobs retain a paused label. The original layout is preserved, and the template also accepts legacy string logs.

Additive migration 0011 records actual execution and terminal times for comics and novels. Repeated state updates retain the first start and terminal time; an explicit retry clears them. Historical start times remain unknown rather than being inferred from submission times. Historical terminal times use the existing update timestamp. A task can still show an unknown start time after migration until its next execution.

Reusing a compute job also sends a scheduler wake, so a deduplicated job can recover a lost wake without inserting another job. This does not remove the scheduler's existing daily budget or guarantee prompt GitHub scheduled runs. Staging/production and credential requirements remain separate.

Regression checks cover queued/root/child tasks, expired leases, native downloads, comic/novel times, historical backfill and retries on workerd/libSQL. Browser checks cover desktop and mobile, string/array logs, literal HTML in logs, a seventeen-minute wait, execution times and stopping after completion. Deployment now validates a real task page in both viewports and saves screenshots and `live-task-results.json`. It prefers the reported comic when present. This page check does not claim that a new source chapter was downloaded.
