import { fetchMock } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { providerHosts } from "../src/providers";
import { allowedRemoteUrl, remoteUrl, safeFetch } from "../src/security";

const baozi = ["baozimh.org", "6wm.top", "apikk.top"];

describe("provider remote URL allowlist", () => {
  beforeEach(() => {
    fetchMock.activate();
    fetchMock.disableNetConnect();
  });
  afterEach(() => {
    fetchMock.assertNoPendingInterceptors();
    fetchMock.deactivate();
  });

  it("rejects IP literals, including decimal, octal, hex, and IPv6", () => {
    for (const url of [
      "https://8.8.8.8/lookup",
      "https://1.2.3.4/",
      "https://2130706433/",
      "https://0x7f000001/",
      "https://0177.0.0.1/",
      "https://127.1/",
      "https://0x7f.0.0.1/",
      "https://[::1]/",
      "https://[2001:db8::1]/",
      "https://[::ffff:7f00:1]/",
    ])
      expect(() => remoteUrl(url), url).toThrow(/远端地址/);
  });

  it("rejects private and non-global ranges", () => {
    for (const url of [
      "https://10.1.2.3/",
      "https://127.0.0.1/",
      "https://0.0.0.0/",
      "https://172.16.5.4/",
      "https://172.31.255.1/",
      "https://192.168.1.1/",
      "https://169.254.169.254/",
      "https://100.64.0.1/",
      "https://0xa000001/",
      "https://3232235777/",
      "https://012.0.0.1/",
      "https://[::1]/",
      "https://[fc00::1]/",
      "https://[fd00::1]/",
      "https://[fe80::1]/",
      "https://[::ffff:a00:1]/",
    ])
      expect(() => remoteUrl(url), url).toThrow(/远端地址/);
  });

  it("rejects localhost. and other non-public names", () => {
    for (const url of [
      "https://localhost/",
      "https://localhost./",
      "https://foo.localhost/",
      "https://foo.localhost./",
      "https://printer.local/",
      "https://db.internal/",
      "https://baozimh.org./manga/test",
      "https://127.0.0.1.nip.io/",
      "https://10.0.0.1.sslip.io/secret",
      "https://customer.app.localtest.me/",
      "https://8.8.8.8.xip.io/",
      "https://user:pass@baozimh.org/manga/test",
      "http://baozimh.org/manga/test",
      "https://baozimh.org:8443/manga/test",
    ])
      expect(() => remoteUrl(url), url).toThrow(/远端地址/);
    expect(remoteUrl("https://baozimh.org:443/manga/test").hostname).toBe(
      "baozimh.org",
    );
  });

  it("rejects an off-allowlist host", async () => {
    await expect(
      safeFetch("https://evil.example/secret", {}, baozi),
    ).rejects.toThrow(/允许域名/);
    expect(() =>
      allowedRemoteUrl("https://us.hamreus.com/a.jpg", providerHosts("baozimh_org")),
    ).toThrow(/允许域名/);
    expect(() =>
      allowedRemoteUrl(
        "https://image.kalaimg.top/play/min/ab",
        providerHosts("hipmh"),
      ),
    ).toThrow(/允许域名/);
  });

  it("blocks a redirect to a disallowed host", async () => {
    fetchMock
      .get("https://baozimh.org")
      .intercept({ path: "/go" })
      .reply(302, "", { headers: { location: "https://evil.example/secret" } });
    await expect(safeFetch("https://baozimh.org/go", {}, baozi)).rejects.toThrow(
      /允许域名/,
    );
    fetchMock
      .get("https://baozimh.org")
      .intercept({ path: "/loop" })
      .reply(302, "", { headers: { location: "https://127.0.0.1/latest" } });
    await expect(
      safeFetch("https://baozimh.org/loop", {}, baozi),
    ).rejects.toThrow(/远端地址/);
  });

  it("allows a host on the provider allowlist", async () => {
    expect(
      allowedRemoteUrl(
        "https://v2.apikk.top/api/manga/get?mid=1",
        providerHosts("baozimh_org"),
      ).hostname,
    ).toBe("v2.apikk.top");
    expect(
      allowedRemoteUrl(
        "https://c-nd2-1.6wm.top/one.png",
        providerHosts("baozimh_org"),
      ).hostname,
    ).toBe("c-nd2-1.6wm.top");
    expect(
      allowedRemoteUrl(
        "https://us.hamreus.com/path/a.jpg?e=1&m=2",
        providerHosts("manhuagui"),
      ).hostname,
    ).toBe("us.hamreus.com");
    expect(
      allowedRemoteUrl(
        "https://img.guazicdn.com/a.webp",
        providerHosts("guazimanhua"),
      ).hostname,
    ).toBe("img.guazicdn.com");
    expect(
      allowedRemoteUrl(
        "https://hipapi1.s3file.top/v2/chapter?hid=1",
        providerHosts("hipmh"),
      ).hostname,
    ).toBe("hipapi1.s3file.top");
    expect(
      allowedRemoteUrl(
        "https://image.kalaimg.top/play/min/ab",
        providerHosts("kalamanhua"),
      ).hostname,
    ).toBe("image.kalaimg.top");
    fetchMock
      .get("https://baozimh.org")
      .intercept({ path: "/manga/test" })
      .reply(302, "", {
        headers: { location: "https://c-nd2-1.6wm.top/one.png" },
      });
    fetchMock
      .get("https://c-nd2-1.6wm.top")
      .intercept({ path: "/one.png" })
      .reply(200, "img", { headers: { "content-type": "image/png" } });
    const response = await safeFetch(
      "https://baozimh.org/manga/test",
      { headers: { "User-Agent": "test" } },
      baozi,
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("img");
    expect(response.headers.get("content-type")).toContain("image/png");
  });
});
