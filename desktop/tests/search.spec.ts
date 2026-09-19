import { expect, test } from "@playwright/test";

// End-to-end smoke tests for resume import, indexing and human review. The
// Playwright config starts an isolated local sidecar and Vite server.
const RESUME_FIXTURE = "tests/fixtures/resume.pdf";

test("imports a resume and finds it", async ({ page }) => {
  await page.goto("/");

  const fileInput = page.locator('input[aria-label="选择简历文件"]');
  await fileInput.setInputFiles(RESUME_FIXTURE);

  await expect(page.getByRole("heading", { name: /候选人（共 [1-9]/ })).toBeVisible({ timeout: 30_000 });

  await page.getByLabel("人才搜索").fill("Python");
  await expect(async () => {
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await expect(page.getByRole("row", { name: /Python/ })).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] });
});

test("opens a resume revision and shows candidate actions", async ({ page }) => {
  await page.goto("/");

  await page.locator('input[aria-label="选择简历文件"]').setInputFiles(RESUME_FIXTURE);
  await expect(page.getByRole("heading", { name: /候选人（共 [1-9]/ })).toBeVisible({ timeout: 30_000 });

  await page.getByLabel("人才搜索").fill("Python");
  await expect(async () => {
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await expect(page.getByRole("row", { name: /Python/ })).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] });

  // 正常解析的候选人无需人工复核，不显示遗留的「解析与方向」入口。
  const row = page.getByRole("row", { name: /Python/ });
  await expect(row.getByRole("button", { name: "匹配" })).toBeVisible();
  await expect(row.getByRole("button", { name: "查看详情" })).toBeVisible();
  await expect(row.getByRole("button", { name: "解析与方向" })).toHaveCount(0);
});
