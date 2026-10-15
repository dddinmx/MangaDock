import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  mkdtempSync,
  writeFileSync,
  rmSync,
  chmodSync,
  mkdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const workflow = new URL(
  "../../.github/workflows/workers-compute.yml",
  import.meta.url,
);
test("isolated staging rollout skips every owner-progress acceptance while retaining deployment and scoped whole-book verification", () => {
  const source = readFileSync(
    new URL("../../.github/workflows/workers-deploy.yml", import.meta.url),
    "utf8",
  );
  const blocks = source.split(/\n      - /);
  const inputs = {
    runtime_only: true,
    environment: "staging",
    complete_comic_id: "c_ee02de2c2194",
    diagnostic_task_id: "c6fe02d4-c454-4111-b962-495120175313",
    queue_control: "none",
  };
  const allowed = (block, values = inputs) => {
    const condition = block.match(/\n        if: \$\{\{ (.*) \}\}/)?.[1];
    return condition
      ? Boolean(
          Function(
            "inputs",
            "success",
            "cancelled",
            "hashFiles",
            "steps",
            `return (${condition})`,
          )(
            values,
            () => true,
            () => false,
            () => "fixture",
            { cloud_compute: { conclusion: "success" } },
          ),
        )
      : true;
  };
  const owner = blocks.filter((block) =>
    /run: (?:npm run test:live|node scripts\/(?:latency-report|live-browser|live-comic|live-comic-browser|live-task-browser)\.mjs)/.test(
      block,
    ),
  );
  assert.ok(owner.length >= 7);
  for (const block of owner)
    assert.equal(allowed(block), false, block.split("\n")[0]);
  for (const name of [
    "Provision and deploy without upgrading plan",
    "Wait for provider recovery without changing original user progress",
    "Verify the complete existing comic, every R2 image and the last chapter reader",
  ])
    assert.equal(
      allowed(blocks.find((block) => block.startsWith("name: " + name))),
      true,
      name,
    );
  const guard = blocks.find((block) =>
    block.startsWith("name: Restrict isolated deployment"),
  );
  assert.equal(allowed(guard), false);
  const historicalGuard = blocks.find((block) =>
    block.startsWith("name: Restrict historical image repair"),
  );
  const repair = blocks.find((block) =>
    block.startsWith("name: Repair only audited historical missing images"),
  );
  const repairInputs = { ...inputs, repair_legacy_images: true };
  assert.equal(allowed(historicalGuard, repairInputs), false);
  assert.equal(allowed(repair, repairInputs), true);
  assert.equal(allowed(repair, inputs), false);
  for (const change of [
    { runtime_only: false },
    { environment: "production" },
    { complete_comic_id: "other" },
    { diagnostic_task_id: "other" },
  ])
    assert.equal(
      allowed(historicalGuard, { ...repairInputs, ...change }),
      true,
    );
  assert.ok(
    source.indexOf("run: node scripts/repair-legacy-comic-images.mjs") <
      source.indexOf("run: node scripts/verify-complete-comic.mjs"),
  );
  for (const change of [
    { environment: "production" },
    { test_novels: true },
    { complete_comic_id: "other" },
    { diagnostic_task_id: "other" },
    { verify_only: true },
    { preflight_only: true },
    { queue_control: "pause" },
    { pause_queue_until_reset: true },
  ])
    assert.equal(allowed(guard, { ...inputs, ...change }), true);
  const santi = {
    ...inputs,
    complete_comic_id: "",
    diagnostic_task_id: "4a0378ca-c507-436c-81af-eb75edc2bcf9",
  };
  assert.equal(allowed(guard, santi), false);
  for (const block of owner) assert.equal(allowed(block, santi), false);
  assert.equal(
    allowed(
      blocks.find((block) => block.startsWith("name: Provision and deploy")),
      santi,
    ),
    true,
  );
  assert.equal(
    allowed(
      blocks.find((block) =>
        block.startsWith("name: Verify the complete existing comic"),
      ),
      santi,
    ),
    false,
  );
  for (const change of [
    { environment: "production" },
    { diagnostic_task_id: "other" },
    { complete_comic_id: "c_ee02de2c2194" },
    { repair_legacy_images: true },
    { test_novels: true },
    { verify_only: true },
    { preflight_only: true },
    { repair_cancelled_task: "{}" },
    { repair_native_task_id: "other" },
    { repair_storage_task_id: "other" },
    { pause_queue_until_reset: true },
    { queue_control: "resume" },
  ])
    assert.equal(allowed(guard, { ...santi, ...change }), true);
  const sourceGuard = blocks.find((block) =>
    block.startsWith("name: Restrict source credential initialization"),
  );
  const sourceInit = blocks.find((block) =>
    block.startsWith(
      "name: Initialize only missing existing staging source credentials",
    ),
  );
  const sourceInputs = {
    environment: "staging",
    verify_only: true,
    test_novels: true,
    initialize_novel_source: true,
  };
  assert.equal(allowed(sourceGuard, sourceInputs), false);
  assert.equal(allowed(sourceInit, sourceInputs), true);
  assert.equal(
    allowed(sourceInit, { ...sourceInputs, initialize_novel_source: false }),
    false,
  );
  for (const change of [
    { environment: "production" },
    { verify_only: false },
    { test_novels: false },
    { runtime_only: true },
    { preflight_only: true },
    { complete_comic_id: "other" },
    { repair_legacy_images: true },
    { repair_cancelled_task: "{}" },
    { repair_native_task_id: "other" },
    { repair_storage_task_id: "other" },
    { pause_queue_until_reset: true },
    { queue_control: "pause" },
  ])
    assert.equal(allowed(sourceGuard, { ...sourceInputs, ...change }), true);
});
test("overnight verification executes only on its single UTC date and remains scoped to the existing staging task", () => {
  const source = readFileSync(
    new URL(
      "../../.github/workflows/workers-comic-overnight.yml",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(source, /environment: staging/);
  assert.match(source, /cron: "3 0 15 10 \*"/);
  assert.match(source, /ref: a5716b413b23a5fb7c5ba90830f8f29d6764e020/);
  assert.match(source, /COMPLETE_COMIC_ID: c_ee02de2c2194/);
  assert.match(
    source,
    /DIAGNOSTIC_TASK_ID: c6fe02d4-c454-4111-b962-495120175313/,
  );
  assert.match(source, /timeout-minutes: 300/);
  assert.match(source, /validation_user_removed.*True/);
  assert.match(source, /original_user_progress_untouched.*True/);
  assert.match(source, /complete-comic-\*\.png/);
  assert.doesNotMatch(
    source,
    /run: .*live-comic\.mjs|test_novels|register-source/,
  );
  const root = mkdtempSync(join(tmpdir(), "mangadock-night-date-"));
  try {
    mkdirSync(join(root, "bin"));
    const date = join(root, "bin", "date");
    writeFileSync(date, '#!/bin/sh\nprintf "%s\\n" "$MANGADOCK_TEST_DATE"\n');
    chmodSync(date, 0o700);
    for (const [day, active] of [
      ["2026-10-14", false],
      ["2026-10-15", true],
      ["2027-10-15", false],
    ]) {
      const output = join(root, "out-" + day);
      const dateBlock = runBlocks(source).find((block) =>
        block.includes("date -u +%F"),
      );
      assert.ok(dateBlock, "The real one-day guard must be present");
      const result = spawnSync("bash", ["-c", dateBlock], {
        encoding: "utf8",
        env: {
          ...process.env,
          PATH: join(root, "bin") + ":" + process.env.PATH,
          GITHUB_OUTPUT: output,
          MANGADOCK_TEST_DATE: day,
        },
      });
      assert.equal(result.status, 0, result.stderr);
      assert.equal(readFileSync(output, "utf8"), `active=${active}\n`);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function runBlocks(source) {
  const lines = source.split("\n");
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {
    const match = /^(\s*)run:(?:\s(.*))?$/.exec(lines[i]);
    if (!match) continue;
    const indent = match[1].length;
    const rest = (match[2] || "").trim();
    if (rest === "|" || rest === ">" || rest === "|-" || rest === ">-") {
      const body = [];
      for (i += 1; i < lines.length; i++) {
        const line = lines[i];
        if (line.trim() === "") {
          body.push(line);
          continue;
        }
        if (/^\s*/.exec(line)[0].length <= indent) {
          i -= 1;
          break;
        }
        body.push(line);
      }
      blocks.push(body.join("\n"));
    } else blocks.push(rest);
  }
  return blocks;
}

test("workers-compute passes limit through the environment and checks it", () => {
  const source = readFileSync(workflow, "utf8");
  const runs = runBlocks(source);
  assert.ok(runs.length > 0);
  for (const block of runs)
    assert.equal(
      block.includes("${{ inputs."),
      false,
      "run block interpolates an input: " + block,
    );
  assert.match(source, /\[1-9\]\[0-9\]\?/);
  assert.match(source, /COMPUTE_LIMIT:\s*\$\{\{\s*inputs\.limit\s*\}\}/);
});

const automatic = readFileSync(
  new URL("../../.github/workflows/workers-compute-auto.yml", import.meta.url),
  "utf8",
);
const guard = runBlocks(automatic).find((block) =>
  block.includes("merge-base"),
);
function invoke(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", ...options });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}
test("automatic execution accepts main and only the exact approved staging commit", () => {
  const root = mkdtempSync(join(tmpdir(), "md-workflow-"));
  try {
    invoke("git", ["init", "-q", "-b", "main", root]);
    const git = (args) => invoke("git", args, { cwd: root });
    git(["config", "user.email", "local-test@example.invalid"]);
    git(["config", "user.name", "Local test"]);
    writeFileSync(join(root, "file"), "main");
    git(["add", "file"]);
    git(["commit", "-qm", "main"]);
    const main = git(["rev-parse", "HEAD"]);
    git(["remote", "add", "origin", root]);
    git(["switch", "-qc", "feature"]);
    writeFileSync(join(root, "file"), "reviewed");
    git(["commit", "-qam", "reviewed"]);
    const feature = git(["rev-parse", "HEAD"]);
    git(["switch", "-q", "main"]);
    const run = (ref, target, approved) =>
      spawnSync("bash", ["-euo", "pipefail", "-c", guard], {
        cwd: root,
        encoding: "utf8",
        env: {
          ...process.env,
          COMPUTE_REF: ref,
          DEPLOY_TARGET: target,
          APPROVED_COMPUTE_SHA: approved,
        },
      });
    assert.equal(run(main, "production", "").status, 0);
    assert.equal(run(feature, "staging", feature).status, 0);
    assert.notEqual(run(feature, "staging", "").status, 0);
    assert.notEqual(run(feature, "production", feature).status, 0);
    const sentinel = join(root, "injected");
    assert.notEqual(run(`$(touch ${sentinel})`, "staging", feature).status, 0);
    assert.equal(spawnSync("test", ["-e", sentinel]).status, 1);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
test("production timers skip credential and network access until enabled", () => {
  const block = runBlocks(automatic)[0],
    lines = block.split("\n");
  const start = lines.findIndex((line) => line.includes("python - <<'PY'"));
  const end = lines.findIndex((line, i) => i > start && line.trim() === "PY");
  const indent = /^\s*/.exec(lines[start + 1])[0].length;
  const python = lines
    .slice(start + 1, end)
    .map((line) => line.slice(indent))
    .join("\n");
  const root = mkdtempSync(join(tmpdir(), "md-probe-"));
  try {
    const out = join(root, "output");
    writeFileSync(out, "");
    const result = spawnSync("python3", ["-c", python], {
      encoding: "utf8",
      env: {
        PATH: process.env.PATH,
        DEPLOY_TARGET: "production",
        EVENT_NAME: "schedule",
        PRODUCTION_ENABLED: "false",
        REQUEST_TARGET: "staging",
        GITHUB_OUTPUT: out,
      },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(out, "utf8"), "ready=false\n");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
test("bootstrap rejects inaccessible or malformed wake configuration before database initialization", () => {
  for (const wake of ["", "short", "a".repeat(32) + "\n"]) {
    const result = spawnSync("node", ["scripts/bootstrap.mjs", "staging"], {
      cwd: new URL("../", import.meta.url),
      encoding: "utf8",
      env: {
        PATH: process.env.PATH,
        CLOUDFLARE_ACCOUNT_ID: "a".repeat(32),
        CLOUDFLARE_API_TOKEN: "synthetic",
        MANGADOCK_ADMIN_PASSWORD: "synthetic-password",
        DATABASE_PROVIDER: "turso",
        TURSO_DATABASE_URL: "libsql://fixture.turso.io",
        TURSO_AUTH_TOKEN: "synthetic",
        COMPUTE_WAKE_TOKEN: wake,
      },
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /COMPUTE_WAKE_TOKEN/);
    assert.doesNotMatch(result.stdout, /initialization|部署地址/);
  }
});

test("delivery control pauses both staging queues but resumes only the current Turso queue", () => {
  const source = readFileSync(
    new URL("../scripts/queue-delivery.py", import.meta.url),
    "utf8",
  );
  const probe = `import io,json,urllib.request
calls=[]
class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
def fake(request,timeout):
    if request.get_method() in ('PATCH','PUT'):
        calls.append({'method':request.get_method(),'url':request.full_url,'body':json.loads(request.data)})
        result={}
    elif '/schedules' in request.full_url:result={'schedules':[{'cron':'*/5 * * * *'}]}
    else:result=[{'queue_name':'mangadock-full-staging','queue_id':'old'},{'queue_name':'mangadock-turso-staging','queue_id':'new'},{'queue_name':'mangadock-turso-production','queue_id':'prod'}]
    return Response(json.dumps({'success':True,'result':result}).encode())
urllib.request.urlopen=fake
exec(${JSON.stringify(source)})
print(json.dumps(calls))`;
  for (const action of ["pause", "resume"]) {
    const result = spawnSync("python3", ["-c", probe], {
      encoding: "utf8",
      env: {
        ...process.env,
        QUEUE_CONTROL: action,
        DEPLOY_TARGET: "staging",
        CLOUDFLARE_ACCOUNT_ID: "fixture",
        CLOUDFLARE_API_TOKEN: "fixture",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    const calls = JSON.parse(result.stdout.trim().split("\n").at(-1));
    assert.deepEqual(
      calls
        .filter((call) => call.method === "PATCH")
        .map((call) => call.body.queue_name),
      action === "pause"
        ? ["mangadock-full-staging", "mangadock-turso-staging"]
        : ["mangadock-turso-staging"],
    );
    assert.ok(
      calls
        .filter((call) => call.method === "PATCH")
        .every(
          (call) => call.body.settings.delivery_paused === (action === "pause"),
        ),
    );
    const timers = calls.filter((call) => call.method === "PUT");
    assert.equal(timers.length, action === "pause" ? 1 : 0);
    if (timers.length) {
      assert.match(timers[0].url, /mangadock-full-staging\/schedules$/);
      assert.deepEqual(timers[0].body, []);
    }
  }
});
test("idle queue recovery runs only after reset and through the reviewed-ref guard", () => {
  const block = runBlocks(automatic)[0];
  const start = block.indexOf("python - <<'PY'");
  const lines = block.slice(start).split("\n");
  const end = lines.findIndex((line, i) => i > 0 && line.trim() === "PY");
  const indent = /^\s*/.exec(lines[1])[0].length;
  const python = lines
    .slice(1, end)
    .map((line) => line.slice(indent))
    .join("\n");
  const root = mkdtempSync(join(tmpdir(), "md-queue-resume-"));
  try {
    for (const due of [false, true]) {
      const output = join(root, "output");
      writeFileSync(output, "");
      const probe = `import io,json,time,urllib.request
calls=[]
class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
def fake(request,timeout):
    calls.append([request.get_method(),request.full_url])
    if 'workers/subdomain' in request.full_url:result={'result':{'subdomain':'fixture'}}
    elif '/queues' in request.full_url:result={'result':[{'queue_name':'mangadock-turso-staging','settings':{'delivery_paused':True}},{'queue_name':'mangadock-full-staging','settings':{'delivery_paused':True}}]}
    else:result={'data':{'pending':False,'ref':'a'*40,'queue_resume_after':time.time()+${due ? -60 : 60}}}
    return Response(json.dumps(result).encode())
urllib.request.urlopen=fake
exec(${JSON.stringify(python)})
print(json.dumps(calls))`;
      const result = spawnSync("python3", ["-c", probe], {
        encoding: "utf8",
        env: {
          ...process.env,
          DEPLOY_TARGET: "staging",
          EVENT_NAME: "schedule",
          REQUEST_TARGET: "staging",
          PRODUCTION_ENABLED: "false",
          CLOUDFLARE_ACCOUNT_ID: "fixture",
          CLOUDFLARE_API_TOKEN: "fixture",
          COMPUTE_WAKE_TOKEN: "fixture-wake".repeat(4),
          GITHUB_OUTPUT: output,
          GITHUB_ENV: join(root, "env"),
        },
      });
      assert.equal(result.status, 0, result.stderr);
      const calls = JSON.parse(result.stdout.trim().split("\n").at(-1));
      assert.ok(
        calls.every(([method]) => method === "GET"),
        "probe must not resume before ref verification",
      );
      assert.match(
        readFileSync(output, "utf8"),
        new RegExp(`resume_ready=${due}`),
      );
    }
    const step = automatic
      .split("name: Resume only the current queue")[1]
      .split("- uses:")[0];
    assert.match(step, /steps\.guard\.outcome == 'success'/);
    assert.match(step, /steps\.pending\.outputs\.resume_ready == 'true'/);
    assert.match(
      automatic,
      /steps\.guard\.outcome == 'success' && \(steps\.pending\.outputs\.ready == 'true' \|\| steps\.pending\.outputs\.resume_ready == 'true'\)/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("staging continuation runs only after real successful work on the public repository", () => {
  const block = automatic
    .split(/\n      - /)
    .find((value) => value.startsWith("name: Continue staging once"));
  const condition = block.match(/\n        if: (.*)/)[1];
  const evaluate = (changes = {}) => {
    const matrix = { target: "staging", ...changes.matrix };
    const steps = {
      pending: { outputs: { ready: "true" } },
      guard: { outcome: "success" },
      compute: { outputs: { continue_work: "true" } },
      ...changes.steps,
    };
    const github = {
      repository: "dddinmx/MangaDock",
      event: { repository: { visibility: "public" } },
      ...changes.github,
    };
    return Function(
      "matrix",
      "steps",
      "github",
      "success",
      `return (${condition})`,
    )(matrix, steps, github, () => changes.success !== false);
  };
  assert.equal(evaluate(), true);
  assert.equal(evaluate({ matrix: { target: "production" } }), false);
  assert.equal(
    evaluate({ steps: { pending: { outputs: { ready: "false" } } } }),
    false,
  );
  assert.equal(evaluate({ steps: { guard: { outcome: "failure" } } }), false);
  assert.equal(
    evaluate({ steps: { compute: { outputs: { continue_work: "false" } } } }),
    false,
  );
  assert.equal(
    evaluate({ github: { event: { repository: { visibility: "private" } } } }),
    false,
  );
  assert.equal(evaluate({ github: { repository: "someone/fork" } }), false);
  assert.equal(evaluate({ success: false }), false);
  assert.match(automatic, /timeout-minutes: 12/);
  assert.match(automatic, /runs-on: ubuntu-latest/);
  assert.match(automatic, /actions: write/);
  assert.match(automatic, /continue_at=.*int\(time\.time\(\)\)\+5/);
  assert.ok(
    automatic.indexOf("Release a failed launcher hold") <
      automatic.indexOf("Continue staging once"),
  );
});

test("real continuation code waits within bounds, dispatches staging exactly once, and rejects private repositories or ambiguous retries", () => {
  const block = runBlocks(automatic).find((value) =>
    value.includes("def continue_staging"),
  );
  const lines = block.trimEnd().split("\n").slice(1, -1);
  const indent = lines[0].match(/^\s*/)[0].length;
  const python = lines.map((line) => line.slice(indent)).join("\n");
  const harness = `
import io,json,os,urllib.error
namespace={}
source=os.environ['CONTINUATION_SOURCE'].rsplit('\\ncontinue_staging(',1)[0]
exec(compile(source,'<real-continuation>','exec'),namespace)
run=namespace['continue_staging']
class Response(io.BytesIO):
 def __init__(self,data,status=200):super().__init__(json.dumps(data).encode());self.status=status
base={'CONTINUATION_REPOSITORY':'dddinmx/MangaDock','CONTINUATION_TARGET':'staging','CONTINUATION_AT':'1005','CONTINUATION_TOKEN':'synthetic-only'}
for mode in ['success','expired_delay','private','became_private','missing_visibility','wrong_repository','production','too_long','dispatch_failure','dispatch_rejected']:
 env=dict(base);calls=[];sleeps=[];gets=[]
 if mode=='expired_delay':env['CONTINUATION_AT']='900'
 if mode=='wrong_repository':env['CONTINUATION_REPOSITORY']='someone/fork'
 if mode=='production':env['CONTINUATION_TARGET']='production'
 if mode=='too_long':env['CONTINUATION_AT']='1006'
 def request(req,timeout):
  assert timeout==20
  assert req.full_url.startswith('https://api.github.com/repos/dddinmx/MangaDock')
  if req.get_method()=='GET':
   gets.append(req.full_url)
   if mode=='missing_visibility':return Response({'private':False})
   private=mode=='private' or mode=='became_private' and len(gets)==2
   return Response({'private':private,'visibility':'private' if private else 'public'})
  calls.append(req)
  assert req.full_url=='https://api.github.com/repos/dddinmx/MangaDock/actions/workflows/workers-compute-auto.yml/dispatches'
  assert json.loads(req.data)=={'ref':'main','inputs':{'environment':'staging'}}
  if mode=='dispatch_failure':raise urllib.error.URLError('synthetic failure')
  return Response({},202 if mode=='dispatch_rejected' else 204)
 error=None
 try:run(env,request,lambda:1000,sleeps.append)
 except (ValueError,RuntimeError,urllib.error.URLError) as exc:error=exc
 if mode in ['success','expired_delay']:
  assert error is None,(mode,error)
  assert len(calls)==1 and len(gets)==2 and sleeps==[5 if mode=='success' else 0],mode
 elif mode in ['dispatch_failure','dispatch_rejected']:
  assert error is not None and len(calls)==1 and sleeps==[5],mode
 else:
  assert error is not None and calls==[],mode
  assert sleeps==([5] if mode=='became_private' else []),mode
print('10 bounded continuation scenarios passed; no real sleep or network')
`;
  const result = spawnSync("python3", ["-c", harness], {
    encoding: "utf8",
    env: { ...process.env, CONTINUATION_SOURCE: python },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /10 bounded continuation scenarios passed/);
});
