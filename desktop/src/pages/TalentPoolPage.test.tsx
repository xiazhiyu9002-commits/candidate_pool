import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { CandidateListItem, CandidateSearchItem } from "../App";
import type { CandidateColumnKey } from "../components/CandidateTable";
import { CANDIDATE_COLUMNS_DEFAULT, CANDIDATE_COLUMNS_ORDER_DEFAULT } from "../components/CandidateTable";
import { TalentPoolPage, type SearchFilterDraft, type TalentPoolPageProps } from "./TalentPoolPage";


/**
 * 取某个开关旁边的 ⓘ 说明入口。
 *
 * ⓘ 的可访问名称是固定的「查看说明」（三个都一样），所以按所属分组定位，
 * 而不是按名称——按名称会拿到第一个 ⓘ。
 */
function infoTipFor(toggleLabel: string): HTMLElement {
  const group = screen.getByLabelText(toggleLabel).closest(".rewrite-toggle-group");
  const tip = group?.querySelector(".info-tip");
  if (!tip) throw new Error(`未找到「${toggleLabel}」的说明入口`);
  return tip as HTMLElement;
}


function makeProps(overrides: Partial<TalentPoolPageProps> = {}): TalentPoolPageProps {
  return {
    query: "",
    onQueryChange: vi.fn(),
    searchMode: "hybrid",
    onSearchModeChange: vi.fn(),
    searching: false,
    searchFilterDraft: {
      minYears: "", maxYears: "", minAge: "", maxAge: "", degree: "", locations: "",
      preferredLocations: "", schoolLevel: "", maxQsRank: "", excludeSkills: "",
      phone: "", gender: "", name: "", communicationNote: "", company: "", title: "", school: "",
      careerDirections: [], careerSpecializations: [], businessDirections: [], schoolRegion: "",
    },
    onSearchFilterChange: vi.fn(),
    batchProgress: null,
    batchTaskIds: [],
    batchTimedOut: false,
    hasSearched: false,
    results: [] as CandidateSearchItem[],
    searchConditions: [],
    relaxedSearchFields: [],
    searchPlanEcho: null,
    removedConditions: [],
    onRemoveSearchCondition: vi.fn(),
    onEditSearchCondition: vi.fn(),
    onRestoreSearchCondition: vi.fn(),
    onDropInferredConditions: vi.fn(),
    candidates: [] as CandidateListItem[],
    candidatePage: 1,
    candidateTotal: 0,
    candidateTotalPages: 1,
    candidatePageSize: 20,
    onCandidatePageSizeChange: vi.fn(),
    visibleColumns: {} as Record<CandidateColumnKey, boolean>,
    columnOrder: [],
    columnsMenuOpen: false,
    candidateListRef: { current: null },
    onSubmitSearch: vi.fn(),
    onContinueBatchPolling: vi.fn(),
    onLoadCandidates: vi.fn(),
    onCloseSearchResults: vi.fn(),
    onResetSearch: vi.fn(),
    onToggleColumn: vi.fn(),
    onMoveColumn: vi.fn(),
    onColumnsMenuOpenChange: vi.fn(),
    onScrollTop: vi.fn(),
    onUpdateField: vi.fn(async () => {}),
    onSaveCommunicationNote: vi.fn(async () => {}),
    onEditEducation: vi.fn(),
    onEditProfile: vi.fn(),
    onMatch: vi.fn(),
    onPreview: vi.fn(),
    onDownload: vi.fn(),
    onCreateCase: vi.fn(),
    onCreateReminder: vi.fn(),
    onReparse: vi.fn(async () => {}),
    onForceReparse: vi.fn(async () => {}),
    onOpenReview: vi.fn(),
    onOpenParsed: vi.fn(),
    onDelete: vi.fn(),
    selectedCandidateIds: new Set<string>(),
    bulkBusy: false,
    onToggleCandidateSelect: vi.fn(),
    onToggleCandidateSelectAll: vi.fn(),
    onBulkDelete: vi.fn(),
    onBulkReparse: vi.fn(),
    onBulkForceOcr: vi.fn(),
    onBulkMatch: vi.fn(),
    onBulkDownload: vi.fn(),
    searchReview: null,
    selectedSearchCount: 0,
    onBulkSearchReview: vi.fn(),
    onCancelSearchReview: vi.fn(),
    onRetrySearchReview: vi.fn(),
    keywordOperator: "smart",
    onKeywordOperatorChange: vi.fn(),
    rewriteEnabled: false,
    onRewriteEnabledChange: vi.fn(),
    searchBody: false,
    onSearchBodyChange: vi.fn(),
    parseEnabled: false,
    onParseEnabledChange: vi.fn(),
    ...overrides,
  };
}


test("keyword mode renders the three keyword-logic options and hides the rewrite toggle", () => {
  render(<TalentPoolPage {...makeProps({ searchMode: "keyword" })} />);
  const select = screen.getByLabelText("关键词逻辑");
  expect(within(select).getByRole("option", { name: "智能排序" })).toBeInTheDocument();
  expect(within(select).getByRole("option", { name: "同时满足" })).toBeInTheDocument();
  expect(within(select).getByRole("option", { name: "满足任一" })).toBeInTheDocument();
  expect(screen.queryByLabelText("AI 语义改写")).not.toBeInTheDocument();
});


test("hybrid mode renders the body toggle alongside AI rewrite", () => {
  // 「检索经历正文」在关键词与混合模式下含义一致；混合模式还要有 AI 语义改写。
  render(<TalentPoolPage {...makeProps({ searchMode: "hybrid" })} />);
  expect(screen.getByLabelText("检索工作与项目经历正文")).toBeInTheDocument();
  expect(screen.getByLabelText("AI 语义改写")).toBeInTheDocument();
  expect(screen.queryByLabelText("关键词逻辑")).not.toBeInTheDocument();
});


test("vector mode hides the body toggle because there is no FTS channel", () => {
  render(<TalentPoolPage {...makeProps({ searchMode: "vector" })} />);
  expect(screen.queryByLabelText("检索工作与项目经历正文")).not.toBeInTheDocument();
  expect(screen.getByLabelText("AI 语义改写")).toBeInTheDocument();
});


test("vector mode renders the semantic-rewrite toggle and hides the keyword-logic selector", () => {
  render(<TalentPoolPage {...makeProps({ searchMode: "vector" })} />);
  expect(screen.getByLabelText("AI 语义改写")).toBeInTheDocument();
  // 说明文字改为「悬停出现」：默认只占一个 ⓘ，不占横向空间。
  expect(screen.queryByText("可能增加搜索时间")).not.toBeInTheDocument();
  expect(infoTipFor("AI 语义改写")).toBeInTheDocument();
  expect(screen.queryByLabelText("关键词逻辑")).not.toBeInTheDocument();
});


test("explanation text only appears while hovering the info icon", async () => {
  const user = userEvent.setup();
  render(<TalentPoolPage {...makeProps({ searchMode: "hybrid" })} />);

  // 三个开关的说明都不常显。
  expect(screen.queryByText("工作职责 + 项目描述（仅影响关键词通道）")).not.toBeInTheDocument();
  expect(screen.queryByText("可能增加搜索时间")).not.toBeInTheDocument();
  expect(screen.queryByText("拆出硬条件 + 词条 + 语义查询")).not.toBeInTheDocument();

  await user.hover(infoTipFor("AI 智能解析"));
  expect(screen.getByRole("tooltip")).toHaveTextContent("拆出硬条件 + 词条 + 语义查询");

  await user.unhover(infoTipFor("AI 智能解析"));
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});


test("info icon is keyboard reachable and shows the explanation on focus", async () => {
  const user = userEvent.setup();
  render(<TalentPoolPage {...makeProps({ searchMode: "hybrid" })} />);

  // 键盘/触屏用户也要能读到说明，不能只依赖鼠标悬停。
  const tip = infoTipFor("AI 智能解析");
  let guard = 0;
  while (document.activeElement !== tip && guard < 30) {
    await user.tab();
    guard += 1;
  }
  expect(document.activeElement).toBe(tip);
  expect(screen.getByRole("tooltip")).toHaveTextContent("拆出硬条件 + 词条 + 语义查询");
});


test("hybrid mode renders the semantic-rewrite toggle and hides the keyword-logic selector", () => {
  render(<TalentPoolPage {...makeProps({ searchMode: "hybrid" })} />);
  expect(screen.getByLabelText("AI 语义改写")).toBeInTheDocument();
  expect(screen.queryByLabelText("关键词逻辑")).not.toBeInTheDocument();
});


test("renders parsed hard conditions as visible tags", () => {
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchConditions: [
          { field: "location", value: "北京", confidence: "explicit" },
          { field: "min_years", value: "5年", confidence: "explicit" },
        ],
      })}
    />
  );
  expect(screen.getByLabelText("生效硬条件")).toBeInTheDocument();
  expect(screen.getByText(/现居 北京/)).toBeInTheDocument();
  expect(screen.getByText(/年限≥ 5年/)).toBeInTheDocument();
});


test("school entity produces no residence location tag", () => {
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        // 后端对「北京大学 后端」不会产生 location 条件，条件列表为空。
        searchConditions: [],
      })}
    />
  );
  expect(screen.queryByLabelText("生效硬条件")).not.toBeInTheDocument();
  expect(screen.queryByText(/现居 北京/)).not.toBeInTheDocument();
});


test("inferred condition is marked as inferred", () => {
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchConditions: [{ field: "location", value: "上海", confidence: "inferred" }],
      })}
    />
  );
  // 空结果自诊断会重复提到推断条件，所以断言收敛到「生效硬条件」那一行。
  const row = screen.getByLabelText("生效硬条件");
  expect(within(row).getByText(/现居 上海/)).toBeInTheDocument();
  expect(within(row).getByText("推断")).toBeInTheDocument();
});


test("AI 智能解析 toggle is available in all three modes", () => {
  for (const searchMode of ["keyword", "vector", "hybrid"] as const) {
    const { unmount } = render(<TalentPoolPage {...makeProps({ searchMode })} />);
    expect(screen.getByLabelText("AI 智能解析")).not.toBeChecked();
    unmount();
  }
});


test("effective conditions show the merged source and can be edited or removed", async () => {
  const user = userEvent.setup();
  const onRemoveSearchCondition = vi.fn();
  const onEditSearchCondition = vi.fn();
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchConditions: [
          { field: "locations", value: "北京", confidence: "inferred", source: "llm" },
          { field: "min_years", value: "8.0", confidence: "explicit", source: "panel" },
        ],
        onRemoveSearchCondition,
        onEditSearchCondition,
      })}
    />
  );
  // 合并后的生效条件按来源标注，方便区分面板手填与解析推断。
  const row = screen.getByLabelText("生效硬条件");
  expect(within(row).getByText(/现居 北京/)).toBeInTheDocument();
  expect(within(row).getByText("AI 解析")).toBeInTheDocument();
  expect(within(row).getByText("面板手填")).toBeInTheDocument();
  expect(within(row).getByText("年限≥ 8.0")).toBeInTheDocument();

  await user.click(screen.getByLabelText("移除条件 现居 北京"));
  expect(onRemoveSearchCondition).toHaveBeenCalledWith("locations", "北京");

  await user.click(screen.getByLabelText("修改条件 年限≥ 8.0"));
  expect(onEditSearchCondition).toHaveBeenCalledWith("min_years", "8.0");
  // 「改」同时打开精确筛选面板，方便就地调整。
  expect(screen.getByLabelText("最低工作年限")).toBeInTheDocument();
});


test("removed conditions stay visible and can be restored", async () => {
  const user = userEvent.setup();
  const onRestoreSearchCondition = vi.fn();
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchConditions: [],
        removedConditions: [{ field: "locations", value: "上海", confidence: "inferred" }],
        onRestoreSearchCondition,
      })}
    />
  );
  await user.click(screen.getByLabelText("恢复条件 现居 上海"));
  expect(onRestoreSearchCondition).toHaveBeenCalledWith("locations");
});


test("AI parse echo renders keywords, semantic query and unparsed fragments", () => {
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchPlanEcho: {
          source: "mixed",
          keywordTerms: "后端 服务端",
          semanticQuery: "负责交易系统的后端服务开发",
          unparsedTerms: ["抗压", "稳定"],
        },
      })}
    />
  );
  const echo = screen.getByLabelText("AI 解析回显");
  expect(within(echo).getByText("AI + 规则")).toBeInTheDocument();
  expect(within(echo).getByText("后端 服务端")).toBeInTheDocument();
  expect(within(echo).getByText("负责交易系统的后端服务开发")).toBeInTheDocument();
  expect(within(echo).getByText("抗压、稳定")).toBeInTheDocument();
});


test("AI parse echo stays hidden when parsing was not used", () => {
  render(<TalentPoolPage {...makeProps({ hasSearched: true })} />);
  expect(screen.queryByLabelText("AI 解析回显")).not.toBeInTheDocument();
});


test("empty result with inferred conditions offers a one-click re-search", async () => {
  const user = userEvent.setup();
  const onDropInferredConditions = vi.fn();
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        results: [],
        searchConditions: [{ field: "locations", value: "上海", confidence: "inferred", source: "llm" }],
        onDropInferredConditions,
      })}
    />
  );
  expect(screen.getByText("没有符合条件的候选人")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "去掉推断条件重搜" }));
  expect(onDropInferredConditions).toHaveBeenCalledTimes(1);
});


test("empty result without inferred conditions shows no re-search hint", () => {
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        results: [],
        // 面板手填的条件不参与「可能是解析误判」的判断。
        searchConditions: [{ field: "min_years", value: "8.0", confidence: "explicit", source: "panel" }],
      })}
    />
  );
  expect(screen.queryByRole("button", { name: "去掉推断条件重搜" })).not.toBeInTheDocument();
});


test("hard-filter-degraded condition is marked as ranking-only", () => {
  // 硬筛无匹配 → 后端把它降级为软排：条件仍在（仍是排序信号），但必须标出来，
  // 否则用户会以为结果里每个人都满足这条条件。
  render(
    <TalentPoolPage
      {...makeProps({
        hasSearched: true,
        searchConditions: [
          { field: "company", value: "并不存在的公司", confidence: "inferred", source: "llm" },
          { field: "min_years", value: "5.0", confidence: "inferred", source: "llm" },
        ],
        relaxedSearchFields: ["company"],
      })}
    />
  );
  expect(screen.getByText("已退化为排序")).toBeInTheDocument();
  // 只有被降级的那条带标记，未降级的条件不能被误标。
  expect(screen.getAllByText("已退化为排序")).toHaveLength(1);
});


async function openFilters(overrides: Partial<TalentPoolPageProps> = {}) {
  const user = userEvent.setup();
  render(<TalentPoolPage {...makeProps(overrides)} />);
  await user.click(screen.getByRole("button", { name: "精确筛选" }));
  return user;
}


function makeSearchItem(index: number): CandidateSearchItem {
  return {
    candidate_id: `candidate-${index}`,
    revision_id: `revision-${index}`,
    name: `候选人${index}`,
    phone: null,
    reasons: [],
    parsed_data: null,
    content: "",
    score: 1,
    matched_channels: [],
    total_years: null,
    highest_degree: null,
    location: null,
  };
}


test("批量工具栏：重新解析与强制 OCR 分别入队，语义互不替代", async () => {
  const onBulkReparse = vi.fn();
  const onBulkForceOcr = vi.fn();
  const user = userEvent.setup();
  render(<TalentPoolPage {...makeProps({
    hasSearched: true,
    results: [makeSearchItem(0), makeSearchItem(1)],
    selectedCandidateIds: new Set(["candidate-0"]),
    onBulkReparse,
    onBulkForceOcr,
  })} />);

  const toolbar = screen.getByRole("toolbar", { name: "批量操作" });
  await user.click(within(toolbar).getByRole("button", { name: "重新解析" }));
  expect(onBulkReparse).toHaveBeenCalledWith(["revision-0"]);
  expect(onBulkForceOcr).not.toHaveBeenCalled();

  await user.click(within(toolbar).getByRole("button", { name: "强制 OCR" }));
  expect(onBulkForceOcr).toHaveBeenCalledWith(["revision-0"]);
});


test("搜索结果每页条数可切换：缩小之外还能一页装更多人", async () => {
  const results = Array.from({ length: 30 }, (_, i) => makeSearchItem(i));
  const user = userEvent.setup();
  const { container } = render(
    <TalentPoolPage {...makeProps({ hasSearched: true, results })} />
  );

  // 默认每页 20 条：第 21 条必须翻页才看得到。
  expect(container.querySelectorAll("tbody tr")).toHaveLength(20);
  expect(screen.getByText(/共 30 条/)).toBeInTheDocument();

  await user.selectOptions(screen.getByLabelText("搜索结果每页条数"), "50");
  expect(container.querySelectorAll("tbody tr")).toHaveLength(30);
});


test("人才库每页条数变化会上报给父级（后端分页）", async () => {
  const onCandidatePageSizeChange = vi.fn();
  const user = userEvent.setup();
  render(
    <TalentPoolPage
      {...makeProps({
        candidates: [{
          candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三",
          total_years: null, highest_degree: null, location: null, status: "AVAILABLE",
          revision_status: "READY", phone: null, original_filename: null, parsed_data: null,
        }],
        candidateTotal: 1,
        onCandidatePageSizeChange,
      })}
    />
  );

  await user.selectOptions(screen.getByLabelText("人才库每页条数"), "100");
  expect(onCandidatePageSizeChange).toHaveBeenCalledWith(100);
});


test("AI 画像列下半部分记录沟通记录，且不走 parsed_data 更新", async () => {
  // 沟通记录存的是「性格偏内向」这类主观判断，必须与画像分区：写 parsed_data 会
  // 触发画像过期判定与索引重建，等于把这些判断喂进向量检索。
  const onSaveCommunicationNote = vi.fn(async () => {});
  const onUpdateField = vi.fn(async () => {});
  const user = userEvent.setup();
  render(
    <TalentPoolPage
      {...makeProps({
        candidates: [{
          candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三",
          total_years: null, highest_degree: null, location: null, status: "AVAILABLE",
          revision_status: "READY", phone: null, original_filename: null, parsed_data: null,
          communication_note: "性格偏内向",
        }],
        candidateTotal: 1,
        onSaveCommunicationNote,
        onUpdateField,
        // 测试默认把列全关掉；这条用例要断言单元格内容，得打开默认列。
        visibleColumns: CANDIDATE_COLUMNS_DEFAULT,
        columnOrder: CANDIDATE_COLUMNS_ORDER_DEFAULT,
      })}
    />
  );

  // 已有记录显示在画像下方（与画像同格上下分栏）。
  expect(screen.getByText("性格偏内向")).toBeInTheDocument();

  await user.dblClick(screen.getByTitle(/双击记录沟通信息/));
  const editor = screen.getByLabelText("沟通记录");
  await user.clear(editor);
  await user.type(editor, "沟通要多推一把");
  await user.tab(); // 失焦即保存

  await waitFor(() =>
    expect(onSaveCommunicationNote).toHaveBeenCalledWith("candidate-1", "沟通要多推一把")
  );
  expect(onUpdateField).not.toHaveBeenCalled();
});


test("「建提醒」收在行内「更多操作」菜单里，与解析表、下载同处一处", async () => {
  // 行内一排已经有「匹配 / 查看详情 / 建流程」，再塞一个会把操作列挤爆，
  // 所以建提醒与解析表、下载一起放进「更多操作」。
  const onCreateReminder = vi.fn();
  const user = userEvent.setup();
  render(
    <TalentPoolPage
      {...makeProps({
        candidates: [{
          candidate_id: "candidate-1", revision_id: "revision-1", display_name: "张三",
          total_years: null, highest_degree: null, location: null, status: "AVAILABLE",
          revision_status: "READY", phone: null, original_filename: null, parsed_data: null,
          communication_note: null,
        }],
        candidateTotal: 1,
        onCreateReminder,
      })}
    />
  );

  expect(screen.queryByRole("button", { name: "建提醒" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "建提醒" })).toBeNull();

  await user.click(screen.getByLabelText("更多操作"));

  expect(screen.getByRole("menuitem", { name: "解析表" })).toBeInTheDocument();
  await user.click(screen.getByRole("menuitem", { name: "建提醒" }));
  expect(onCreateReminder).toHaveBeenCalledWith("candidate-1", "张三");
});


test("职业方向是下拉框：收起时不渲染任何选项", async () => {
  await openFilters();
  const trigger = screen.getByRole("button", { name: "职业方向" });

  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(trigger).toHaveTextContent("全部");
  expect(screen.queryByRole("button", { name: "后端" })).not.toBeInTheDocument();
});


test("展开后左列是大类，点大类才在右侧弹出它的细分", async () => {
  const user = await openFilters();

  await user.click(screen.getByRole("button", { name: "职业方向" }));
  const panel = screen.getByRole("group", { name: "职业方向选项" });
  for (const label of ["后端", "前端", "算法", "数据", "运维", "测试", "产品", "管理", "其他"]) {
    expect(within(panel).getByRole("button", { name: label })).toBeInTheDocument();
  }
  // 还没点大类：没有二级。
  expect(within(panel).queryByRole("button", { name: "服务端架构" })).not.toBeInTheDocument();

  await user.click(within(panel).getByRole("button", { name: "后端" }));
  // 点了「后端」，它的细分在面板里出现，且只列它自己的。
  expect(within(panel).getByRole("button", { name: "服务端架构" })).toBeInTheDocument();
  expect(within(panel).getByRole("button", { name: "全栈交付" })).toBeInTheDocument();
  expect(within(panel).getByRole("button", { name: "后端（不限细分）" })).toBeInTheDocument();
  expect(within(panel).queryByRole("button", { name: "Web 前端" })).not.toBeInTheDocument();
  // 选一级不收起，方便接着挑细分。
  expect(screen.getByRole("button", { name: "职业方向" })).toHaveAttribute("aria-expanded", "true");
});


test("无细分的大类不弹出二级列", async () => {
  const user = await openFilters();
  await user.click(screen.getByRole("button", { name: "职业方向" }));
  const panel = screen.getByRole("group", { name: "职业方向选项" });

  await user.click(within(panel).getByRole("button", { name: "其他" }));
  expect(within(panel).queryByRole("button", { name: /不限细分/ })).not.toBeInTheDocument();
});


test("展开时左列高亮对齐已选大类，点别的一级清空细分", async () => {
  const onSearchFilterChange = vi.fn();
  const user = await openFilters({
    onSearchFilterChange,
    searchFilterDraft: { ...makeProps().searchFilterDraft, careerDirections: ["BACKEND"] },
  });
  await user.click(screen.getByRole("button", { name: "职业方向" }));
  const panel = screen.getByRole("group", { name: "职业方向选项" });

  // 左列高亮已选大类，右列直接给出它的细分且「不限细分」为当前值。
  expect(within(panel).getByRole("button", { name: "后端" })).toHaveAttribute("aria-pressed", "true");
  expect(within(panel).getByRole("button", { name: "后端（不限细分）" })).toHaveAttribute("aria-pressed", "true");

  await user.click(within(panel).getByRole("button", { name: "数据" }));
  expect(onSearchFilterChange).toHaveBeenLastCalledWith(expect.objectContaining({
    careerDirections: ["DATA"],
    careerSpecializations: [],
  }));
});


test("点二级细分写入细分并收起面板", async () => {
  const onSearchFilterChange = vi.fn();
  const user = await openFilters({
    onSearchFilterChange,
    searchFilterDraft: { ...makeProps().searchFilterDraft, careerDirections: ["BACKEND"] },
  });
  await user.click(screen.getByRole("button", { name: "职业方向" }));
  await user.click(screen.getByRole("button", { name: "服务端架构" }));

  // 保留父级大类，写入细分。
  expect(onSearchFilterChange).toHaveBeenLastCalledWith(expect.objectContaining({
    careerDirections: ["BACKEND"],
    careerSpecializations: ["BACKEND_SERVICE"],
  }));
  expect(screen.queryByRole("group", { name: "职业方向选项" })).not.toBeInTheDocument();
});


test("点左列「全部」清空两级并收起", async () => {
  const onSearchFilterChange = vi.fn();
  const user = await openFilters({
    onSearchFilterChange,
    searchFilterDraft: {
      ...makeProps().searchFilterDraft,
      careerDirections: ["BACKEND"],
      careerSpecializations: ["BACKEND_SERVICE"],
    },
  });
  await user.click(screen.getByRole("button", { name: "职业方向" }));
  await user.click(screen.getByRole("button", { name: "全部职业方向" }));

  expect(onSearchFilterChange).toHaveBeenLastCalledWith(expect.objectContaining({
    careerDirections: [],
    careerSpecializations: [],
  }));
  expect(screen.queryByRole("group", { name: "职业方向选项" })).not.toBeInTheDocument();
});


test("业务方向下拉发单值业务条件", async () => {
  const onSearchFilterChange = vi.fn();
  const user = await openFilters({ onSearchFilterChange });

  await user.selectOptions(screen.getByLabelText("业务方向"), "MARKETING");
  expect(onSearchFilterChange).toHaveBeenLastCalledWith(expect.objectContaining({
    businessDirections: ["MARKETING"],
  }));
});


test("精确筛选：沟通文本写入草稿并随提交下发", async () => {
  // 沟通文本是候选人级备注：面板把它写进 communicationNote，父级（App）据此放进
  // 请求体的 filters.communication_note，后端按子串在 SQLite 层过滤。
  // 草稿由父级持有，所以这里用一个最小状态容器模拟 App，否则受控输入会被打回空值。
  const onSubmitSearch = vi.fn();
  function Harness() {
    const [draft, setDraft] = useState<SearchFilterDraft>(makeProps().searchFilterDraft);
    return (
      <TalentPoolPage
        {...makeProps({
          searchFilterDraft: draft,
          onSearchFilterChange: setDraft,
          onSubmitSearch,
        })}
      />
    );
  }
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "精确筛选" }));

  const input = screen.getByLabelText("沟通文本");
  await user.type(input, "沟通主动");
  expect(input).toHaveValue("沟通主动");

  await user.click(screen.getByRole("button", { name: "搜索" }));
  expect(onSubmitSearch).toHaveBeenCalledTimes(1);
});
