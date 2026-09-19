import { readFileSync } from "node:fs";


function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`missing ${name}`);
  }
  return process.argv[index + 1];
}

function load(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

const base = load(argument("--base"));
const platformConfig = load(argument("--platform-config"));
const platform = argument("--platform");
const baseResources = base.bundle?.resources ?? [];
const resources = platformConfig.bundle?.resources ?? baseResources;
const sidecars = resources.filter((resource) => resource.includes("kerui-recruit-sidecar"));

if (baseResources.some((resource) => resource.includes("kerui-recruit-sidecar"))) {
  throw new Error("platform-specific sidecars must not be declared in the base config");
}
if (sidecars.length !== 1) {
  throw new Error(`expected exactly one ${platform} sidecar resource`);
}
if (platform === "windows" && !sidecars[0].endsWith(".exe")) {
  throw new Error("the Windows sidecar must use the .exe suffix");
}
if (platform === "macos" && sidecars[0].endsWith(".exe")) {
  throw new Error("the macOS sidecar must not use the .exe suffix");
}
if (!new Set(["windows", "macos"]).has(platform)) {
  throw new Error(`unsupported platform: ${platform}`);
}
