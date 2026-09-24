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
  test("appends the new connection instead of dropping it", () => {
    const result = mergeAiConnection(
      [conn("primary", true), conn("backup", true)],
      conn("new-third", true),
    );
    expect(result.map((c) => c.connection_id)).toEqual(["primary", "backup", "new-third"]);
  });

  test("keeps existing order and never promotes the new connection above it", () => {
    const result = mergeAiConnection(
      [conn("kept-1", false), conn("kept-2", false)],
      conn("new-connection", true),
    );
    expect(result[0].connection_id).toBe("kept-1");
    expect(result[result.length - 1].connection_id).toBe("new-connection");
  });
});
