import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
