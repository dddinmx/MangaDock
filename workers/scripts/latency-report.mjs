import { readFile, writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
let cookie = "",
  csrf = "";
const samples = [];
async function request(path, options = {}) {
  const started = performance.now();
  const response = await fetch(url + path, {
    ...options,
    headers: {
      Origin: url,
      Cookie: cookie,
      "X-CSRFToken": csrf,
      "Content-Type": "application/json",
    },
    signal: AbortSignal.timeout(30000),
  });
  const text = await response.text();
  samples.push({
    path,
    status: response.status,
    wall_ms: Math.round(performance.now() - started),
    colo: response.headers.get("cf-ray")?.split("-").at(-1),
  });
  if (!response.ok)
    throw new Error(
      "Latency check failed: " + path + " HTTP " + response.status,
    );
  return { response, text };
}
try {
  const login = await request("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: "admin",
      password: process.env.MANGADOCK_ADMIN_PASSWORD,
    }),
  });
  cookie = login.response.headers.get("set-cookie")?.split(";")[0];
  csrf = JSON.parse(login.text).data.csrf_token;
  let books = [];
  for (let i = 0; i < 5; i++) {
    await request("/");
    await request("/api/comics");
    books = JSON.parse((await request("/api/books")).text).data;
  }
  if (books.length) {
    const book = books[0];
    for (let i = 0; i < 3; i++) {
      const current =
        JSON.parse((await request(`/api/books/${book.id}/progress`)).text)
          .data || {};
      await request(`/api/books/${book.id}/progress`, {
        method: "PUT",
        body: JSON.stringify({
          chapter: current.chapter || 0,
          page: current.page || 0,
          scroll: current.scroll || 0,
          client_ms: Date.now(),
        }),
      });
    }
  }
  const summary = Object.fromEntries(
    [...new Set(samples.map((item) => item.path))].map((path) => {
      const times = samples
        .filter((item) => item.path === path)
        .map((item) => item.wall_ms)
        .sort((a, b) => a - b);
      return [
        path,
        {
          count: times.length,
          min_ms: times[0],
          median_ms: times[Math.floor(times.length / 2)],
          max_ms: times.at(-1),
        },
      ];
    }),
  );
  const result = {
    url,
    database_provider: "turso",
    samples,
    summary,
    progress_tested: books.length > 0,
    warning:
      "GitHub runner elapsed time including network and response body; not Workers CPU, not a D1 comparison or a China-client benchmark.",
  };
  await writeFile("latency-results.json", JSON.stringify(result, null, 2));
  console.log(
    JSON.stringify(
      { summary, progress_tested: result.progress_tested },
      null,
      2,
    ),
  );
} catch (error) {
  console.error("::error::" + error.message);
  process.exitCode = 1;
}
