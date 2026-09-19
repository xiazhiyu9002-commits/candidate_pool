import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import { App, type CaseDetail, type CaseEventItem, type RecruitmentApi } from "../src/App";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function recruitmentFixture() {
  const api = fakeApi();
  const event = (id: string, event_type: string, result: string | null = null, time = "2026-08-30T00:00:00Z"): CaseEventItem => ({
    id, event_type, result, case_round_id: event_type.startsWith("INTERVIEW") ? "round-1" : null,
    round_name: event_type.startsWith("INTERVIEW") ? "终面" : null,
    occurred_at: time, recorded_at: time, note: null, status: "active",
  });
  const detail: CaseDetail = {
    id: "case-1", candidate_id: "candidate-1", jd_id: "jd-1", stage: "初试", note: null,
    rounds: [{ id: "round-1", round_no: 1, round_name: "终面", round_type: null, skipped: false }],
    events: [event("recommend", "RECOMMENDED"), event("entered", "INTERVIEW_ENTERED"), event("pending", "INTERVIEW_RESULT", "待反馈")],
  };
  api.getCase = async () => structuredClone(detail);
  api.listCasesPage = async () => ({ items: [structuredClone(detail)], total: 1, page: 1, page_size: 20, has_more: false });
  api.recordResult = async (_caseId, roundId, result) => {
    const next = { ...event("result-final", "INTERVIEW_RESULT", result, "2026-08-31T02:00:00Z"), case_round_id: roundId };
    detail.events.push(next);
    return next;
  };
  api.voidEvent = async (id) => {
    detail.events = detail.events.filter((item) => item.id !== id);
    return { deleted: id };
  };
  return { api, event, detail };
}

async function openRecruitment(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /流程中/ }));
  await user.click(await screen.findByRole("button", { name: "查看流程" }));
  return screen.getByRole("dialog", { name: "流程中" });
}

function selectTime(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText("发生时间（上海）"), { target: { value: "2026-08-30T15:30" } });
}

describe("recruitment consistency through the App", () => {

  test("creates a linked case reminder using Shanghai time and shows its paused state", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.candidate_name = "张三";
    fixture.detail.jd_title = "后端";
    const submitted: unknown[] = [];
    fixture.api.createReminder = async (input) => {
      submitted.push(input);
      return { id: "linked-1", title: input.title, note: null, remind_at: "2026-09-02T01:30:00Z", dismissed: false, dismissed_at: null, case_id: input.case_id, paused_by_workflow: true };
    };
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    const reminderSection = within(dialog).getByRole("region", { name: "本流程跟进提醒" });
    expect(within(reminderSection).getByLabelText("提醒内容")).toHaveValue("跟进 张三 · 后端");
    expect(within(reminderSection).getByRole("button", { name: "添加提醒" })).toBeDisabled();
    fireEvent.change(within(reminderSection).getByLabelText("提醒时间（上海）"), { target: { value: "2026-09-02T09:30" } });
    await user.dblClick(within(reminderSection).getByRole("button", { name: "添加提醒" }));
    expect(await within(reminderSection).findByText("跟进 张三 · 后端", { selector: "strong" })).toBeVisible();
    expect(within(reminderSection).getByText("2026/9/2 09:30:00（上海）")).toBeVisible();
    expect(within(reminderSection).getByText("已暂停：候选人、岗位或流程状态暂不允许跟进；状态恢复后自动继续。")).toBeVisible();
    expect(submitted).toEqual([{ title: "跟进 张三 · 后端", remind_at: "2026-09-02T09:30:00+08:00", case_id: "case-1" }]);
  });

  test("keeps generic reminders available and opens a linked case from reminder management", async () => {
    const fixture = recruitmentFixture();
    fixture.api.listReminders = async () => [{ id: "linked-1", title: "客户反馈", note: null, remind_at: "2026-09-02T01:30:00Z", dismissed: false, dismissed_at: null, case_id: "case-1", paused_by_workflow: true }];
    let submitted: unknown;
    fixture.api.createReminder = async (input) => {
      submitted = input;
      return { id: "generic-1", title: input.title, note: null, remind_at: "2026-09-03T01:30:00Z", dismissed: false, dismissed_at: null, case_id: null, paused_by_workflow: false };
    };
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    await user.click(screen.getByText("设置"));
    const section = await screen.findByRole("region", { name: "提醒管理" });
    expect(await within(section).findByText("客户反馈")).toBeVisible();
    await user.type(within(section).getByLabelText("提醒内容"), "整理周报");
    fireEvent.change(within(section).getByLabelText("提醒时间（上海）"), { target: { value: "2026-09-03T09:30" } });
    await user.click(within(section).getByRole("button", { name: "添加提醒" }));
    expect(await within(section).findByText("整理周报", { selector: "strong" })).toBeVisible();
    expect(submitted).toEqual({ title: "整理周报", remind_at: "2026-09-03T09:30:00+08:00", case_id: undefined });
    await user.click(within(section).getByRole("button", { name: "查看关联流程" }));
    expect(await screen.findByRole("dialog", { name: "流程中" })).toBeVisible();
  });



  test.each(["passed", "pending"] as const)("requires an explicit name to add a round after a final %s round", async (state) => {
    const fixture = recruitmentFixture();
    fixture.detail.process_rounds = [{ round_no: 1, round_name: "终面" }];
    if (state === "passed") fixture.detail.events.push(fixture.event("final-pass", "INTERVIEW_RESULT", "通过", "2026-08-31T00:00:00Z"));
    let enteredName: string | undefined;
    function addRound(name?: string) {
      enteredName = name;
      const entered = { ...fixture.event("entered-2", "INTERVIEW_ENTERED", null, "2026-08-31T02:00:00Z"), case_round_id: "round-2", round_name: name || null };
      fixture.detail.rounds.push({ id: "round-2", round_no: 2, round_name: name || "", round_type: null, skipped: false });
      fixture.detail.events.push(entered);
      return entered;
    }
    fixture.api.enterInterview = async (_caseId, payload) => addRound(payload?.round_name);
    fixture.api.passAndAdvance = async (_caseId, _roundId, payload) => {
      const passed = fixture.event("final-pass", "INTERVIEW_RESULT", "通过", "2026-08-31T01:00:00Z");
      fixture.detail.events.push(passed);
      return [passed, addRound(payload?.next_round_name)];
    };
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    expect(within(dialog).getByRole("button", { name: "进入面试" })).toBeDisabled();
    expect(within(dialog).queryByRole("button", { name: "通过进下一轮" })).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("临时轮次名称"), "   ");
    expect(within(dialog).getByRole("button", { name: "进入面试" })).toBeDisabled();
    expect(within(dialog).queryByRole("button", { name: "通过进下一轮" })).not.toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("临时轮次名称"));
    await user.type(within(dialog).getByLabelText("临时轮次名称"), "  补充沟通  ");
    selectTime(dialog);
    await user.click(within(dialog).getByRole("button", { name: state === "passed" ? "进入面试" : "通过进下一轮" }));
    expect(await within(dialog).findByText("第2轮 · 补充沟通")).toBeVisible();
    expect(enteredName).toBe("补充沟通");
    expect(fixture.detail.process_rounds).toEqual([{ round_no: 1, round_name: "终面" }]);
    expect(within(dialog).getByLabelText("临时轮次名称")).toHaveValue("");
  });

  test("still blocks named add-on interviews after an offer was issued", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.process_rounds = [{ round_no: 1, round_name: "终面" }];
    fixture.detail.events.push(fixture.event("offer-issued", "OFFER", "已发放", "2026-08-31T00:00:00Z"));
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    await user.type(within(dialog).getByLabelText("临时轮次名称"), "补充沟通");
    expect(within(dialog).getByRole("button", { name: "进入面试" })).toBeDisabled();
    expect(within(dialog).queryByRole("button", { name: "通过进下一轮" })).not.toBeInTheDocument();
    expect(fixture.detail.rounds).toHaveLength(1);
  });



  test("separates incompatible index versions from retryable sync failures", async () => {
    const api = fakeApi();
    api.indexStatus = async () => ({
      pending: 0, failed: 0, items: [],
      indexes: [{ entity_type: "candidate", compatible: false, error: "INDEX_VERSION_MISMATCH" }, { entity_type: "jd", compatible: true, error: null }],
    });
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    const section = await screen.findByRole("region", { name: "索引同步" });
    expect(await within(section).findByText("候选人索引：版本不兼容")).toBeVisible();
    expect(within(section).getByText("INDEX_VERSION_MISMATCH")).toBeVisible();
    expect(within(section).getByText("索引与当前版本不兼容：退出并重新启动应用，启动时会自动重建。")).toBeVisible();
    expect(within(section).getByRole("button", { name: "重试失败同步" })).toBeDisabled();
    expect(within(section).getByText("岗位索引：版本兼容")).toBeVisible();
  });



  test("shows index sync failures and requests retry without claiming a rebuild or completion", async () => {
    const api = fakeApi();
    let retries = 0;
    api.indexStatus = async () => ({ pending: 2, failed: 1, items: [{ entity_type: "candidate", entity_id: "candidate-1", status: "RETRY_WAIT", attempts: 2, error: "TimeoutError" }] });
    let resolveRetry!: (value: Awaited<ReturnType<RecruitmentApi["retryIndexSync"]>>) => void;
    api.retryIndexSync = async () => { retries++; return new Promise((resolve) => { resolveRetry = resolve; }); };
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    const section = await screen.findByRole("region", { name: "索引同步" });
    expect(await within(section).findByText("TimeoutError")).toBeVisible();
    expect(within(section).getByText("等待同步 2 项，失败 1 项")).toBeVisible();
    await user.dblClick(within(section).getByRole("button", { name: "重试失败同步" }));
    expect(retries).toBe(1);
    expect(within(section).getByRole("button", { name: "重试失败同步" })).toBeDisabled();
    await act(async () => resolveRetry({ pending: 2, failed: 0, items: [{ entity_type: "candidate", entity_id: "candidate-1", status: "PENDING", attempts: 2, error: null }] }));
    expect(await within(section).findByText("已请求重试，等待后台同步完成。")).toBeVisible();
    expect(within(section).getByText("等待同步 2 项，失败 0 项")).toBeVisible();
    expect(within(section).queryByText("同步完成")).not.toBeInTheDocument();
  });

  test("makes a failed reparse visible even when the accepted resume remains READY", async () => {
    const api = fakeApi();
    api.listCandidates = async () => [{ candidate_id: "candidate-1", revision_id: "revision-1", display_name: "人工确认", total_years: 5, highest_degree: null, location: null, status: "AVAILABLE", revision_status: "READY", phone: null, original_filename: "resume.pdf", parsed_data: { name: "人工确认", skills: ["Python"] }, error_code: "E_OCR_FAILED", error_message: "重新解析失败，已保留人工资料" }];
    const user = userEvent.setup();
    render(<App api={api} />);
    expect(await screen.findByText("重新解析失败，已保留人工资料")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "解析失败·重新解析" }));
    expect(await screen.findByRole("dialog", { name: "简历解析详情" })).toBeVisible();
  });

  test("breaks equal event timestamps by event id like the backend projection", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.events = [
      fixture.event("recommend", "RECOMMENDED"), fixture.event("entered", "INTERVIEW_ENTERED"),
      fixture.event("z-passed", "INTERVIEW_RESULT", "通过"),
      fixture.event("a-pending", "INTERVIEW_RESULT", "待反馈"),
    ];
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("通过");
    expect(within(dialog).queryByRole("button", { name: "通过（结束面试）" })).not.toBeInTheDocument();
  });


  test("shows a failed resume as read-only review with original text", async () => {
    const api = fakeApi();
    api.listCandidates = async () => [{ candidate_id: "candidate-1", revision_id: "revision-1", display_name: "已确认姓名", total_years: 5, highest_degree: null, location: null, status: "ACTIVE", revision_status: "FAILED", phone: null, original_filename: "resume.pdf", parsed_data: null }];
    api.getResumeReview = async () => ({
      candidate_id: "candidate-1", revision_id: "revision-1", status: "FAILED", review_required: true,
      raw_text: "原始简历正文", parsed_data: null, review_data: null, manual_overrides: {},
      extraction_diagnostics: {}, error_code: "E_PARSE_INCOMPLETE", error_message: "解析结果不完整，已归为不合格，可重新解析",
    });
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(await screen.findByRole("button", { name: "解析失败·重新解析" }));
    const dialog = await screen.findByRole("dialog", { name: "简历解析详情" });
    expect(within(dialog).getByText("解析结果不完整，已归为不合格，可重新解析")).toBeVisible();
    expect(within(dialog).getByText("原始简历正文", { selector: "pre" })).toBeVisible();
  });


  test("reopens a newly created case from its original match result", async () => {
    const fixture = recruitmentFixture();
    fixture.api.listJds = async () => [{ jd_id: "jd-1", revision_id: "rev-1", company: "金融公司", title: "后端", status: "READY", jd_status: "OPEN", ai_category: null, location: null, min_years: null, parsed_data: null, source_text: null }];
    fixture.api.listJdsPage = async () => ({ items: [{ jd_id: "jd-1", revision_id: "rev-1", company: "金融公司", title: "后端", status: "READY", jd_status: "OPEN", ai_category: null, location: null, min_years: null, parsed_data: null, source_text: null }], total: 1, page: 1, page_size: 10, has_more: false });
    const match = fixture.api.matchJd;
    fixture.api.matchJd = async (...args) => { const response = await match(...args); return { ...response, items: response.items.map((item) => ({ ...item, result_id: "result-1" })) }; };
    let created = 0;
    fixture.api.createCaseFromMatchResult = async () => { created += 1; return { case_id: "case-1", result_id: "result-1", status: "保留" }; };
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    await user.click(screen.getByText("JD 管理"));
    await user.click(await screen.findByRole("button", { name: "匹配" }));
    await user.click(screen.getByRole("button", { name: "建流程" }));
    await user.click(within(await screen.findByRole("dialog", { name: "流程中" })).getByRole("button", { name: "关闭" }));
    await user.click(screen.getByRole("button", { name: "查看流程" }));
    expect(await screen.findByRole("dialog", { name: "流程中" })).toBeVisible();
    expect(created).toBe(1);
  });

  test("shows final snapshot and closed-job controls without allowing accidental advancement", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.process_rounds = [{ round_no: 1, round_name: "终面" }];
    fixture.detail.can_advance = false;
    fixture.detail.blocked_reason = "岗位已关闭";
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    expect(within(dialog).getByText("岗位已关闭")).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "通过进下一轮" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "通过（结束面试）" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "作废面试结果 pending" })).toBeEnabled();
    expect(within(dialog).getAllByText("2026/8/30 08:00:00（上海）")).toHaveLength(3);
  });

  test("reopens an existing case after closing its drawer", async () => {
    const fixture = recruitmentFixture();
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    let dialog = await openRecruitment(user);
    expect(within(dialog).getByText("第1轮 · 终面")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));
    fixture.detail.note = "下周继续跟进";
    await user.click(screen.getByRole("button", { name: "查看流程" }));
    dialog = await screen.findByRole("dialog", { name: "流程中" });
    expect(within(dialog).getByText("下周继续跟进")).toBeVisible();
  });

  test("resolves pending feedback with a final pass without creating another round", async () => {
    const fixture = recruitmentFixture();
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    selectTime(dialog);
    await user.click(within(dialog).getByRole("button", { name: "通过（结束面试）" }));
    await waitFor(() => expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("通过"));
    expect(fixture.detail.rounds).toHaveLength(1);
    expect(fixture.detail.events.filter((e) => e.event_type === "INTERVIEW_RESULT").map((e) => e.result)).toEqual(["待反馈", "通过"]);
  });

  test("uses the newest effective result and restores actions after that result is voided", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.events.push(fixture.event("passed", "INTERVIEW_RESULT", "通过", "2026-08-31T00:00:00Z"));
    fixture.detail.events.unshift({ ...fixture.event("void-fail", "INTERVIEW_RESULT", "未通过", "2026-09-01T00:00:00Z"), status: "void" });
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("通过");
    expect(within(dialog).queryByRole("button", { name: "通过（结束面试）" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "作废面试结果 passed" }));
    selectTime(dialog);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "通过（结束面试）" })).toBeEnabled());
    expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("待反馈");
  });

  test("hides voided entries from the timeline and offers no result actions", async () => {
    const fixture = recruitmentFixture();
    fixture.detail.events.find((e) => e.id === "entered")!.status = "void";
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("待反馈");
    expect(within(dialog).queryByRole("button", { name: "通过（结束面试）" })).not.toBeInTheDocument();
    selectTime(dialog);
    expect(within(dialog).getByRole("button", { name: "进入面试" })).toBeEnabled();
  });

  test("blocks duplicate clicks and reuses the same request identity after uncertain failure", async () => {
    const fixture = recruitmentFixture();
    const bodies: unknown[] = [];
    let rejectFirst!: (error: Error) => void;
    fixture.api.recordResult = async (...args) => {
      bodies.push(args[3]);
      if (bodies.length === 1) await new Promise<void>((_resolve, reject) => { rejectFirst = reject; });
      const next = fixture.event("passed", "INTERVIEW_RESULT", args[2], "2026-08-31T03:00:00Z");
      fixture.detail.events.push(next);
      return next;
    };
    const user = userEvent.setup();
    render(<App api={fixture.api} />);
    const dialog = await openRecruitment(user);
    fireEvent.change(within(dialog).getByLabelText("发生时间（上海）"), { target: { value: "2026-08-30T15:30" } });
    await user.type(within(dialog).getByLabelText("操作备注"), "电话确认");
    const pass = within(dialog).getByRole("button", { name: "通过（结束面试）" });
    await user.dblClick(pass);
    expect(pass).toBeDisabled();
    expect(bodies).toHaveLength(1);
    await act(async () => rejectFirst(new Error("响应中断")));
    await user.click(within(dialog).getByRole("button", { name: "重试上次操作" }));
    await waitFor(() => expect(within(dialog).getByLabelText("第1轮当前结果")).toHaveTextContent("通过"));
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[0]).toMatchObject({ occurred_at: "2026-08-30T15:30:00+08:00", note: "电话确认", idempotency_key: expect.any(String) });
  });

  test("uses one applied company, JD and date scope for every dashboard view and export", async () => {
    const api = fakeApi();
    const scopes: Record<string, unknown> = {};
    api.listJds = async () => [{ jd_id: "jd-finance", revision_id: "rev-1", company: "金融公司", title: "后端", status: "READY", jd_status: "OPEN", ai_category: null, location: null, min_years: null, parsed_data: null, source_text: null }];
    api.dashboardOverview = async (filters) => {
      scopes.overview = filters;
      return { recommendation_total: filters?.company === "金融公司" ? 7 : 99, offer_total: 1, active_offer_total: 1, onboarded_total: 0, candidate_total: 2, monthly_new_candidates: [] };
    };
    api.dashboardByJd = async (filters) => { scopes.byJd = filters; return []; };
    api.dashboardTrend = async (_granularity, filters) => { scopes.trend = filters; return []; };
    api.dashboardExport = async (filters) => { scopes.export = filters; };
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("数据看板"));
    await user.selectOptions(await screen.findByLabelText("看板公司"), "金融公司");
    await user.selectOptions(screen.getByLabelText("看板岗位"), "jd-finance");
    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2026-08-01" } });
    fireEvent.change(screen.getByLabelText("结束日期"), { target: { value: "2026-08-31" } });
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(await screen.findByText("7", { selector: "strong" })).toBeVisible();
    fireEvent.change(screen.getByLabelText("开始日期"), { target: { value: "2026-08-20" } });
    await user.click(screen.getByRole("button", { name: "周" }));
    await user.click(screen.getByRole("button", { name: "导出 Excel" }));
    const expected = { company: "金融公司", jd_id: "jd-finance", date_from: "2026-08-01", date_to: "2026-08-31" };
    expect(scopes).toEqual({ overview: expected, byJd: expected, trend: expected, export: expected });
  });
});



function fakeApi(): RecruitmentApi {
  return {
    importResume: async () => ({
      action: "CREATED",
      candidate_id: "candidate-1",
      document_id: "document-1",
      revision_id: "revision-1",
      blob_id: "blob-1",
      task_id: "task-1",
      message: "",
      conflict_candidate_ids: [],
      created_task: true
    }),
    importFolder: async () => ({ imported: [], skipped: [], errors: [] }),
    getTask: async () => ({
      id: "task-1",
      task_type: "PARSE_RESUME",
      status: "SUCCESS",
      progress: 100,
      error_message: null
    }),
    listTasks: async () => [],
    getTaskStatusBatch: async (taskIds) => ({ found: [], missing_ids: taskIds }),
    controlTask: async (_taskId, action) => ({
      id: "task-1",
      task_type: "PARSE_RESUME",
      status: action === "pause" ? "PAUSED" : "QUEUED",
      progress: 20,
      error_message: null
    }),
    triggerBackfill: async (kind) => ({ task_id: `backfill-${kind}`, task_type: "BACKFILL" }),
    listResumeRevisions: async () => [],
    lookupPool: async () => [],
    testMail: async () => ({ imap: { ok: true, message: "ok" }, smtp: { ok: true, message: "ok" } }),
    sendMailConfirmation: async () => ({ sent: true, to: "a@b.com", message: "确认邮件已发送" }),
    sendFollowupTest: async () => ({ sent: true, to: "a@b.com", message: "测试报告已发送" }),
    syncMail: async () => ({ ingested: 0, revision_ids: [] }),
    mailStatus: async () => ({ configured: false, last_uid: 0 }),
    reviseOrgImport: async (draft) => draft,
    downloadResume: async () => undefined,
    previewResume: async () => "blob:preview",
    viewResume: async () => ({ kind: "preview", filename: "resume.pdf", url: "blob:preview" }),
    listCandidates: async () => [],
    listDirectionPending: async () => [],
    listJds: async () => [],
    listJdsPage: async () => ({ items: [], total: 0, page: 1, page_size: 10, has_more: false }),
    updateCandidateField: async (candidateId, field, value) => ({ candidate_id: candidateId, revision_id: "revision-1", field, value }),
    updateCandidateParsed: async (candidateId) => ({ candidate_id: candidateId, revision_id: "revision-1", updated_fields: [] }),
    updateJdParsed: async (jdId, parsedData) => ({ jd_id: jdId, revision_id: "revision-1", field: "parsed_data", value: parsedData }),
    dailyFollowupToday: async () => ({ followup: [], interview: [] }),
    regenerateCandidateProfile: async () => ({ generated: true, summary: "测试画像", input_hash: "hash" }),
    parseJdConstraints: async () => ({ constraints: [] }),
    deleteCandidate: async (candidateId) => ({ candidate_id: candidateId, deleted: true }),
    deleteJd: async (jdId) => ({ jd_id: jdId, deleted: true }),
    bulkDeleteCandidates: async () => ({ results: [], succeeded: 0, failed: 0 }),
    bulkForceOcr: async () => ({ results: [], succeeded: 0, failed: 0 }),
    bulkReparse: async () => ({ results: [], succeeded: 0, failed: 0 }),
    bulkDownloadCandidates: async () => {},
    bulkMatchCandidates: async () => ({ results: [], jd_details: [] }),
    bulkDeleteJds: async () => ({ results: [], succeeded: 0, failed: 0 }),
    bulkDeleteCases: async () => ({ results: [], succeeded: 0, failed: 0 }),
    switchResumeRevision: async (revisionId) => ({
      revision_id: revisionId,
      display_name: "候选人",
      original_filename: "resume.pdf",
      status: "READY",
      is_current: true,
      created_at: "2026-08-30T00:00:00Z",
      parsed_data: null
    }),
    reparseResume: async (revisionId) => ({ revision_id: revisionId, task_id: "task-1" }),
    getResumeReview: async (revisionId) => ({ candidate_id: "candidate-1", revision_id: revisionId, status: "READY", review_required: false, raw_text: "", parsed_data: null, review_data: null, manual_overrides: {}, extraction_diagnostics: {}, error_code: null, error_message: null }),
    indexStatus: async () => ({ pending: 0, failed: 0, items: [] }),
    retryIndexSync: async () => ({ pending: 0, failed: 0, items: [] }),
    searchCandidates: async () => ({
      items: [
        {
          candidate_id: "candidate-1",
          revision_id: "revision-1",
          name: "张三",
          phone: null,
          reasons: [],
          parsed_data: null,
          content: "张三 Python 金融风控",
          score: 0.98,
          matched_channels: ["bm25", "vector"],
          total_years: 6,
          highest_degree: "MASTER",
          location: "上海"
        }
      ],
      degraded_reasons: []
    }),
    importJd: async () => ({ jd_id: "jd-1", revision_id: "rev-1" }),
    importJdFile: async () => ({ jd_id: "jd-file-1", revision_id: "rev-file-1" }),
    importJdBatch: async () => ({ imported: [{ jd_id: "jd-1", revision_id: "rev-1" }] }),
    importJdBatchFile: async () => ({ imported: [{ jd_id: "jd-file-1", revision_id: "rev-file-1" }] }),
    matchJd: async () => ({
      run_id: "run-1",
      items: [
        {
          candidate_id: "candidate-1",
          revision_id: "revision-1",
          name: "张三",
          phone: null,
          reasons: [],
          parsed_data: null,
          content: "张三 Python 金融风控",
          score: 0.95,
          matched_channels: ["bm25", "vector"],
          total_years: 6,
          highest_degree: "MASTER",
          location: "上海"
        }
      ]
    }),
    startAiReview: async () => ({ review_id: "review-1", status: "QUEUED" }),
    getAiReview: async () => ({ status: "SUCCESS", progress: 100, result_ref: null, error_message: null }),
    startSearchReview: async () => ({ review_id: "search-review-1", status: "QUEUED", query_key: "qk-1" }),
    getSearchReview: async () => ({ status: "SUCCESS", progress: 100, error_message: null, query_key: "qk-1", items: [] }),
    matchBatch: async (revisionIds) => ({
      results: revisionIds.map((revisionId) => ({
        revision_id: revisionId,
        run_id: `run-${revisionId}`,
        items: []
      }))
    }),
    markMatchResult: async (resultId, status) => ({ result_id: resultId, status }),
    listMatchResults: async () => ({ groups: [] }),
    listMatchResultsForCandidate: async () => [],
    matchCandidate: async () => ({ run_id: null, items: [] }),
    createCaseFromMatchResult: async (resultId) => ({ case_id: "case-1", result_id: resultId, status: "保留" }),
    updateJdStatus: async (jdId, status) => ({ jd_id: jdId, status }),
    updateJdField: async (jdId, field, value) => ({ jd_id: jdId, revision_id: "rev-1", field, value }),
    regenerateJdProfile: async () => ({ generated: true, summary: "测试画像", input_hash: "hash" }),
    exportMatchJd: async () => undefined,
    health: async () => ({
      database: { status: "healthy" },
      search: { status: "healthy" }
    }),
    diagnostics: async () => ({
      sqlite_version: "3.45.3",
      database_path: "/tmp/recruit.sqlite3",
      database_size_bytes: 2048,
      counts: { candidate: 3, jd: 1 },
      pragmas: { journal_mode: "wal" }
    }),
    exportDiagnostics: async () => undefined,
    listMappingProjects: async () => [],
    createMappingProject: async (name: string) => ({ id: "proj-1", name, description: null }),
    buildMappingTree: async () => ({ id: "snap-1", label: "v1", is_current: true }),
    listMappingSnapshots: async () => [],
    getMappingTree: async () => [],
    searchBdLeads: async () => [],
    searchLeadsForCandidate: async () => [],
    updateLeadStatus: async () => ({ id: "lead-1", source: "web", company_name: "某公司", job_title: null, raw_snippet: null, url: null, status: "已联系" }),
    runBdAgent: async () => ({ session_id: "session-1", leads: [] }),
    runBdAgentStream: async () => ({ session_id: "session-1", leads: [] }),
    followUpBdAgent: async () => ({ session_id: "session-1", leads: [] }),
    createCase: async () => ({ id: "case-1", candidate_id: "candidate-1", jd_id: "jd-1", stage: "待评估", note: null }),
    listCasesPage: async () => ({ items: [], total: 0, page: 1, page_size: 20, has_more: false }),
    getCase: async () => ({
      id: "case-1",
      candidate_id: "candidate-1",
      jd_id: "jd-1",
      stage: "待评估",
      note: null,
      rounds: [],
      events: []
    }),
    deleteCase: async () => ({ deleted: "case-1" }),
    recommendCase: async () => ({ id: "evt-1", event_type: "RECOMMENDED", case_round_id: null, round_name: null, occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: null, note: null, status: "active" }),
    enterInterview: async () => ({ id: "evt-2", event_type: "INTERVIEW_ENTERED", case_round_id: "round-1", round_name: "第1轮", occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: null, note: null, status: "active" }),
    recordResult: async () => ({ id: "evt-3", event_type: "INTERVIEW_RESULT", case_round_id: "round-1", round_name: "第1轮", occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: "通过", note: null, status: "active" }),
    passAndAdvance: async () => [],
    offerCase: async () => ({ id: "evt-4", event_type: "OFFER", case_round_id: null, round_name: null, occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: "已发放", note: null, status: "active" }),
    onboardCase: async () => ({ id: "evt-5", event_type: "ONBOARDED", case_round_id: null, round_name: null, occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: null, note: null, status: "active" }),
    exitCase: async () => ({ id: "evt-6", event_type: "EXIT", case_round_id: null, round_name: null, occurred_at: "2026-08-31T00:00:00Z", recorded_at: "2026-08-31T00:00:00Z", result: null, note: null, status: "active" }),
    voidEvent: async () => ({ deleted: "evt-1" }),
    dashboardOverview: async () => ({
      recommendation_total: 1,
      offer_total: 1,
      active_offer_total: 1,
      onboarded_total: 0,
      candidate_total: 2,
      monthly_new_candidates: [{ month: "2026-08", count: 2 }]
    }),
    dashboardByJd: async () => [],
    dashboardTrend: async () => [],
    dashboardExport: async () => undefined,
    reverseMatch: async () => [],
    getCandidateContact: async () => ({ email: "zhang@example.com", phone: "13800138000", email_confidence: 0.9, phone_confidence: 0.9 }),
    updateCandidateContact: async (_candidateId, input) => ({ email: input.email, phone: input.phone, email_confidence: 1.0, phone_confidence: 1.0 }),
    applyCorrection: async (input) => ({
      correction_id: "correction-1",
      entity_type: input.entityType,
      field_name: input.fieldName,
      old_value: "张三",
      new_value: input.newValue,
      reverted: false
    }),
    undoCorrection: async () => ({
      correction_id: "correction-1",
      entity_type: "candidate",
      field_name: "display_name",
      old_value: "张三",
      new_value: "张四",
      reverted: true
    }),
    exportMappingTree: async () => undefined,
    exportMappingTreePdf: async () => undefined,
    listCompanies: async () => [],
    createCompany: async (name: string) => ({ id: "company-1", name }),
    updateCompany: async (companyId: string, name: string) => ({ id: companyId, name }),
    listDepartments: async () => [],
    createDepartment: async (input) => ({ id: "dept-1", company_id: input.company_id, parent_id: input.parent_id ?? null, name: input.name, leader_id: null, leader_report_to: null, team_size: null, business_direction: null, tech_stack: null, office_location: null, hc_status: null, hc_internal_note: null }),
    listEmployees: async () => [],
    createEmployee: async (input) => ({ id: "emp-1", company_id: input.company_id, department_id: input.department_id ?? null, candidate_id: null, candidate_name: null, current_revision_id: null, name: input.name, title: input.title ?? null, job_level: input.job_level ?? null, report_to: input.report_to ?? null, subordinate_count: null, tenure_years: null, business_module: null, status: null, intention: null, remark: null, contact: null, is_key: input.is_key ?? false }),
    getOrgTree: async () => ({ id: "company-1", kind: "company", name: "字节跳动", title: null, job_level: null, team_size: null, is_key: false, children: [] }),
    exportOrgInternal: async () => undefined,
    exportOrgClient: async () => undefined,
    exportOrgArchPdf: async () => undefined,
    updateDepartment: async (departmentId, changes) => ({ id: departmentId, company_id: "company-1", parent_id: null, name: changes.name ?? "部门", leader_id: null, leader_report_to: null, team_size: null, business_direction: null, tech_stack: null, office_location: null, hc_status: null, hc_internal_note: null }),
    deleteDepartment: async () => undefined,
    updateEmployee: async (employeeId, changes) => ({ id: employeeId, company_id: "company-1", department_id: null, candidate_id: null, candidate_name: null, current_revision_id: null, name: changes.name ?? "人员", title: null, job_level: null, report_to: null, subordinate_count: null, tenure_years: null, business_module: null, status: null, intention: null, remark: null, contact: null, is_key: false }),
    deleteEmployee: async () => undefined,
    deleteCompany: async () => undefined,
    parseOrgImport: async () => ({ draft: { company_name: "得物", departments: [], employees: [] }, questions: [] }),
    parseOrgWord: async () => ({ result: { draft: { company_name: "得物", departments: [], employees: [] }, questions: [] }, source_text: "得物" }),
    answerOrgImport: async () => ({ draft: { company_name: "得物", departments: [], employees: [] }, questions: [] }),
    commitOrgImport: async () => ({ departments: 0, employees: 0 }),
    getCompanySource: async () => ({ company_id: "company-1", source_text: "得物 · 10 个部门 · 10 名人员" }),
    bindEmployee: async (employeeId) => ({ employee_id: employeeId, matched: false, candidate_id: null, candidate_name: null, name_mismatch: false }),
    getSettings: async () => ({}),
    updateSettings: async () => ({}),
    getVendors: async () => [],
    getAiCatalog: async () => ({ version: 1, default_provider_id: "deepseek", providers: [] }),
    refreshAiCatalog: async () => ({ status: "disabled", active_version: 1, message: "" }),
    getAiConfig: async () => ({ protection_level: "none", connections: [], catalog_version: 1 }),
    updateAiConfig: async () => ({ protection_level: "single", connections: [], catalog_version: 1 }),
    probeAiConnection: async () => ({ auth: { ok: true, error_code: null, suggested_action: null }, text: { ok: true, error_code: null, suggested_action: null }, json: { ok: true, error_code: null, suggested_action: null }, reasoning: { ok: true, error_code: null, suggested_action: null }, vision: { ok: true, error_code: null, suggested_action: null }, role_models: { fast_text: "deepseek-v4-flash", reasoning_text: "deepseek-v4-pro", vision: "deepseek-v4-flash-vision-exp" }, models: [] }),
    getAiStatus: async () => ({ connections: [], last_fallback: null }),
    exportMatchRun: async () => undefined,
    listBackups: async () => [],
    createBackup: async () => ({ filename: "backup_1.sqlite3", path: "/tmp/backup_1.sqlite3" }),
    restoreBackup: async () => ({ restored_from: "backup_1.sqlite3", safety_backup: "/tmp/safety.sqlite3" }),
    createPortableBackup: async (targetPath) => ({ path: targetPath, same_volume: false }),
    restorePortableBackup: async (_backupPath, targetRoot) => ({ target_root: targetRoot, files_restored: 3, files_verified: 3, ok: true }),
    listReminders: async () => [],
    createReminder: async () => ({ id: "reminder-1", title: "跟进", note: null, remind_at: "2026-08-29T09:00:00", dismissed: false, dismissed_at: null }),
    dismissReminder: async () => ({ id: "reminder-1", title: "跟进", note: null, remind_at: "2026-08-29T09:00:00", dismissed: true, dismissed_at: "2026-08-29T10:00:00" }),
    migrateData: async () => ({ target_root: "/tmp/new", files_copied: 3, files_verified: 3, candidate_count: 1, ok: true }),
    setDataRoot: async (path: string) => path,
    onboardingStatus: async () => ({ data_root: "/tmp/data", llm_enabled: false, search_enabled: false, bd_search_enabled: false, mail_enabled: false, smtp_enabled: false, health: { database: { status: "healthy" } } }),
    testProviders: async () => [{ name: "llm", ok: true, message: "可用" }]
  };
}

describe("reviewed desktop reliability", () => {
  test("keeps the OCR drawer open through an initial status network failure", async () => {
    const api = fakeApi();
    const oldReview = await api.getResumeReview("revision-1");
    api.getResumeReview = vi.fn().mockResolvedValueOnce({ ...oldReview, parsed_data: { name: "old" } })
      .mockResolvedValue({ ...oldReview, parsed_data: { name: "new OCR" }, raw_text: "updated evidence" });
    api.getTask = vi.fn().mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValue({ id: "task-1", task_type: "PARSE_RESUME", status: "SUCCESS", progress: 100, error_message: null });
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 6,
      highest_degree: "MASTER",
      location: "上海",
      status: "PENDING_REVIEW",
      revision_status: "FAILED",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三" },
      error_code: "E_STRUCTURED_EMPTY",
      error_message: "解析失败",
    }]);
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(await screen.findByRole("button", { name: "解析失败·重新解析" }));
    const drawer = screen.getByRole("dialog", { name: "简历解析详情" });
    await user.click(within(drawer).getByRole("button", { name: "重新解析" }));
    expect(await within(drawer).findByRole("button", { name: "解析中…" })).toBeDisabled();
    await waitFor(() => expect(within(drawer).getByText("updated evidence")).toBeVisible(), { timeout: 4000 });
  });
  test("loads saved search settings and removes SerpApi from the form", async () => {
    const api = fakeApi();
    api.getSettings = vi.fn(async () => ({ siliconflow_api_key: "sf-saved", tavily_api_key: "tv-saved" }));
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    expect(await screen.findByDisplayValue("sf-saved")).toBeVisible();
    expect(screen.getByDisplayValue("tv-saved")).toBeVisible();
    expect(screen.queryByLabelText("SerpApi API Key")).not.toBeInTheDocument();
  });

  test("searches using filters only and distinguishes zero matches", async () => {
    const api = fakeApi();
    api.searchCandidates = vi.fn(async () => ({ items: [], degraded_reasons: [], empty_reason: "no_match" }));
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("精确筛选"));
    await user.selectOptions(screen.getByLabelText("最低学历"), "MASTER");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    expect(api.searchCandidates).toHaveBeenCalledWith("", { highest_degree: "MASTER" }, { mode: "hybrid", operator: "smart", rewriteEnabled: false, searchBody: false });
    expect(await screen.findByText("没有符合条件的候选人")).toBeVisible();
  });

  test("uses the document viewing service for Word details without downloading", async () => {
    const api = fakeApi();
    api.searchCandidates = async () => ({ items: [{ candidate_id: "c", revision_id: "r", name: "张三", phone: null, reasons: [], parsed_data: null, content: "", score: 1, matched_channels: [], total_years: null, highest_degree: null, location: null, original_filename: "简历.docx" }], degraded_reasons: [] });
    api.viewResume = vi.fn(async () => ({ kind: "opened" as const, filename: "简历.docx" }));
    api.downloadResume = vi.fn();
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.type(screen.getByLabelText("人才搜索"), "张三");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await user.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(api.viewResume).toHaveBeenCalledWith("r");
    expect(api.downloadResume).not.toHaveBeenCalled();
  });

  test("reviews selected search results and shows highlights and risks", async () => {
    const api = fakeApi();
    api.searchCandidates = async () => ({
      items: [
        { candidate_id: "c1", revision_id: "r1", name: "张三", phone: null, reasons: [], parsed_data: null, content: "", score: 1, matched_channels: [], total_years: 6, highest_degree: "MASTER", location: "上海", original_filename: "a.pdf" },
        { candidate_id: "c2", revision_id: "r2", name: "李四", phone: null, reasons: [], parsed_data: null, content: "", score: 0.9, matched_channels: [], total_years: 3, highest_degree: null, location: null, original_filename: "b.pdf" },
      ],
      degraded_reasons: [],
    });
    const startSearchReview = vi.fn(async () => ({ review_id: "sr-1", status: "QUEUED", query_key: "qk-1" }));
    api.startSearchReview = startSearchReview;
    api.getSearchReview = vi.fn(async () => ({
      status: "SUCCESS", progress: 100, error_message: null, query_key: "qk-1",
      items: [
        { candidate_id: "c1", verdict: "recommend", highlights: ["支付经验匹配（projects[0]）"], risks: ["未见管理经验"], failed: false, error: null },
        { candidate_id: "c2", verdict: "pending", highlights: [], risks: ["复核超时"], failed: true, error: "TimeoutError" },
      ],
    }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.type(screen.getByLabelText("人才搜索"), "java");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await screen.findByText("张三");
    await user.click(screen.getByLabelText("选择 张三"));
    await user.click(screen.getByRole("button", { name: "AI 复核" }));

    // 只用搜索结果里勾选的人、并且带上这次搜索的条件。
    expect(startSearchReview).toHaveBeenCalledWith({ query: "java", filters: {}, candidate_ids: ["c1"] });
    // 两列自动展开，结论落到对应候选人行。
    expect(await screen.findByRole("columnheader", { name: "亮点" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "风险点" })).toBeVisible();
    expect(await screen.findByText("支付经验匹配")).toBeVisible();
    expect(screen.getByText("未见管理经验")).toBeVisible();
    expect(screen.getByText("复核超时")).toBeVisible();
    // 复核失败的条数要看得见，不能混在「待核」里。
    const status = screen.getByRole("status", { name: "AI 复核状态" });
    expect(status.textContent).toContain("推荐 1");
    expect(status.textContent).toContain("复核失败 1");
  });
});


describe("desktop recruitment workflow", () => {
  test("refreshes the candidate list after a single resume finishes parsing", async () => {
    const api = fakeApi();
    let listCalls = 0;
    api.listCandidatesPage = async () => {
      listCalls += 1;
      const items = listCalls === 1 ? [] : [{ candidate_id: "candidate-1", revision_id: "revision-1",
        display_name: "上传后候选人", total_years: 5, highest_degree: "BACHELOR",
        location: "上海", status: "AVAILABLE", revision_status: "READY", phone: null,
        original_filename: "resume.pdf", parsed_data: null }];
      return { items, total: items.length, page: 1, page_size: 100, has_more: false };
    };
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.upload(
      screen.getByLabelText("选择简历文件"),
      new File(["resume"], "resume.pdf", { type: "application/pdf" }),
    );
    expect(await screen.findByText("上传后候选人")).toBeVisible();
  });

  test("submits explicit search filters and pages the bounded candidate list", async () => {
    const api = fakeApi();
    let receivedFilters: unknown;
    api.searchCandidates = async (_query, filters) => {
      receivedFilters = filters;
      return { items: [], degraded_reasons: [] };
    };
    const pages: number[] = [];
    api.listCandidatesPage = async (page, pageSize) => {
      pages.push(page);
      return { items: [{ candidate_id: `candidate-${page}`, revision_id: `revision-${page}`,
        display_name: `第${page}页候选人`, total_years: 5, highest_degree: "BACHELOR",
        location: "上海", status: "AVAILABLE", revision_status: "READY", phone: null,
        original_filename: "resume.pdf", parsed_data: null }], total: 150, page, page_size: pageSize,
        has_more: page < 2 };
    };
    const user = userEvent.setup();
    render(<App api={api} />);
    await screen.findByText("第1页候选人");
    await user.click(screen.getByText("精确筛选"));
    await user.type(screen.getByLabelText("最低工作年限"), "5");
    await user.type(screen.getByLabelText("现居城市"), "上海、苏州");
    await user.type(screen.getByLabelText("意向城市"), "北京");
    await user.type(screen.getByLabelText("排除技能"), "外包");
    await user.type(screen.getByLabelText("人才搜索"), "Python");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    expect(receivedFilters).toEqual({ min_years: 5, locations: ["上海", "苏州"],
      preferred_locations: ["北京"], exclude_skills: ["外包"] });
    // 搜索后只展示搜索结果（即使为空），候选人列表隐藏，不出现矛盾区域。
    expect(screen.getByText("没有符合条件的候选人")).toBeVisible();
    expect(screen.queryByText("第1页候选人")).not.toBeInTheDocument();
    // 清空搜索后恢复人才库列表，再分页。
    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(await screen.findByText("第1页候选人")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("第2页候选人")).toBeVisible();
    expect(pages[pages.length - 1]).toBe(2);
  });

  test("searches candidates and previews the resume", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.type(screen.getByPlaceholderText("搜索人才、技能、公司或自然语言"), "Python 金融");
    await user.click(screen.getByRole("button", { name: "搜索" }));

    expect(await screen.findByText("张三")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看详情" }));

    expect(await screen.findByRole("button", { name: "关闭" })).toBeVisible();
  });

  test("imports a JD and shows its revision id", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("JD 管理"));
    await user.click(screen.getByRole("button", { name: "导入 JD" }));
    await user.type(screen.getByLabelText("JD 原文"), "负责支付系统，3年 Java");
    await user.click(screen.getByRole("button", { name: "导入并解析" }));

    expect(await screen.findByText("rev-1")).toBeVisible();
  });

  test("imports a Word JD file", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("JD 管理"));
    await user.click(screen.getByRole("button", { name: "导入 JD" }));
    await user.upload(
      screen.getByLabelText("选择 JD 文件"),
      new File(["jd"], "后端工程师.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" })
    );

    expect(await screen.findByText("rev-file-1")).toBeVisible();
  });

  test("matches a JD and shows candidates in the drawer without shortlist marks", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.listJds = async () => [
      {
        jd_id: "jd-1",
        revision_id: "rev-1",
        company: "某金融",
        title: "Java 后端工程师",
        status: "READY",
        jd_status: "OPEN",
        ai_category: null,
        location: null,
        min_years: null,
        parsed_data: null,
        source_text: "Java 后端"
      }
    ];
    api.listJdsPage = async () => ({
      items: [{
        jd_id: "jd-1",
        revision_id: "rev-1",
        company: "某金融",
        title: "Java 后端工程师",
        status: "READY",
        jd_status: "OPEN",
        ai_category: null,
        location: null,
        min_years: null,
        parsed_data: null,
        source_text: "Java 后端"
      }],
      total: 1,
      page: 1,
      page_size: 10,
      has_more: false
    });
    api.matchJd = async () => ({
      run_id: "run-1",
      items: [
        {
          candidate_id: "candidate-1",
          revision_id: "revision-1",
          name: "张三",
          phone: null,
          reasons: [],
          parsed_data: null,
          content: "张三 Python 金融风控",
          score: 0.95,
          matched_channels: ["bm25", "vector"],
          total_years: 6,
          highest_degree: "MASTER",
          location: "上海",
          result_id: "result-1"
        }
      ]
    });
    render(<App api={api} />);

    await user.click(screen.getByText("JD 管理"));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    expect(await screen.findByText("张三")).toBeVisible();
    expect(screen.queryByRole("button", { name: "短名单" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "排除" })).not.toBeInTheDocument();
  });

  test("reviews a JD match run and surfaces verdicts, failures and the row cap", async () => {
    const api = fakeApi();
    const jd = {
      jd_id: "jd-1", revision_id: "rev-1", company: "某金融", title: "Java 后端工程师",
      status: "READY", jd_status: "OPEN", ai_category: null, location: null, min_years: null,
      parsed_data: null, source_text: "Java 后端",
    };
    api.listJdsPage = async () => ({ items: [jd], total: 1, page: 1, page_size: 10, has_more: false });
    api.matchJd = async () => ({
      run_id: "run-1",
      items: [
        { candidate_id: "candidate-1", revision_id: "revision-1", name: "张三", phone: null, reasons: [], parsed_data: null, content: "", score: 0.95, matched_channels: [], total_years: 6, highest_degree: "MASTER", location: "上海", result_id: "result-1" },
        { candidate_id: "candidate-2", revision_id: "revision-2", name: "李四", phone: null, reasons: [], parsed_data: null, content: "", score: 0.9, matched_channels: [], total_years: 3, highest_degree: null, location: null, result_id: "result-2" },
      ],
    });
    const startAiReview = vi.fn(async () => ({ review_id: "review-1", status: "QUEUED" }));
    api.startAiReview = startAiReview;
    api.getAiReview = vi.fn(async () => ({
      status: "SUCCESS",
      progress: 100,
      result_ref: JSON.stringify([
        { match_result_id: "result-1", candidate_id: "candidate-1", jd_revision_id: "rev-1", verdict: "recommend", reasons: ["支付经验匹配"], cautions: [] },
        { match_result_id: "result-2", candidate_id: "candidate-2", jd_revision_id: "rev-1", verdict: "pending", reasons: [], cautions: ["复核超时（单条调用超时）"], failed: true, error: "TimeoutError" },
      ]),
      error_message: null,
    }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("JD 管理"));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    await screen.findByText("张三");
    await user.click(screen.getByRole("button", { name: "AI 深度复核" }));

    expect(startAiReview).toHaveBeenCalledWith("run-1", false);
    // 结论按 result_id 落到对应候选人行。
    expect(await screen.findByText("支付经验匹配")).toBeVisible();
    // 失败条目要看得见（匹配到的每一条都会复核，不再有「未复核」汇总条）。
    expect(screen.getByText("复核失败")).toBeVisible();
    expect(screen.getByText(/（复核失败 1）/)).toBeVisible();
    // 已成功的复核可以重跑（后端换新幂等键）。
    expect(screen.getByRole("button", { name: "重新复核" })).toBeVisible();
  });

  test("loads the recruitment dashboard", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("数据看板"));
    await user.click(screen.getByRole("button", { name: "刷新看板" }));

    expect(await screen.findByText("推荐总数")).toBeVisible();
    expect(screen.getByText("offer 总数")).toBeVisible();
    expect(screen.getByText("每岗位每轮通过率")).toBeVisible();
  });

  test("creates a company in the mapping tab", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("Mapping"));
    await user.type(screen.getByLabelText("公司名称"), "字节跳动");
    await user.click(screen.getByRole("button", { name: "新建公司" }));

    expect(await screen.findByText("字节跳动")).toBeVisible();
  });

  test("searches BD leads and shows empty state", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("BD 助手"));
    await user.type(screen.getByLabelText("BD 深度检索"), "Java 工程师");
    await user.click(screen.getByRole("button", { name: "深度检索" }));

    expect(await screen.findByText("暂无线索")).toBeVisible();
  });

  test("saves an AI connection and reports it applies immediately", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("设置"));
    await user.click(screen.getByRole("button", { name: "添加 AI 服务" }));
    await user.type(screen.getByLabelText("API Key"), "sk-test-key");
    await user.click(screen.getByRole("button", { name: "检测并继续" }));
    await user.click(screen.getByRole("button", { name: "保存为主服务" }));
    expect(await screen.findByText("AI 配置已保存并生效")).toBeVisible();
  });

  test("displays a recent fallback without a blocking dialog", async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    api.getAiStatus = async () => ({
      connections: [],
      last_fallback: {
        primary_provider_id: "deepseek",
        primary_model: "deepseek-v4-flash",
        backup_provider_id: "qwen",
        backup_model: "qwen-flash",
        error_code: "E_API_RATE_LIMIT",
        occurred_at: Date.now() / 1000,
      },
    });
    render(<App api={api} />);

    await user.click(screen.getByText("设置"));
    expect(await screen.findByText("DeepSeek 当前不可用，本次已由 通义千问 完成")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});


describe("settings backup and migration", () => {
  const backupFixture = () => ({
    filename: "backup_1.sqlite3",
    path: "/tmp/backup_1.sqlite3",
    size_bytes: "123",
    created: "2026-01-01T00:00:00Z",
  });

  test("shows backup and migration sections without advanced flag", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(screen.getByText("设置"));

    expect(screen.getByRole("heading", { name: "备份与恢复" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "数据迁移" })).toBeVisible();
  });

  test("load, create and restore buttons call the correct API", async () => {
    const api = fakeApi();
    const listBackups = vi.fn(async () => [backupFixture()]);
    const createBackup = vi.fn(async () => ({ filename: "backup_1.sqlite3", path: "/tmp/backup_1.sqlite3" }));
    const restoreBackup = vi.fn(async () => ({ restored_from: "backup_1.sqlite3", safety_backup: "/tmp/safety.sqlite3" }));
    api.listBackups = listBackups;
    api.createBackup = createBackup;
    api.restoreBackup = restoreBackup;
    vi.stubGlobal("confirm", vi.fn(() => true));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));

    await user.click(screen.getByRole("button", { name: "加载备份" }));
    expect(listBackups).toHaveBeenCalled();
    await screen.findByText("backup_1.sqlite3");

    await user.click(screen.getByRole("button", { name: "立即备份" }));
    expect(createBackup).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "恢复" }));
    expect(restoreBackup).toHaveBeenCalledWith("backup_1.sqlite3");
  });

  test("asks for confirmation before restoring", async () => {
    const api = fakeApi();
    api.listBackups = async () => [backupFixture()];
    const restoreBackup = vi.fn(async () => ({ restored_from: "backup_1.sqlite3", safety_backup: "/tmp/safety.sqlite3" }));
    api.restoreBackup = restoreBackup;
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    await user.click(screen.getByRole("button", { name: "加载备份" }));
    await screen.findByText("backup_1.sqlite3");

    await user.click(screen.getByRole("button", { name: "恢复" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("确认恢复备份"));
    expect(restoreBackup).not.toHaveBeenCalled();
  });

  test("disables the restore button while a restore is running", async () => {
    const api = fakeApi();
    api.listBackups = async () => [backupFixture()];
    let resolveRestore!: (value: { restored_from: string; safety_backup: string }) => void;
    api.restoreBackup = () => new Promise((resolve) => { resolveRestore = resolve; });
    vi.stubGlobal("confirm", vi.fn(() => true));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    await user.click(screen.getByRole("button", { name: "加载备份" }));
    await screen.findByText("backup_1.sqlite3");

    fireEvent.click(screen.getByRole("button", { name: "恢复" }));
    expect(screen.getByRole("button", { name: "恢复" })).toBeDisabled();

    resolveRestore({ restored_from: "backup_1.sqlite3", safety_backup: "/tmp/safety.sqlite3" });
    await waitFor(() => expect(screen.getByRole("button", { name: "恢复" })).not.toBeDisabled());
  });

  test("shows backend error when restore fails", async () => {
    const api = fakeApi();
    api.listBackups = async () => [backupFixture()];
    api.restoreBackup = async () => { throw new Error("备份数据库完整性检查失败"); };
    vi.stubGlobal("confirm", vi.fn(() => true));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    await user.click(screen.getByRole("button", { name: "加载备份" }));
    await screen.findByText("backup_1.sqlite3");

    await user.click(screen.getByRole("button", { name: "恢复" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("备份数据库完整性检查失败");
  });

  test("shows restart prompt after a successful restore", async () => {
    const api = fakeApi();
    api.listBackups = async () => [backupFixture()];
    api.restoreBackup = async () => ({ restored_from: "backup_1.sqlite3", safety_backup: "/tmp/safety.sqlite3" });
    vi.stubGlobal("confirm", vi.fn(() => true));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    await user.click(screen.getByRole("button", { name: "加载备份" }));
    await screen.findByText("backup_1.sqlite3");

    await user.click(screen.getByRole("button", { name: "恢复" }));

    expect(await screen.findByText(/请从托盘退出后重新启动/)).toBeVisible();
  });

  test("shows migration path conflict error", async () => {
    const api = fakeApi();
    api.migrateData = async () => { throw new Error("目标目录与当前数据目录相同，请选择其他目录"); };

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("设置"));
    await user.type(screen.getByLabelText("迁移目标目录"), "C:/same-dir");
    await user.click(screen.getByRole("button", { name: "复制并校验" }));

    expect(await screen.findByText("目标目录与当前数据目录相同，请选择其他目录")).toBeVisible();
  });
});


describe("org import and binding", () => {
  test("parses and previews org import in the mapping tab", async () => {
    const api = fakeApi();
    const parseOrgImport = vi.fn(async () => ({
      draft: {
        company_name: "得物",
        departments: [{ name: "算法平台", parent_name: null, leader_name: null, team_size: null, business_direction: null }],
        employees: [{ name: "贺喜", alias: "叶程", title: "负责人", job_level: null, report_to_name: null, department_name: "算法平台", subordinate_count: null, team_size: null, remark: null }],
      },
      questions: [],
    }));
    api.parseOrgImport = parseOrgImport;

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("Mapping"));
    await user.click(screen.getByRole("button", { name: "导入组织" }));
    await user.type(screen.getByLabelText("组织文本"), "算法平台 负责人 贺喜");
    await user.click(screen.getByRole("button", { name: "解析粘贴文本" }));

    expect(parseOrgImport).toHaveBeenCalledWith("算法平台 负责人 贺喜");
    expect(await screen.findByText("解析结果（可编辑后导入）")).toBeVisible();
    expect(screen.getByLabelText("导入公司名称")).toHaveValue("得物");
    expect(screen.getByText("部门（1）")).toBeVisible();
    expect(screen.getByText("人员（1）")).toBeVisible();
    expect(screen.getByLabelText("部门0名称")).toHaveValue("算法平台");
    expect(screen.getByLabelText("人员0姓名")).toHaveValue("贺喜");
    expect(screen.getByLabelText("人员0花名")).toHaveValue("叶程");
  });
});


describe("candidate education and profile editing", () => {
  test("keeps education editor open with input on save failure", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 6,
      highest_degree: "MASTER",
      location: "上海",
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三", educations: [{ school: "北京大学" }] },
    }]);
    api.updateCandidateField = vi.fn().mockRejectedValue(new Error("保存失败"));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "北京大学" }));
    await user.clear(screen.getByLabelText("学校-0"));
    await user.type(screen.getByLabelText("学校-0"), "清华大学");
    await user.click(screen.getByRole("button", { name: "保存" }));

    // 保存失败：弹窗保留、输入保留、显示错误。
    const dialog = screen.getByRole("dialog", { name: "编辑教育经历" });
    expect(await within(dialog).findByText("保存失败")).toBeVisible();
    expect(within(dialog).getByLabelText("学校-0")).toHaveValue("清华大学");
    expect(dialog).toBeInTheDocument();
  });

  test("opens profile editor with source and stale status", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 6,
      highest_degree: "MASTER",
      location: "上海",
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三", ai_profile_summary: "资深后端工程师", ai_profile_source: "ai", ai_profile_stale: true },
    }]);

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "资深后端工程师" }));

    const dialog = screen.getByRole("dialog", { name: "编辑 AI 画像" });
    expect(within(dialog).getByText("AI 生成")).toBeVisible();
    expect(within(dialog).getByLabelText("画像正文")).toHaveValue("资深后端工程师");
    expect(within(dialog).getByRole("button", { name: "重新生成" })).toBeVisible();
  });

  test("does not render legacy parse-and-direction button for ready candidates", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 6,
      highest_degree: "MASTER",
      location: "上海",
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三" },
    }]);

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    expect(screen.queryByRole("button", { name: "解析与方向" })).not.toBeInTheDocument();
  });

  test("does not show zero results when candidates are loaded without search", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 6,
      highest_degree: "MASTER",
      location: "上海",
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三" },
    }]);

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    // 未搜索但已加载候选人：显示「共 N 位候选人」，不出现「0 条结果」。
    expect(await screen.findByText("共 1 位候选人")).toBeVisible();
    expect(screen.queryByText("0 条结果")).not.toBeInTheDocument();
    expect(screen.queryByText("0 条搜索结果")).not.toBeInTheDocument();
  });
});

describe("direction pending queue", () => {
  test("lists pending-direction candidates and corrects their direction", async () => {
    const api = fakeApi();
    api.listDirectionPending = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 5,
      highest_degree: null,
      location: null,
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三", skills: ["Python"], direction: null, career_directions: [] },
    }]);
    api.updateCandidateField = vi.fn(async (candidateId, field, value) => ({ candidate_id: candidateId, revision_id: "revision-1", field, value }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "方向待核" }));

    const panel = await screen.findByRole("region", { name: "方向待核" });
    expect(within(panel).getByText("1 人方向待核")).toBeVisible();

    // 二级分类控件：大类是 chip 按钮，选完显式「确认」再提交。
    await user.click(within(panel).getByRole("button", { name: "后端" }));
    await user.click(within(panel).getByRole("button", { name: "确认" }));
    // 普通字段编辑不带双形态载荷（第 4 个参数为空），只有画像重生成/保存才透传 points/compact。
    expect(api.updateCandidateField).toHaveBeenCalledWith(
      "candidate-1", "career_directions", ["BACKEND"], undefined);
  });
});

describe("candidate match AI review", () => {
  test("offers AI deep review from the candidate match drawer", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 5,
      highest_degree: null,
      location: null,
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三", skills: ["Python"] },
    }]);
    api.matchCandidate = vi.fn(async () => ({
      run_id: "run-1",
      items: [{
        result_id: "result-1",
        jd_id: "jd-1",
        revision_id: "rev-1",
        company: "金融公司",
        title: "后端",
        score: 0.9,
        status: "未处理" as const,
        case_id: null,
        jd_status: "OPEN",
        ai_category: null,
        parsed_data: null,
        source_text: "Java 后端",
      }],
    }));
    const startAiReview = vi.fn(async () => ({ review_id: "review-1", status: "QUEUED" }));
    api.startAiReview = startAiReview;
    api.getAiReview = vi.fn(async () => ({ status: "SUCCESS", progress: 100, result_ref: null, error_message: null }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    await user.click(await screen.findByRole("button", { name: "AI 深度复核" }));

    expect(startAiReview).toHaveBeenCalledWith("run-1", false);
  });

  test("shows per-row AI verdicts in candidate match drawer", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1",
      revision_id: "revision-1",
      display_name: "张三",
      total_years: 5,
      highest_degree: null,
      location: null,
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: "简历.pdf",
      parsed_data: { name: "张三", skills: ["Python"] },
    }]);
    api.matchCandidate = vi.fn(async () => ({
      run_id: "run-1",
      items: [
        { result_id: "result-1", jd_id: "jd-1", revision_id: "rev-1", company: "A公司", title: "后端", score: 0.9, status: "未处理" as const, case_id: null, jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: "Java 后端" },
        { result_id: "result-2", jd_id: "jd-2", revision_id: "rev-2", company: "B公司", title: "算法", score: 0.8, status: "未处理" as const, case_id: null, jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: "算法" },
      ],
    }));
    api.startAiReview = vi.fn(async () => ({ review_id: "review-1", status: "QUEUED" }));
    api.getAiReview = vi.fn(async () => ({
      status: "SUCCESS",
      progress: 100,
      result_ref: JSON.stringify([
        { match_result_id: "result-1", candidate_id: "candidate-1", jd_revision_id: "rev-1", verdict: "recommend", reasons: ["有证据"], cautions: [] },
        { match_result_id: "result-2", candidate_id: "candidate-1", jd_revision_id: "rev-2", verdict: "reject", reasons: [], cautions: ["无证据"] },
      ]),
      error_message: null,
    }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    await user.click(await screen.findByRole("button", { name: "AI 深度复核" }));

    // 两个 JD 行分别显示各自的符合点与注意点，不串项。
    expect(await screen.findByText("有证据")).toBeVisible();
    expect(screen.getByText("无证据")).toBeVisible();
  });

  test("marks failed review verdicts so they are not mistaken for plain pending", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三", total_years: 5,
      highest_degree: null, location: null, status: "AVAILABLE", revision_status: "READY",
      phone: null, original_filename: "简历.pdf", parsed_data: { name: "张三" },
    }]);
    api.matchCandidate = vi.fn(async () => ({
      run_id: "run-1",
      items: [
        { result_id: "result-1", jd_id: "jd-1", revision_id: "rev-1", company: "A公司", title: "后端", score: 0.9, status: "未处理" as const, case_id: null, jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: null },
        { result_id: "result-2", jd_id: "jd-2", revision_id: "rev-2", company: "B公司", title: "算法", score: 0.8, status: "未处理" as const, case_id: null, jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: null },
      ],
    }));
    api.startAiReview = vi.fn(async () => ({ review_id: "review-1", status: "QUEUED" }));
    api.getAiReview = vi.fn(async () => ({
      status: "SUCCESS",
      progress: 100,
      result_ref: JSON.stringify([
        { match_result_id: "result-1", candidate_id: "candidate-1", jd_revision_id: "rev-1", verdict: "recommend", reasons: ["有证据"], cautions: [] },
        { match_result_id: "result-2", candidate_id: "candidate-1", jd_revision_id: "rev-2", verdict: "pending", reasons: [], cautions: ["复核失败：TimeoutError"], failed: true, error: "TimeoutError" },
      ]),
      error_message: null,
    }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    await user.click(await screen.findByRole("button", { name: "AI 深度复核" }));

    // 失败条目仍然显示「待核」，但必须另有失败标记与计数，否则用户以为只是模型判不了。
    expect(await screen.findByText("复核失败")).toBeVisible();
    expect(screen.getByText(/（复核失败 1）/)).toBeVisible();
  });

  test("retries a cancelled review on the same run", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [{
      candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三", total_years: 5,
      highest_degree: null, location: null, status: "AVAILABLE", revision_status: "READY",
      phone: null, original_filename: "简历.pdf", parsed_data: { name: "张三" },
    }]);
    api.matchCandidate = vi.fn(async () => ({
      run_id: "run-1",
      items: [{ result_id: "result-1", jd_id: "jd-1", revision_id: "rev-1", company: "A公司", title: "后端", score: 0.9, status: "未处理" as const, case_id: null, jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: null }],
    }));
    api.startAiReview = vi.fn(async () => ({ review_id: "review-1", status: "QUEUED" }));
    // 用户看到的现象：点击后只拿到 CANCELLED，任务再也不跑。
    api.getAiReview = vi.fn(async () => ({ status: "CANCELLED", progress: 0, result_ref: null, error_message: null }));
    const controlTask = vi.fn(async (taskId: string) => ({
      id: taskId, task_type: "MATCH_REVIEW", status: "QUEUED", progress: 0, payload: {},
      result_ref: null, error_code: null, error_message: null, created_at: "", updated_at: "",
    }));
    api.controlTask = controlTask;

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(screen.getByRole("button", { name: "匹配" }));
    await user.click(await screen.findByRole("button", { name: "AI 深度复核" }));
    await user.click(await screen.findByRole("button", { name: "重试复核" }));

    expect(controlTask).toHaveBeenCalledWith("review-1", "retry");
  });

  test("shows batch match results per candidate with row-level review", async () => {
    const api = fakeApi();
    api.listCandidates = vi.fn(async () => [
      { candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三", total_years: 6,
        highest_degree: null, location: null, status: "AVAILABLE", revision_status: "READY",
        phone: null, original_filename: "a.pdf", parsed_data: { name: "张三" } },
      { candidate_id: "candidate-2", revision_id: "revision-2", display_name: "李四", total_years: 3,
        highest_degree: null, location: null, status: "AVAILABLE", revision_status: "READY",
        phone: null, original_filename: "b.pdf", parsed_data: { name: "李四" } },
    ]);
    const bulkMatch = vi.fn(async () => ({
      results: [
        { candidate_id: "candidate-1", name: "张三", run_id: "run-1", matched_jobs: 1, error: null,
          items: [{ result_id: "result-1", jd_id: "jd-1", revision_id: "jdrev-1", resume_revision_id: "revision-1",
            company: "A公司", title: "后端", score: 0.9, status: "未处理" as const, case_id: null,
            jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: null }] },
        { candidate_id: "candidate-2", name: "李四", run_id: "run-2", matched_jobs: 1, error: null,
          items: [{ result_id: "result-2", jd_id: "jd-2", revision_id: "jdrev-2", resume_revision_id: "revision-2",
            company: "B公司", title: "算法", score: 0.8, status: "未处理" as const, case_id: null,
            jd_status: "OPEN", ai_category: null, parsed_data: null, source_text: null }] },
      ],
      // 岗位详情按 JD 修订去重返回，前端合并回行内后画像列才有内容。
      jd_details: [
        { revision_id: "jdrev-1", jd_id: "jd-1", company: "A公司", title: "后端", jd_status: "OPEN",
          ai_category: null, parsed_data: { candidate_profile: "5 年 Java" }, source_text: null },
        { revision_id: "jdrev-2", jd_id: "jd-2", company: "B公司", title: "算法", jd_status: "OPEN",
          ai_category: null, parsed_data: { candidate_profile: "3 年算法" }, source_text: null },
      ],
    }));
    api.bulkMatchCandidates = bulkMatch;
    const startAiReview = vi.fn(async () => ({ review_id: "review-2", status: "QUEUED" }));
    api.startAiReview = startAiReview;
    api.getAiReview = vi.fn(async () => ({
      status: "SUCCESS",
      progress: 100,
      result_ref: JSON.stringify([
        { match_result_id: "result-2", candidate_id: "candidate-2", jd_revision_id: "jdrev-2", verdict: "recommend", reasons: ["命中算法"], cautions: [] },
      ]),
      error_message: null,
    }));

    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByRole("button", { name: "显示候选人" }));
    await user.click(await screen.findByLabelText("全选当前页"));
    await user.click(within(screen.getByRole("toolbar", { name: "批量操作" })).getByRole("button", { name: "匹配" }));

    expect(bulkMatch).toHaveBeenCalledWith(["candidate-1", "candidate-2"]);
    const drawer = await screen.findByRole("dialog", { name: "匹配结果" });
    expect(within(drawer).getByText("批量匹配（2 人）")).toBeVisible();
    // 两位候选人各一行，岗位详情来自去重表。
    expect(within(drawer).getByText("后端")).toBeVisible();
    expect(within(drawer).getByText("算法")).toBeVisible();
    expect(within(drawer).getByText("5 年 Java")).toBeVisible();

    // 按候选人筛选：只看李四时张三四行消失。
    await user.selectOptions(within(drawer).getByLabelText("按候选人筛选匹配结果"), "candidate-2");
    expect(within(drawer).queryByText("后端")).not.toBeInTheDocument();
    expect(within(drawer).getByText("算法")).toBeVisible();

    // 复核按钮按行触发，用的是该行候选人自己的 run。
    await user.click(within(drawer).getByRole("button", { name: "AI 复核" }));
    expect(startAiReview).toHaveBeenCalledWith("run-2", false);
    expect(await within(drawer).findByText("命中算法")).toBeVisible();
  });
});


describe("JD candidate profile saving", () => {
  function jdApi(profile: string) {
    const api = fakeApi();
    api.listJdsPage = async () => ({
      items: [{
        jd_id: "jd-1", revision_id: "rev-1", company: "某金融", title: "Java 后端工程师",
        status: "READY", jd_status: "OPEN", ai_category: null, location: null, min_years: null,
        parsed_data: { candidate_profile: profile }, source_text: "Java 后端",
      }],
      total: 1, page: 1, page_size: 10, has_more: false,
    });
    return api;
  }

  async function openJdProfile(api: RecruitmentApi, profile: string) {
    const user = userEvent.setup();
    render(<App api={api} />);
    await user.click(screen.getByText("JD 管理"));
    await user.click(await screen.findByRole("button", { name: profile }));
    return user;
  }

  test("saves the profile directly without re-parsing constraints when unchanged", async () => {
    const api = jdApi("旧画像");
    const updateJdParsed = vi.fn(async () => ({ jd_id: "jd-1", revision_id: "rev-1", field: "parsed_data", value: {} }));
    api.updateJdParsed = updateJdParsed;
    const parseJdConstraints = vi.fn(async () => ({ constraints: [] }));
    api.parseJdConstraints = parseJdConstraints;

    const user = await openJdProfile(api, "旧画像");
    await user.click(screen.getByRole("button", { name: "保存画像" }));

    // 画像没变：直接落库，不动硬条件（不带 exact_constraints，也就不会覆盖已有约束）。
    await waitFor(() => expect(updateJdParsed).toHaveBeenCalledWith("jd-1", { candidate_profile: "旧画像" }));
    expect(parseJdConstraints).not.toHaveBeenCalled();
    // 不再弹画像弹窗。
    expect(screen.queryByRole("dialog", { name: /画像/ })).not.toBeInTheDocument();
  });

  test("re-parses constraints with AI when the profile text changed", async () => {
    const api = jdApi("旧画像");
    const updateJdParsed = vi.fn(async () => ({ jd_id: "jd-1", revision_id: "rev-1", field: "parsed_data", value: {} }));
    api.updateJdParsed = updateJdParsed;
    const constraints = [{ kind: "skill", operator: "OR", alternatives: ["Java"], strength: "MUST", source: "inferred", source_text: "必须熟悉 Java" }];
    const parseJdConstraints = vi.fn(async () => ({ constraints }));
    api.parseJdConstraints = parseJdConstraints;

    const user = await openJdProfile(api, "旧画像");
    const textarea = await screen.findByLabelText("候选人画像要求");
    await user.clear(textarea);
    await user.type(textarea, "必须熟悉 Java");
    await user.click(screen.getByRole("button", { name: "保存画像" }));

    // 画像一变就重解析硬条件并覆盖，且与画像同一次提交。
    await waitFor(() => expect(parseJdConstraints).toHaveBeenCalledWith("必须熟悉 Java"));
    expect(updateJdParsed).toHaveBeenCalledWith("jd-1", {
      candidate_profile: "必须熟悉 Java",
      exact_constraints: constraints,
    });
  });
});
