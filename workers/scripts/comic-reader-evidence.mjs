import { expect } from "@playwright/test";

// Scroll restoration can finish while the newly loaded document still shows
// image placeholders. Require every comic page in the viewport to be decoded
// and visible before accepting a reading screenshot.
export async function waitForVisibleComicImages(page, timeout = 60000) {
  let visibleImages = 0;
  await expect
    .poll(
      async () => {
        const state = await page.evaluate(() => {
          const pages = [...document.querySelectorAll(".cbz-page")].filter(
            (element) => {
              const rect = element.getBoundingClientRect();
              return (
                rect.width > 0 && rect.bottom > 0 && rect.top < innerHeight
              );
            },
          );
          return {
            count: pages.length,
            loaded:
              pages.length > 0 &&
              pages.every((element) => {
                const image = element.querySelector("img.comic-page");
                return (
                  element.dataset.loading === "loaded" &&
                  image?.complete &&
                  image.naturalWidth > 0 &&
                  image.naturalHeight > 0 &&
                  getComputedStyle(image).visibility === "visible" &&
                  !element.querySelector(".cbz-page-retry")
                );
              }),
          };
        });
        visibleImages = state.count;
        return state.loaded;
      },
      {
        timeout,
        message: "Visible comic images must replace loading placeholders",
      },
    )
    .toBe(true);
  await page.evaluate(async () => {
    await Promise.all(
      [...document.querySelectorAll(".cbz-page img.comic-page")]
        .filter((image) => {
          const rect = image.getBoundingClientRect();
          return rect.width > 0 && rect.bottom > 0 && rect.top < innerHeight;
        })
        .map((image) => image.decode()),
    );
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
  });
  return visibleImages;
}
