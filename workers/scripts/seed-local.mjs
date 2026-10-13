import { mkdir, writeFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { pbkdf2Sync, createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
const require = createRequire(import.meta.url),
  JSZip = require("../../static/js/vendor/jszip.min.js");
export async function seed({
  persist = ".wrangler/state/e2e",
  password = "local-e2e-password-2026",
  fixture = true,
} = {}) {
  if (persist === ".wrangler/state/e2e")
    await rm(persist, { recursive: true, force: true });
  await mkdir(".wrangler/e2e", { recursive: true });
  const args = ["--local", "--persist-to", persist];
  const cli = (argv) => {
    const result = spawnSync("npx", ["wrangler", ...argv], {
      stdio: "inherit",
      env: { ...process.env, WRANGLER_SEND_METRICS: "false" },
    });
    if (result.status !== 0) throw new Error("本地初始化失败");
  };
  cli(["d1", "migrations", "apply", "mangadock-local", ...args]);
  const encoded = `pbkdf2:sha256:600000$local-test-salt$${pbkdf2Sync(password, "local-test-salt", 600000, 32, "sha256").toString("hex")}`;
  let sql = `INSERT INTO users(id,username,password_hash,role) VALUES ('admin','admin','${encoded}','admin') ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash; DELETE FROM sessions; DELETE FROM login_attempts;`;
  if (fixture) {
    const zip = new JSZip();
    zip.file("mimetype", "application/epub+zip");
    zip.file(
      "META-INF/container.xml",
      '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
    );
    zip.file(
      "OEBPS/book.opf",
      '<package xmlns="http://www.idpf.org/2007/opf"><metadata><title>本地测试小说</title></metadata><manifest><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="b" href="two.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
    );
    zip.file(
      "OEBPS/nav.xhtml",
      '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="one.xhtml">第一章</a><a href="two.xhtml">第二章</a></nav></body></html>',
    );
    for (const [name, title] of [
      ["one", "第一章"],
      ["two", "第二章"],
    ])
      zip.file(
        `OEBPS/${name}.xhtml`,
        `<html xmlns="http://www.w3.org/1999/xhtml"><head><title>${title}</title></head><body><h1>${title}</h1><script>window.EPUB_INJECTED=true</script>${Array.from({ length: 50 }, (_, i) => `<p>${i + 1}。故事在云端继续。这是一段用于验证翻页和阅读进度的测试内容。</p>`).join("")}</body></html>`,
      );
    const bytes = await zip.generateAsync({ type: "nodebuffer" }),
      digest = createHash("sha256").update(bytes).digest("hex"),
      key = `novels/123456789/${digest}.epub`;
    await writeFile(".wrangler/e2e/fixture.epub", bytes);
    cli([
      "r2",
      "object",
      "put",
      `mangadock-local/${key}`,
      "--file",
      ".wrangler/e2e/fixture.epub",
      ...args,
    ]);
    sql += ` INSERT OR REPLACE INTO books (id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('123456789','123456789','本地测试小说','测试作者','${key}',${bytes.length},'${digest}',${Math.floor(Date.now() / 1000)}); DELETE FROM progress; DELETE FROM jobs;`;
  }
  sql += ` UPDATE books SET chapter_count=2 WHERE id='123456789'; DELETE FROM comic_progress; DELETE FROM comic_jobs; DELETE FROM chapters; DELETE FROM comics; INSERT INTO comics(id,name,provider,source_url,description,cover_key,created_at,updated_at) VALUES ('c_test','本地测试漫画','baozimh_org','https://baozimh.org/manga/test','这是用于原版界面和漫画阅读验证的作品简介。','fixture/cover',1700000000,1700000000); INSERT INTO chapters(id,comic_id,ordinal,title,source_id,manifest_key,page_count,state) VALUES ('ch_test','c_test',0,'第一话','test-source','fixture/manifest',2,'ready');`;
  const manifest = JSON.stringify([
    { key: "fixture/page0", width: 447, height: 626 },
    { key: "fixture/page1", width: 447, height: 626 },
  ]);
  await writeFile(".wrangler/e2e/pages.json", manifest);
  cli([
    "r2",
    "object",
    "put",
    "mangadock-local/fixture/manifest",
    "--file",
    ".wrangler/e2e/pages.json",
    ...args,
  ]);
  for (const key of ["fixture/page0", "fixture/page1", "fixture/cover"])
    cli([
      "r2",
      "object",
      "put",
      "mangadock-local/" + key,
      "--file",
      "../static/default-comic-cover.jpg",
      "--content-type",
      "image/jpeg",
      ...args,
    ]);
  await writeFile(".wrangler/e2e/seed.sql", sql);
  cli([
    "d1",
    "execute",
    "mangadock-local",
    "--file",
    ".wrangler/e2e/seed.sql",
    ...args,
  ]);
}
if (process.argv[1] === new URL(import.meta.url).pathname)
  await seed({
    persist: process.env.LOCAL_PERSIST || ".wrangler/state/default",
    fixture: process.env.NO_FIXTURE !== "1",
  });
