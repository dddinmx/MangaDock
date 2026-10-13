import { describe, expect, it } from "vitest";
import {
  clampMaxChapters,
  DEFAULT_MAX_CHAPTERS,
  HARD_MAX_CHAPTERS,
  selectSourceChapters,
} from "../src/downloads";

const chapters = Array.from({ length: 50 }, (_, i) => ({
  id: `ch-${i + 1}`,
  order: i,
  title: `第${i + 1}话`,
}));

describe("clampMaxChapters", () => {
  it("defaults missing, 0, and invalid values to DEFAULT_MAX_CHAPTERS", () => {
    expect(DEFAULT_MAX_CHAPTERS).toBe(20);
    expect(HARD_MAX_CHAPTERS).toBe(200);
    for (const value of [
      undefined,
      null,
      "",
      "abc",
      0,
      "0",
      -1,
      -20,
      0.9,
      NaN,
      Infinity,
      -Infinity,
      {},
      [],
    ])
      expect(clampMaxChapters(value), String(value)).toBe(DEFAULT_MAX_CHAPTERS);
  });

  it("keeps a finite positive count and clamps to HARD_MAX_CHAPTERS", () => {
    expect(clampMaxChapters(1)).toBe(1);
    expect(clampMaxChapters("20")).toBe(20);
    expect(clampMaxChapters(20.9)).toBe(20);
    expect(clampMaxChapters(200)).toBe(HARD_MAX_CHAPTERS);
    expect(clampMaxChapters(201)).toBe(HARD_MAX_CHAPTERS);
    expect(clampMaxChapters(10000)).toBe(HARD_MAX_CHAPTERS);
  });
});

describe("selectSourceChapters", () => {
  it("takes the first clamped chapters for a new download", () => {
    expect(selectSourceChapters(chapters, 3).map((ch) => ch.id)).toEqual([
      "ch-1",
      "ch-2",
      "ch-3",
    ]);
    expect(selectSourceChapters(chapters, 0)).toHaveLength(DEFAULT_MAX_CHAPTERS);
    expect(selectSourceChapters(chapters, 10000)).toHaveLength(
      Math.min(HARD_MAX_CHAPTERS, chapters.length),
    );
  });

  it("takes only the newest pending chapters for an update", () => {
    const ready = chapters.slice(0, 40).map((ch) => ch.id);
    expect(
      selectSourceChapters(chapters, 3, true, ready).map((ch) => ch.id),
    ).toEqual(["ch-48", "ch-49", "ch-50"]);
    expect(
      selectSourceChapters(chapters, 0, true, ready).map((ch) => ch.id),
    ).toEqual(chapters.slice(40).map((ch) => ch.id));
    expect(
      selectSourceChapters(chapters, 5, true, ready).map((ch) => ch.id),
    ).toEqual(["ch-46", "ch-47", "ch-48", "ch-49", "ch-50"]);
    expect(selectSourceChapters(chapters, 20, true, ready)).toHaveLength(10);
  });
});
