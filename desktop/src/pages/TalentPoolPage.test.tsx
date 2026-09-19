import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { CandidateListItem, CandidateSearchItem } from "../App";
import type { CandidateColumnKey } from "../components/CandidateTable";
import { TalentPoolPage, type TalentPoolPageProps } from "./TalentPoolPage";


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
      phone: "", gender: "", name: "", company: "", title: "", school: "",
      careerDirections: [], careerSpecializations: [], businessDirections: [], schoolRegion: "",
    },
    onSearchFilterChange: vi.fn(),
    batchProgress: null,
    batchTaskIds: [],
    batchTimedOut: false,
    hasSearched: false,
    results: [] as CandidateSearchItem[],
    searchConditions: [],
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
    onEditEducation: vi.fn(),
    onEditProfile: vi.fn(),
    onMatch: vi.fn(),
    onPreview: vi.fn(),
    onDownload: vi.fn(),
    onCreateCase: vi.fn(),
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


test("vector mode renders the semantic-rewrite toggle and hides the keyword-logic selector", () => {
  render(<TalentPoolPage {...makeProps({ searchMode: "vector" })} />);
  expect(screen.getByLabelText("AI 语义改写")).toBeInTheDocument();
  expect(screen.getByText("可能增加搜索时间")).toBeInTheDocument();
  expect(screen.queryByLabelText("关键词逻辑")).not.toBeInTheDocument();
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
  expect(screen.getByText(/现居 上海/)).toBeInTheDocument();
  expect(screen.getByText("推断")).toBeInTheDocument();
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
