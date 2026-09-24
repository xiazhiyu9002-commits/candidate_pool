import { expect, test, type Page } from "@playwright/test";

// End-to-end smoke tests for resume import, indexing and human review. The
// Playwright config starts an isolated local sidecar and Vite server.
//
// 用带文本层的 `resume-e2e.pdf`（342 字符的中文简历），而不是原来的 `resume.pdf`：
// 后者只有 21 个字符（`Python Finance Resume`），在未配置 AI 的 e2e 环境里本地规则
// 解析凑不够完整性门槛，修订会被判 `E_PARSE_INCOMPLETE`，导入后入不了索引，
// 「导入 → 检索」这条链路就永远测不到。
const RESUME_FIXTURE = "tests/fixtures/resume-e2e.pdf";

// 结果行按**表格结构**定位，不按姓名。姓名取决于本次解析走的是本地规则解析还是 AI mock：
// e2e 同一轮里 `tests/ai-settings.spec.ts` 会先建好一个 mock AI 连接，之后导入的简历
// 姓名来自 mock 载荷（「测试候选人」），而不是简历原文。按 `/张伟/` 之类断言会让
// 这两个用例「单跑通过、整轮跑失败」——它测的本来是「导入 → 入索引 → 搜得到」。
function resultsRow(page: Page) {
  const table = page
    .getByRole("table")
    .filter({ has: page.getByRole("columnheader", { name: "AI 画像" }) });
  // 第 0 行是表头，第 1 行是第一条数据。
  return table.getByRole("row").nth(1);
}

test("imports a resume and finds it", async ({ page }) => {
  await page.goto("/");

  const fileInput = page.locator('input[aria-label="选择简历文件"]');
  await fileInput.setInputFiles(RESUME_FIXTURE);

  await expect(page.getByText(/共 [1-9]\d* 位候选人/)).toBeVisible({ timeout: 30_000 });

  // 用技能词检索（验证 FTS/向量确实按技能命中），但**行断言要看已渲染的字段**：
  // 结果表列是 姓名/电话/学校学历/…/操作，不含技能列，按 /Python/ 找行永远找不到。
  await page.getByLabel("人才搜索").fill("Python");
  await expect(async () => {
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await expect(page.getByText("1 条搜索结果")).toBeVisible({ timeout: 1_000 });
    await expect(resultsRow(page)).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] });
});

test("opens a resume revision and shows candidate actions", async ({ page }) => {
  await page.goto("/");

  await page.locator('input[aria-label="选择简历文件"]').setInputFiles(RESUME_FIXTURE);
  await expect(page.getByText(/共 [1-9]\d* 位候选人/)).toBeVisible({ timeout: 30_000 });

  await page.getByLabel("人才搜索").fill("Python");
  await expect(async () => {
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await expect(resultsRow(page)).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 30_000, intervals: [500, 1_000, 2_000] });

  // 正常解析的候选人无需人工复核，不显示遗留的「解析与方向」入口。
  const row = resultsRow(page);
  await expect(row.getByRole("button", { name: "匹配" })).toBeVisible();
  await expect(row.getByRole("button", { name: "查看详情" })).toBeVisible();
  await expect(row.getByRole("button", { name: "解析与方向" })).toHaveCount(0);
});
