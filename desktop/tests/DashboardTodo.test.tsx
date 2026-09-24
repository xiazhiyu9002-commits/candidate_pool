import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { DailyFollowupToday } from "../src/App";
import { DashboardPage } from "../src/pages/DashboardPage";

function followup(): DailyFollowupToday {
  return {
    followup: [
      { name: "张三", company: "某公司", title: "后端", date: "2026-09-20", item_key: "followup:case-1", done: false },
    ],
    interview: [
      { name: "李四", company: "某公司", title: "前端", time: "2026-09-20 14:00", item_key: "interview:case-2", done: true },
    ],
    reminders: [
      { id: "rem-1", candidate_id: "cand-1", name: "王五", content: "问一下期望薪资", done: false },
    ],
  };
}

function props(dailyFollowup: DailyFollowupToday) {
  return {
    dashboard: null,
    dashboardByJdData: [],
    dashboardTrend: [],
    trendGranularity: "month",
    dashboardDraft: {},
    dashboardBusy: false,
    dailyFollowup,
    jds: [],
    onDashboardDraftChange: vi.fn(),
    onApplyFilters: vi.fn(),
    onReload: vi.fn(),
    onReloadTrend: vi.fn(),
    onExportExcel: vi.fn(),
    onToggleTodo: vi.fn(),
    onCompleteReminder: vi.fn(),
  };
}

describe("今日待办勾选", () => {
  test("未勾选的项无置灰删除线，已勾选的项有", () => {
    render(<DashboardPage {...props(followup())} />);

    const pending = screen.getByText("张三 · 某公司 · 后端");
    expect(pending.className ?? "").not.toContain("is-done");

    const done = screen.getByText("李四 · 某公司 · 前端");
    expect(done.className).toContain("is-done");
  });

  test("勾选会带上稳定键原样上报，由上层负责保存", async () => {
    const user = userEvent.setup();
    const pageProps = props(followup());
    render(<DashboardPage {...pageProps} />);

    const checkbox = screen.getByLabelText("标记 张三 今日已处理");
    expect(checkbox).not.toBeChecked();

    await user.click(checkbox);

    expect(pageProps.onToggleTodo).toHaveBeenCalledWith(
      expect.objectContaining({ item_key: "followup:case-1", done: false }),
    );
  });

  test("已勾选的项也可以取消勾选", async () => {
    const user = userEvent.setup();
    const pageProps = props(followup());
    render(<DashboardPage {...pageProps} />);

    const checkbox = screen.getByLabelText("标记 李四 今日已处理");
    expect(checkbox).toBeChecked();

    await user.click(checkbox);

    expect(pageProps.onToggleTodo).toHaveBeenCalledWith(
      expect.objectContaining({ item_key: "interview:case-2", done: true }),
    );
  });

  test("我的提醒的勾选表示任务完成，上报后由上层移出", async () => {
    const user = userEvent.setup();
    const pageProps = props(followup());
    render(<DashboardPage {...pageProps} />);

    expect(screen.getByText("王五 · 问一下期望薪资")).toBeVisible();

    await user.click(screen.getByLabelText("完成 王五 的提醒"));

    expect(pageProps.onCompleteReminder).toHaveBeenCalledWith(
      expect.objectContaining({ id: "rem-1", name: "王五", content: "问一下期望薪资" }),
    );
  });
});
