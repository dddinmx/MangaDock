import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const workflow = new URL(
  "../../.github/workflows/workers-compute.yml",
  import.meta.url,
);

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
    if request.get_method()=='PATCH':
        calls.append(json.loads(request.data))
        result={}
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
      calls.map((call) => call.queue_name),
      action === "pause"
        ? ["mangadock-full-staging", "mangadock-turso-staging"]
        : ["mangadock-turso-staging"],
    );
    assert.ok(
      calls.every(
        (call) => call.settings.delivery_paused === (action === "pause"),
      ),
    );
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
