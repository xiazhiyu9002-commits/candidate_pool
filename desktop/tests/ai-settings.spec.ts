import { expect, test } from "@playwright/test";

// Nontechnical happy path for the DeepSeek-first dual-service wizard. The
// Playwright config starts an isolated sidecar with KERUI_AI_MOCK=1, so the
// backend injects httpx.MockTransport at the transport boundary: no real
// provider or Internet is contacted, and probe/save succeed deterministically.

test("configures DeepSeek and a different backup", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置", exact: true }).click();

  await page.getByRole("button", { name: "添加 AI 服务" }).click();
  await page.getByLabel("API Key", { exact: true }).fill("e2e-deepseek-key");
  await page.getByRole("button", { name: "检测并继续" }).click();
  await page.getByRole("button", { name: "保存为主服务" }).click();

  await page.getByRole("button", { name: "添加备用 AI 服务" }).click();
  await page.getByRole("button", { name: /通义千问/ }).click();
  await page.getByLabel("API Key", { exact: true }).fill("e2e-qwen-key");
  await page.getByRole("button", { name: "检测并继续" }).click();
  await page.getByRole("button", { name: "保存为备用服务" }).click();

  await expect(page.getByText("双服务保护已开启")).toBeVisible();
});

test("re-probes an existing connection without resubmitting the key", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByRole("button", { name: "高级设置" }).click();
  await page.getByRole("button", { name: "重新探测" }).first().click();
  await expect(page.getByText("探测完成，请保存以生效。")).toBeVisible();
});
