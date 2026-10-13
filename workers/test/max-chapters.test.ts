import { describe, it, expect } from "vitest";
import {
  clampMaxChapters,
  SOURCE_BATCH_SIZE,
  HARD_MAX_CHAPTERS,
  selectSourceChapters,
} from "../src/downloads";
const chapters = Array.from({ length: 50 }, (_, i) => ({
  id: `ch-${i + 1}`,
  order: i,
  title: `第${i + 1}话`,
}));
describe("whole-book selection with bounded execution batches", () => {
  it("uses zero for a whole book and bounds explicit directory limits", () => {
    expect(SOURCE_BATCH_SIZE).toBe(20);
    expect(HARD_MAX_CHAPTERS).toBe(10000);
    for (const value of [undefined, null, "", 0, -1, "abc", Infinity, NaN])
      expect(clampMaxChapters(value)).toBe(0);
    expect(clampMaxChapters(10001)).toBe(10000);
    expect(clampMaxChapters(3)).toBe(3);
  });
  it("selects all chapters by default and skips ready chapters on repeat", () => {
    expect(selectSourceChapters(chapters, undefined)).toHaveLength(50);
    expect(
      selectSourceChapters(
        chapters,
        undefined,
        false,
        chapters.slice(0, 20).map((ch) => ch.id),
      ),
    ).toEqual(chapters.slice(20));
  });
  it("preserves an explicit first-N acceptance window", () => {
    expect(selectSourceChapters(chapters, 3, false, ["ch-1", "ch-2"])).toEqual([
      chapters[2],
    ]);
  });
  it("fills missing chapters in order for updates, including old gaps", () => {
    const ready = chapters
      .filter((_, i) => i !== 3 && i < 40)
      .map((ch) => ch.id);
    expect(
      selectSourceChapters(chapters, 3, true, ready).map((ch) => ch.id),
    ).toEqual(["ch-4", "ch-41", "ch-42"]);
    expect(selectSourceChapters(chapters, 0, true, ready)).toHaveLength(11);
  });
});
