import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BACKEND_SRC = path.resolve(__dirname, "..", "backend", "src");

const SESSION_TOKEN = "0".repeat(64);
const DEFAULT_PYTHON = process.platform === "win32" ? "py -3.12" : "python3";
const PYTHON = process.env.KERUI_E2E_PYTHON ?? DEFAULT_PYTHON;
const BACKEND_PORT = process.env.KERUI_E2E_BACKEND_PORT ?? "43127";
const FRONTEND_PORT = process.env.KERUI_E2E_FRONTEND_PORT ?? "1420";

export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "on-first-retry"
  },
  webServer: [
    {
      command: `${PYTHON} -m kerui_recruit.bench.e2e_sidecar --port ${BACKEND_PORT} --token ${SESSION_TOKEN} --data-root .e2e-data`,
      cwd: "../backend",
      url: `http://127.0.0.1:${BACKEND_PORT}/health/ready`,
      env: { PYTHONPATH: BACKEND_SRC, KERUI_AI_MOCK: "1" },
      reuseExistingServer: false,
      timeout: 60_000
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT} --strictPort`,
      url: `http://127.0.0.1:${FRONTEND_PORT}`,
      env: {
        VITE_API_BASE_URL: `http://127.0.0.1:${BACKEND_PORT}`,
        VITE_SESSION_TOKEN: SESSION_TOKEN
      },
      reuseExistingServer: false,
      timeout: 60_000
    }
  ]
});
