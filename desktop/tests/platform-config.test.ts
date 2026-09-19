import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, test } from "vitest";


describe("platform bundle configuration", () => {
  test("accepts the repository Windows and macOS resource splits", () => {
    expect(() => execFileSync(process.execPath, [
      "scripts/validate-platform-config.mjs",
      "--base", "src-tauri/tauri.conf.json",
      "--platform-config", "src-tauri/tauri.windows.conf.json",
      "--platform", "windows"
    ])).not.toThrow();
    expect(() => execFileSync(process.execPath, [
      "scripts/validate-platform-config.mjs",
      "--base", "src-tauri/tauri.conf.json",
      "--platform-config", "src-tauri/tauri.macos.conf.json",
      "--platform", "macos"
    ])).not.toThrow();
  });

  test("rejects a Windows executable resource in a macOS bundle", () => {
    const directory = mkdtempSync(join(tmpdir(), "kerui-platform-config-"));
    const base = join(directory, "base.json");
    const platform = join(directory, "macos.json");
    writeFileSync(base, JSON.stringify({ bundle: { resources: [] } }));
    writeFileSync(platform, JSON.stringify({ bundle: { resources: ["sidecar.exe"] } }));

    const result = spawnSync(process.execPath, [
      "scripts/validate-platform-config.mjs",
      "--base", base,
      "--platform-config", platform,
      "--platform", "macos"
    ]);

    expect(result.status).not.toBe(0);
  });
});
