import { test as base, expect } from "@playwright/test";
import { waitForVisibleComicImages } from "../../scripts/comic-reader-evidence.mjs";
import {
  createValidationReader,
  removeValidationReader,
} from "../../scripts/validation-reader.mjs";

const test = base.extend({
  reader: async ({ request, baseURL }, use) => {
    const login = await request.post("/api/v1/auth/login", {
      headers: { Origin: baseURL },
      data: { username: "admin", password: "local-e2e-password-2026" },
    });
    expect(login.ok()).toBe(true);
    const csrf = (await login.json()).data.csrf_token;
    const adminRequest = async (path, method = "GET", body) => {
      const response = await request.fetch(path, {
        method,
        headers: { Origin: baseURL, "X-CSRFToken": csrf },
        ...(body !== undefined ? { data: body } : {}),
      });
      expect(response.ok()).toBe(true);
      return response;
    };
    const detail = (await (await adminRequest("/api/comics/c_test")).json())
      .item;
    const credentials = {
      username: `md-verify-${crypto.randomUUID().slice(0, 12)}`,
      password: crypto.randomUUID() + crypto.randomUUID(),
    };
    let identity = { username: credentials.username };
    try {
      identity = await createValidationReader(adminRequest, {
        ...credentials,
        role: "user",
        group_names: detail.group_name ? [detail.group_name] : [],
      });
      await use(credentials);
    } finally {
      await removeValidationReader(adminRequest, identity);
    }
  },
});

async function login(page, reader) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill(reader.username);
  await page.getByLabel("密码").fill(reader.password);
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
}

for (const width of [1280, 390]) {
  test(`whole-book evidence waits for real images after reload at ${width}px`, async ({
    page,
    reader,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page, reader);
    await page.goto("/reader/c_test");
    await waitForVisibleComicImages(page);
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    await page.route("**/api/comic-pages/**", async (route) => {
      if (new URL(route.request().url()).searchParams.has("page")) await gate;
      await route.continue();
    });
    await page.reload();
    await expect(page.locator(".cbz-page-retry").first()).toBeVisible();
    let accepted = false;
    const evidence = waitForVisibleComicImages(page).then((count) => {
      accepted = true;
      return count;
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(accepted).toBe(false);
    release();
    expect(await evidence).toBeGreaterThan(0);
    await expect(
      page.locator('.cbz-page[data-loading="loaded"] .cbz-page-retry'),
    ).toHaveCount(0);
  });
}

test("whole-book evidence rejects a reader containing only failed image placeholders", async ({
  page,
  reader,
}) => {
  await login(page, reader);
  await page.route("**/api/comic-pages/**", async (route) => {
    if (new URL(route.request().url()).searchParams.has("page"))
      await route.fulfill({ status: 503, body: "Unavailable" });
    else await route.continue();
  });
  await page.goto("/reader/c_test");
  await expect(page.locator(".cbz-page-retry").first()).toBeVisible();
  await expect(waitForVisibleComicImages(page, 1000)).rejects.toThrow(
    /Visible comic images must replace loading placeholders/,
  );
});
