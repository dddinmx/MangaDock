import { describe, expect, it } from "vitest";
import type { Env } from "../src/index";
import {
  decodeLibraryCursor,
  encodeLibraryCursor,
  novelPermissionsEnforced,
} from "../src/library";

describe("novel group permissions", () => {
  it("enforces group permissions unless NOVEL_GROUP_PERMISSIONS=off", () => {
    const env = {} as Env;
    expect(novelPermissionsEnforced(env)).toBe(true);
    expect(
      novelPermissionsEnforced({ ...env, NOVEL_GROUP_PERMISSIONS: "on" }),
    ).toBe(true);
    expect(
      novelPermissionsEnforced({ ...env, NOVEL_GROUP_PERMISSIONS: " OFF " }),
    ).toBe(false);
    expect(
      novelPermissionsEnforced({ ...env, NOVEL_GROUP_PERMISSIONS: "off" }),
    ).toBe(false);
  });
});

describe("library cursors", () => {
  it("round-trips an updated_at and id cursor", () => {
    const cursor = encodeLibraryCursor({ updated_at: 1700000000, id: "c_test" });
    expect(decodeLibraryCursor(cursor)).toEqual({
      updated_at: 1700000000,
      id: "c_test",
    });
  });
  it("rejects a malformed or non-integer cursor", () => {
    expect(decodeLibraryCursor("%%%")).toBeNull();
    expect(
      decodeLibraryCursor(encodeLibraryCursor({ updated_at: 1.5, id: "x" })),
    ).toBeNull();
  });
});
