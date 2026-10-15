import { fetchMock } from "cloudflare:test";
import { afterEach, beforeEach, expect, it } from "vitest";
import { chapterImages } from "../src/providers";

const url = "https://www.baozimh.com/comic/chapter/fixture/0_0.html";
beforeEach(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});
afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
  fetchMock.deactivate();
});

it("extracts AMP reader images once in page order and excludes adverts", async () => {
  fetchMock
    .get("https://www.baozimh.com")
    .intercept({ path: new URL(url).pathname })
    .reply(
      200,
      `
    <img data-src="https://s1.bzcdn.net/advert.jpg">
    <amp-img class="comic-contain__item" src="/placeholder.gif" data-src="https://s1.bzcdn.net/001.jpg">
      <noscript><img src="https://s1.bzcdn.net/001.jpg"></noscript>
    </amp-img>
    <amp-img class="comic-contain__item" src="//s1.bzcdn.net/002.jpg">
      <noscript><img src="//s1.bzcdn.net/002.jpg"></noscript>
    </amp-img>`,
    );
  expect(
    await chapterImages("baozimhcn", {
      id: url,
      url,
      order: 0,
      title: "fixture",
    }),
  ).toEqual(["https://s1.bzcdn.net/001.jpg", "https://s1.bzcdn.net/002.jpg"]);
});

it("retains old reader tags and nested img while ignoring empty tags", async () => {
  fetchMock
    .get("https://www.baozimh.com")
    .intercept({ path: new URL(url).pathname })
    .reply(
      200,
      `
    <img class="comic-contain__item" data-original="/001.jpg">
    <div class="comic-contain__item"><img data-src="/002.jpg"></div>
    <img class="comic-contain__item">`,
    );
  expect(
    await chapterImages("baozimhcn", {
      id: url,
      url,
      order: 0,
      title: "fixture",
    }),
  ).toEqual([
    "https://www.baozimh.com/001.jpg",
    "https://www.baozimh.com/002.jpg",
  ]);
});

it("rejects an AMP image on an unapproved host", async () => {
  fetchMock
    .get("https://www.baozimh.com")
    .intercept({ path: new URL(url).pathname })
    .reply(
      200,
      '<amp-img class="comic-contain__item" src="https://private.invalid/image.jpg"></amp-img>',
    );
  await expect(
    chapterImages("baozimhcn", { id: url, url, order: 0, title: "fixture" }),
  ).rejects.toThrow();
});
