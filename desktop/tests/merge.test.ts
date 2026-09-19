import { describe, expect, test } from "vitest";

import { mergeAiConnection } from "../src/ai/merge";
import type { AiConnectionUpdate } from "../src/ai/types";

function conn(connection_id: string, enabled: boolean): AiConnectionUpdate {
  return {
    connection_id,
    provider_id: "deepseek",
    display_name: connection_id,
    enabled,
    models: { fast_text: "deepseek-v4-flash" },
  };
}

describe("mergeAiConnection", () => {
  test("replaces a disabled slot instead of dropping the new connection", () => {
    const result = mergeAiConnection(
      [conn("enabled-1", true), conn("disabled-1", false)],
      conn("new-backup", true),
    );
    expect(result.map((c) => c.connection_id)).toEqual(["enabled-1", "new-backup"]);
  });

  test("two disabled connections still keep the new primary", () => {
    const result = mergeAiConnection(
      [conn("disabled-1", false), conn("disabled-2", false)],
      conn("new-primary", true),
    );
    expect(result.map((c) => c.connection_id)).toContain("new-primary");
  });
});
