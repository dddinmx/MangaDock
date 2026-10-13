import { readFile, writeFile, mkdir, cp, rm, readdir } from "node:fs/promises";
import nunjucks from "nunjucks";
import { build } from "esbuild";
import { install } from "./jinja-compat.mjs";
const root = new URL("../../", import.meta.url),
  out = new URL("../public/", import.meta.url);
await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
// Whitelist bundled UI assets: never publish library files, secrets, covers or extension APKs.
for (const directory of ["css", "fonts", "login-covers"])
  await cp(
    new URL(`static/${directory}`, root),
    new URL(`static/${directory}`, out),
    { recursive: true },
  );
await mkdir(new URL("static/js/vendor/", out), { recursive: true });
for (const file of [
  "logo-192.png",
  "favicon.png",
  "apple-touch-icon.png",
  "default-comic-cover.jpg",
  "pwa-icon-192.png",
  "pwa-icon-512.png",
])
  await cp(new URL(`static/${file}`, root), new URL(`static/${file}`, out));
for (const file of (await readdir(new URL("static/js/", root)))
  .filter((f) => f.endsWith(".js"))
  .concat([
    "vendor/jszip.min.js",
    "vendor/pdf.min.js",
    "vendor/pdf.worker.min.js",
  ]))
  await cp(
    new URL(`static/js/${file}`, root),
    new URL(`static/js/${file}`, out),
  );
const engine = new nunjucks.Environment(null, { autoescape: true });
install(nunjucks, engine);
engine.addFilter(
  "tojson",
  (value) =>
    new nunjucks.runtime.SafeString(
      JSON.stringify(value).replaceAll("<", "\\u003c"),
    ),
);
const context = {
  asset_url: (path) => `/static/${path}`,
  csrf_token: () => "",
  get_flashed_messages: () => [],
  request: { args: { get: () => "" } },
  url_for: (name) =>
    name === "login"
      ? "/login"
      : name === "save_progress"
        ? "/api/progress"
        : "/",
  login_wall: (await readdir(new URL("static/login-covers/", root)))
    .filter((f) => f.endsWith(".jpg"))
    .slice(0, 48)
    .map((f) => ({ url: `/static/login-covers/${f}` })),
  novel: { title: "正在加载…", novel_id: "" },
  chapter: { title: "正在加载…", index: 0, total_chapters: 1, paragraphs: [] },
  saved_page: 0,
  server_progress_ms: null,
  reader_user_id: "",
  progress_key: "",
  chapter_api_base_url: "",
  reader_base_url: "",
};
let login = await readFile(new URL("templates/login.html", root), "utf8");
login = login
  .replace(/{% with messages = get_flashed_messages\(\) %}/g, "")
  .replace(/{% endwith %}/g, "");
login = login.replace(
  "<script src=\"{{ asset_url('js/api_v1.js') }}\"></script>",
  '<script src="/login.js"></script>',
);
await writeFile(
  new URL("login.html", out),
  engine.renderString(login, context),
);
let reader = await readFile(
  new URL("templates/novel_reader.html", root),
  "utf8",
);
const start = reader.indexOf("    <script>\n        const csrfToken"),
  end = reader.indexOf("    </script>", start);
let runtime = reader.slice(start + "    <script>".length, end);
const constants = {
  csrfToken: "MD.csrf",
  progressKey: "MD.id",
  initialChapter: "MD.chapter",
  totalChapters: "MD.count",
  serverSavedPage: "MD.progress?.page || 0",
  serverProgressMs: "MD.progress?.client_ms || 0",
  readerUserId: "MD.user",
  chapterApiBaseUrl: '""',
  readerBaseUrl: "`/reader?book=${MD.id}&`",
  novelTitle: "MD.title",
};
for (const [name, value] of Object.entries(constants))
  runtime = runtime.replace(
    new RegExp(`const ${name} = .*?;`),
    `const ${name} = ${value};`,
  );
runtime = runtime
  .replace("method: 'POST', keepalive", "method: 'PUT', keepalive")
  .replace(
    "JSON.stringify(payload)",
    "JSON.stringify({chapter:payload.chapter,page:payload.page,scroll:0,client_ms:payload.client_progress_ms})",
  );
runtime = runtime.replace(
  "{{ url_for('save_progress')|tojson }}",
  "`/api/books/${MD.id}/progress`",
);
runtime = runtime.replace(
  "}).catch(() => null);",
  "}).then(response => { MD.saveStatus(response.ok); return response; }).catch(() => MD.saveStatus(false));",
);
runtime = runtime.replace(
  /fetch\(chapterApiUrl\(index\), \{headers: \{'Accept': 'application\/json'\}\}\)\s*\.then\(\(response\) => \{[\s\S]*?return response.json\(\);\s*\}\)/,
  "MD.getChapter(index)",
);
runtime = runtime
  .replace("addEventListener('load', () => {", "queueMicrotask(() => {")
  .replace(
    "addEventListener('load', startReadingTimer);",
    "startReadingTimer();",
  );
runtime = runtime.replace(
  "function readerUrl(index) { return `${readerBaseUrl}?start_chapter=${index}`; }",
  "function readerUrl(index) { return `/reader?book=${MD.id}&start_chapter=${index}`; }",
);
runtime = engine.renderString(runtime, context);
const initialRender =
  "flow.replaceChildren(createChapterFragment(initialChapter)); updateChapterUi();";
await writeFile(
  new URL("reader-runtime.js", out),
  `window.startReader = function(MD) {\n${runtime}\n${initialRender}\n};\n`,
);
reader =
  reader.slice(0, start) +
  '<script src="/static/js/vendor/jszip.min.js"></script><script src="/reader-runtime.js"></script><script type="module" src="/reader.js"></script>' +
  reader.slice(end + "    </script>".length);
await writeFile(
  new URL("reader.html", out),
  engine.renderString(reader, context),
);
for (const file of await readdir(new URL("../web/", import.meta.url)))
  await cp(new URL(`../web/${file}`, import.meta.url), new URL(file, out));

// Precompile every unchanged original template; render these in a separate SQLite Durable Object.
const templates = [];
for (const name of await readdir(new URL("templates/", root))) {
  if (!name.endsWith(".html")) continue;
  let source = await readFile(new URL(`templates/${name}`, root), "utf8");
  source = source.replace(
    /([A-Za-z_]\w*(?:\.\w+)*)\[:(\d+)\]/g,
    "$1|sliceitems(0,$2)",
  );
  source = source
    .replace(/{% with (.*?) %}/g, "{% set $1 %}")
    .replace(/{% endwith %}/g, "");
  templates.push(
    nunjucks.precompileString(source, {
      name,
      env: engine,
      wrapper: (entries) =>
        entries
          .map(
            (entry) =>
              JSON.stringify(entry.name) +
              ": (function(){" +
              entry.template +
              "})()",
          )
          .join(","),
    }),
  );
}
await mkdir(new URL("../src/generated/", import.meta.url), { recursive: true });
const templateRuntime = await build({
  entryPoints: [
    new URL(
      "../node_modules/nunjucks/browser/nunjucks-slim.js",
      import.meta.url,
    ).pathname,
  ],
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
});
await writeFile(
  new URL("../src/generated/runtime.ts", import.meta.url),
  "// @ts-nocheck\n" + templateRuntime.outputFiles[0].text,
);
await writeFile(
  new URL("../src/generated/templates.ts", import.meta.url),
  "// @ts-nocheck\n// Generated from original templates. Do not hand-edit.\nexport default {\n" +
    templates.join(",\n") +
    "\n};\n",
);
await cp(
  new URL("static/manifest.json", root),
  new URL("static/manifest.json", out),
);
await cp(new URL("static/sw.js", root), new URL("static/sw.js", out));
await writeFile(
  new URL("login-wall.json", out),
  JSON.stringify(context.login_wall),
);
await mkdir(new URL("static/cover/", out), { recursive: true });
await cp(
  new URL("static/cover/cover.png", root),
  new URL("static/cover/cover.png", out),
);
console.log(
  `Built all ${templates.length} original templates and whitelisted assets.`,
);
