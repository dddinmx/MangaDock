import { describe, expect, it } from "vitest";
import { readBoundedText, webdavContainedHref } from "../src/integrations";

const root = "https://dav.example/lib/";

describe("webdavContainedHref", () => {
  it("rejects dot-dot segments, including encoded ones", () => {
    expect(webdavContainedHref(root, "https://dav.example/lib/../etc")).toBeNull();
    expect(webdavContainedHref(root, "../etc")).toBeNull();
    expect(webdavContainedHref(root, root + "%2e%2e/etc")).toBeNull();
    expect(webdavContainedHref(root, root + "nested/%2E%2E/ok.cbz")).toBeNull();
    expect(webdavContainedHref(root, root + ".%2e/etc")).toBeNull();
  });

  it("rejects %2F used to smuggle a parent segment", () => {
    expect(
      webdavContainedHref(root, root + "foo%2F..%2F..%2Fetc/passwd"),
    ).toBeNull();
    expect(webdavContainedHref(root, root + "%2f..%2fetc")).toBeNull();
  });

  it("rejects a sibling prefix such as /lib2", () => {
    expect(
      webdavContainedHref(root, "https://dav.example/lib2/chap.cbz"),
    ).toBeNull();
    expect(
      webdavContainedHref(
        "https://dav.example/lib",
        "https://dav.example/lib2/chap.cbz",
      ),
    ).toBeNull();
    expect(
      webdavContainedHref(
        "https://dav.example/lib",
        "https://dav.example/library/chap.cbz",
      ),
    ).toBeNull();
  });

  it("accepts a nested file and normalizes dot and duplicate slashes", () => {
    expect(webdavContainedHref(root, root + "series/01.cbz")).toBe(
      "https://dav.example/lib/series/01.cbz",
    );
    expect(webdavContainedHref(root, root + "./series//02.cbz")).toBe(
      "https://dav.example/lib/series/02.cbz",
    );
    expect(webdavContainedHref(root, "01.cbz", root + "series/")).toBe(
      "https://dav.example/lib/series/01.cbz",
    );
    expect(
      webdavContainedHref(
        "https://dav.example/lib",
        "https://dav.example/lib/nested/a.cbz",
      ),
    ).toBe("https://dav.example/lib/nested/a.cbz");
  });

  it("rejects backslash, NUL, and malformed escapes", () => {
    expect(webdavContainedHref(root, root + "foo\\..\\etc")).toBeNull();
    expect(webdavContainedHref(root, root + "foo%5C..%5Cetc")).toBeNull();
    expect(webdavContainedHref(root, root + "a%00.cbz")).toBeNull();
    expect(webdavContainedHref(root, root + "%")).toBeNull();
    expect(webdavContainedHref(root, root + "%ZZ")).toBeNull();
    expect(
      webdavContainedHref(root, "https://evil.example/lib/series/01.cbz"),
    ).toBeNull();
  });
});

describe("readBoundedText", () => {
  it("reads a body up to the limit and rejects anything larger", async () => {
    await expect(readBoundedText(new Response("abcde"), 5)).resolves.toBe(
      "abcde",
    );
    await expect(readBoundedText(new Response("abcdef"), 5)).rejects.toThrow(
      /过大/,
    );
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array(8));
        controller.close();
      },
    });
    await expect(
      readBoundedText(
        new Response(stream, { headers: { "Content-Length": "4" } }),
        5,
      ),
    ).rejects.toThrow(/过大/);
  });
});
