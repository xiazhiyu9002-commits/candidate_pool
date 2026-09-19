import { expect, test, type Page } from "@playwright/test";

// 候选搜索「关键词逻辑」与「AI 语义改写」控件的端到端请求与持久化验证。
// Playwright config 会启动隔离的本地 sidecar 与 Vite 服务。

async function captureSearchBodies(page: Page): Promise<Record<string, unknown>[]> {
  const bodies: Record<string, unknown>[] = [];
  await page.route("**/api/search/candidates", async (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });
  return bodies;
}

test("keyword 同时满足 sends operator=and and never rewrite", async ({ page }) => {
  const bodies = await captureSearchBodies(page);
  await page.goto("/");
  await page.getByRole("button", { name: "关键词" }).click();
  await page.getByLabel("关键词逻辑").selectOption("and");
  await page.getByLabel("人才搜索").fill("Java 后端");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect.poll(() => bodies.length).toBeGreaterThan(0);
  expect(bodies[0]).toMatchObject({ mode: "keyword", operator: "and", rewrite_enabled: false });
});

test("vector rewrite on sends operator=smart and rewrite_enabled=true", async ({ page }) => {
  const bodies = await captureSearchBodies(page);
  await page.goto("/");
  await page.getByRole("button", { name: "向量" }).click();
  await page.getByLabel("AI 语义改写").check();
  await page.getByLabel("人才搜索").fill("交易系统后端");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect.poll(() => bodies.length).toBeGreaterThan(0);
  expect(bodies[0]).toMatchObject({ mode: "vector", operator: "smart", rewrite_enabled: true });
});

test("hybrid rewrite off sends rewrite_enabled=false", async ({ page }) => {
  const bodies = await captureSearchBodies(page);
  await page.goto("/");
  await page.getByRole("button", { name: "混合" }).click();
  await page.getByLabel("人才搜索").fill("交易系统后端");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect.poll(() => bodies.length).toBeGreaterThan(0);
  expect(bodies[0]).toMatchObject({ mode: "hybrid", operator: "smart", rewrite_enabled: false });
});

test("reload preserves keyword operator and rewrite preferences", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("search-keyword-operator:v1", "or");
    localStorage.setItem("search-rewrite-enabled:v1", "true");
  });
  await page.goto("/");
  await page.getByRole("button", { name: "关键词" }).click();
  await expect(page.getByLabel("关键词逻辑")).toHaveValue("or");
  await page.getByRole("button", { name: "向量" }).click();
  await expect(page.getByLabel("AI 语义改写")).toBeChecked();
});

test("rewrite unavailable shows a notice while results remain visible", async ({ page }) => {
  await page.route("**/api/search/candidates", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [{
          candidate_id: "c1", revision_id: "r1", name: "张三", phone: null,
          reasons: ["匹配通道：bm25"], parsed_data: null, content: "Java",
          score: 1, matched_channels: ["bm25"], total_years: null,
          highest_degree: null, location: null, original_filename: "简历.docx",
        }],
        degraded_reasons: [],
        empty_reason: null,
        status: "success",
        query_plan: { operator: "smart", rewrite_requested: true, rewrite_status: "unavailable", semantic_query: null },
      }),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "向量" }).click();
  await page.getByLabel("AI 语义改写").check();
  await page.getByLabel("人才搜索").fill("Java");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByText("AI 改写不可用，已使用原搜索词。")).toBeVisible();
  await expect(page.getByRole("row", { name: /张三/ })).toBeVisible();
});
