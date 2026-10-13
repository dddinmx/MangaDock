import { describe, expect, it } from "vitest";
import { HttpError } from "../src/index";
import {
  COVER_BYTE_LIMIT,
  coverContentLength,
  coverContentType,
  fanqieBookId,
  limitedCover,
} from "../src/media";
import {
  contentTypeForName,
  remoteProxyHeaders,
} from "../src/remote-library";
import {
  IMPORT_OBJECT_PREFIX,
  LIBRARY_R2_PREFIX,
  importObjectKey,
  libraryObjectKey,
  r2Prefix,
  scanPrefix,
} from "../src/scans";

function statusOf(run: () => unknown) {
  try {
    run();
  } catch (error) {
    expect(error).toBeInstanceOf(HttpError);
    return (error as HttpError).status;
  }
  throw new Error("expected HttpError");
}

describe("remote file content type", () => {
  it("derives an allowlisted type and falls back to octet-stream", () => {
    expect(contentTypeForName("page.jpg")).toBe("image/jpeg");
    expect(contentTypeForName("page.JPEG")).toBe("image/jpeg");
    expect(contentTypeForName("a/b.PNG")).toBe("image/png");
    expect(contentTypeForName("anim.gif")).toBe("image/gif");
    expect(contentTypeForName("still.webp")).toBe("image/webp");
    expect(contentTypeForName("modern.avif")).toBe("image/avif");
    expect(contentTypeForName("scan.bmp")).toBe("image/bmp");
    expect(contentTypeForName("book.epub")).toBe("application/epub+zip");
    expect(contentTypeForName("book.PDF")).toBe("application/pdf");
    expect(contentTypeForName("pack.zip")).toBe("application/zip");
    expect(contentTypeForName("https://dav.example/books/book.cbz?token=1")).toBe(
      "application/vnd.comicbook+zip",
    );
    expect(contentTypeForName("notes.TXT")).toBe("text/plain");
    expect(contentTypeForName("file.jpg.exe")).toBe("application/octet-stream");
    expect(contentTypeForName("no-extension")).toBe("application/octet-stream");
    expect(contentTypeForName("page.html")).toBe("application/octet-stream");
  });

  it("does not forward the upstream content type", () => {
    const headers = remoteProxyHeaders(
      new Headers({
        "Content-Type": "text/html; charset=utf-8",
        "Set-Cookie": "session=secret",
        "Content-Disposition": "inline; filename=evil.html",
        ETag: '"abc"',
      }),
      "https://dav.example/books/book.cbz",
    );
    expect(headers.get("Content-Type")).toBe("application/vnd.comicbook+zip");
    expect(headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(headers.get("Content-Security-Policy")).toBe(
      "default-src 'none'; sandbox",
    );
    expect(headers.get("Content-Disposition")).toBe(
      'attachment; filename="book.cbz"',
    );
    expect(headers.get("Set-Cookie")).toBeNull();
    expect(headers.get("ETag")).toBe('"abc"');
  });

  it("attaches non-images and leaves images inline", () => {
    for (const name of ["novel.epub", "chapter.pdf", "pack.zip", "notes.txt", "raw.bin"]) {
      const headers = remoteProxyHeaders(
        new Headers({ "Content-Type": "text/html" }),
        name,
      );
      expect(headers.get("Content-Disposition")).toMatch(/^attachment; filename="/);
      expect(headers.get("Content-Type")).not.toBe("text/html");
    }
    const image = remoteProxyHeaders(
      new Headers({
        "Content-Type": "text/html",
        "Content-Disposition": "attachment; filename=forced.bin",
      }),
      "cover.png",
    );
    expect(image.get("Content-Type")).toBe("image/png");
    expect(image.get("Content-Disposition")).toBeNull();
    expect(image.get("X-Content-Type-Options")).toBe("nosniff");
  });
});

describe("library object keys", () => {
  it("keeps a scan inside an r2://library/ directory prefix", () => {
    expect(r2Prefix("r2://library/scan-fixture/")).toBe("scan-fixture/");
    expect(r2Prefix("r2://library/a/b")).toBe("a/b/");
    expect(scanPrefix(r2Prefix("r2://library/scan-fixture/"))).toBe(
      "scan-fixture/",
    );
    expect(LIBRARY_R2_PREFIX + "scan-fixture/a.cbz").toMatch(
      /^r2:\/\/library\//,
    );
    for (const path of [
      "r2://library/",
      "r2://library",
      "r2://other/secret/",
      "https://evil.example/secret/",
      "r2://library/foo/../bar/",
      "r2://library/foo\\bar/",
      "r2://library/%2e%2e/secret/",
      "r2://library/%252e%252e/secret/",
      "r2://library/%5Csecret/",
    ])
      expect(statusOf(() => r2Prefix(path)), path).toBe(400);
    expect(statusOf(() => scanPrefix("../"))).toBe(400);
    expect(statusOf(() => scanPrefix("foo\\bar/"))).toBe(400);
    expect(statusOf(() => scanPrefix("scan-fixture"))).toBe(400);
    expect(statusOf(() => scanPrefix(""))).toBe(400);
  });

  it("requires object keys to stay under the prefix without .. or backslash", () => {
    expect(libraryObjectKey("scan-fixture/第一话.cbz", "scan-fixture/")).toBe(
      "scan-fixture/第一话.cbz",
    );
    expect(libraryObjectKey("r2://library/imports/abc.cbz")).toBe(
      "imports/abc.cbz",
    );
    expect(
      LIBRARY_R2_PREFIX + libraryObjectKey("imports/abc.cbz"),
    ).toBe("r2://library/imports/abc.cbz");
    expect(importObjectKey("imports/abc.cbz")).toBe("imports/abc.cbz");
    expect(importObjectKey("missing")).toBe("missing");
    for (const key of [
      "../compute/secret",
      "foo/../../compute/secret",
      "foo\\bar",
      "r2://evil/secret",
      "/etc/passwd",
      "scan-fixture/../compute/a.cbz",
      "imports/%2e%2e/compute/a",
      "compute/job/token/original",
      IMPORT_OBJECT_PREFIX + "../secret",
    ])
      expect(statusOf(() => libraryObjectKey(key, "scan-fixture/")), key).toBe(
        400,
      );
    expect(
      statusOf(() => libraryObjectKey("other/a.cbz", "scan-fixture/")),
    ).toBe(400);
    expect(statusOf(() => importObjectKey("compute/job/token/original"))).toBe(
      400,
    );
    expect(statusOf(() => importObjectKey("imports/../compute/a"))).toBe(400);
    expect(statusOf(() => importObjectKey("foo\\bar"))).toBe(400);
  });
});

describe("fanqie cover proxy", () => {
  it("accepts only the numeric book id", () => {
    expect(fanqieBookId("12345678")).toBe("12345678");
    expect(fanqieBookId("1".repeat(24))).toBe("1".repeat(24));
    for (const id of [
      "1234567",
      "1".repeat(25),
      "12345678a",
      "12345678/cover",
      "../12345678",
      " 12345678",
      "12345678\n",
      "",
      null,
      undefined,
    ])
      expect(statusOf(() => fanqieBookId(id)), String(id)).toBe(400);
  });

  it("requires an image content type and a 5 MB ceiling", () => {
    expect(coverContentType("image/jpeg; charset=binary")).toBe("image/jpeg");
    expect(coverContentLength(null)).toBeNull();
    expect(coverContentLength(String(COVER_BYTE_LIMIT))).toBe(COVER_BYTE_LIMIT);
    expect(statusOf(() => coverContentType("text/html"))).toBe(502);
    expect(statusOf(() => coverContentType("image/svg+xml"))).toBe(502);
    expect(statusOf(() => coverContentType(null))).toBe(502);
    expect(statusOf(() => coverContentLength(String(COVER_BYTE_LIMIT + 1)))).toBe(
      413,
    );
    expect(statusOf(() => coverContentLength("nope"))).toBe(413);
  });

  it("caps the cover stream and does not relabel a non-image", async () => {
    const png = new Uint8Array([137, 80, 78, 71, 1, 2, 3, 4]);
    const ok = await limitedCover(
      new Response(png, { headers: { "Content-Type": "image/png" } }),
    );
    expect(ok.headers.get("Content-Type")).toBe("image/png");
    expect(ok.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(new Uint8Array(await ok.arrayBuffer())).toEqual(png);
    await expect(
      limitedCover(
        new Response("<html></html>", {
          headers: { "Content-Type": "text/html" },
        }),
      ),
    ).rejects.toMatchObject({ status: 502 });
    const oversized = new Uint8Array(COVER_BYTE_LIMIT + 1);
    await expect(
      limitedCover(
        new Response(oversized, { headers: { "Content-Type": "image/jpeg" } }),
      ),
    ).rejects.toMatchObject({ status: 413 });
  });
});
