import { expect, test } from "@playwright/test";

// End-to-end smoke tests for the main recruitment workflows. The Playwright
// config starts a temporary sidecar and Vite server with isolated data.

test("imports a JD and exposes its matching action", async ({ page }) => {
  await page.goto("/");

  await page.getByText("JD 管理").click();
  // 导入表单改成了弹层：先点「导入 JD」，内联的 JD 原文在旧版里是常驻的。
  await page.getByRole("button", { name: "导入 JD" }).click();
  await page.getByLabel("JD 原文").fill("负责支付系统，3 年 Java，本科，金融");
  await page.getByRole("button", { name: "导入并解析" }).click();
  await expect(page.getByText("导入成功")).toBeVisible();

  // 旧版断言的是「已导入 JD」标题。现版：头部「共 N 个在招岗位」在解析完成前就会先计数，
  // 真正代表列表可用的是「岗位列表 N 个」非零 + 行内「匹配」按钮出现。
  await expect(page.getByText(/岗位列表 [1-9]\d* 个/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "匹配", exact: true }).first()).toBeVisible();
});

test("loads the recruitment dashboard", async ({ page }) => {
  await page.goto("/");
  await page.getByText("数据看板").click();
  await page.getByRole("button", { name: "刷新看板" }).click();
  await expect(page.getByText("推荐总数")).toBeVisible();
});

test("creates a company in the organization mapping", async ({ page }) => {
  await page.goto("/");
  await page.getByText("Mapping").click();
  await page.getByLabel("公司名称").fill("互联网公司图谱");
  await page.getByRole("button", { name: "新建公司" }).click();
  await expect(page.getByText("互联网公司图谱")).toBeVisible();
});

test("searches BD leads", async ({ page }) => {
  await page.goto("/");
  await page.getByText("BD 助手").click();
  await page.getByLabel("BD 深度检索").fill("Java 工程师 招聘 上海");
  await page.getByRole("button", { name: "深度检索" }).click();
  await expect(page.getByText("暂无线索")).toBeVisible();
});

test("runs the health check and exposes index synchronization", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByRole("button", { name: "运行检测" }).click();
  await expect(page.getByText("数据库", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "索引同步" })).toBeVisible();
  await expect(page.getByText(/等待同步 \d+ 项，失败 \d+ 项/)).toBeVisible();
});
