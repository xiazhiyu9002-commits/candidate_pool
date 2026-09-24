import { useEffect, useState, type RefObject } from "react";
import type {
  CandidateListItem, CandidateSearchItem, ParsedEducationData, ParsedResumeData,
  SearchConditionView, SearchPlanEcho, SearchReviewState,
} from "../App";
import type { CandidateKeywordOperator } from "../api/client";
import { CANDIDATE_COLUMNS, CandidateTable, type CandidateColumnKey } from "../components/CandidateTable";
import { CareerDirectionFilter } from "../components/DirectionPicker";
import { Button, InfoTip } from "../components/ui";
import { BUSINESS_DIRECTION_OPTIONS } from "../constants/directions";

export interface SearchFilterDraft {
  minYears: string;
  maxYears: string;
  minAge: string;
  maxAge: string;
  degree: string;
  locations: string;
  preferredLocations: string;
  schoolLevel: string;
  maxQsRank: string;
  excludeSkills: string;
  phone: string;
  gender: string;
  name: string;
  /** 沟通文本：按子串匹配候选人沟通记录（备注不进索引，由后端在 SQLite 层过滤）。 */
  communicationNote: string;
  company: string;
  title: string;
  school: string;
  /** 职业方向大类（多选，OR）。 */
  careerDirections: string[];
  /** 职业方向细分（多选，OR；按已选大类联动）。 */
  careerSpecializations: string[];
  /** 业务方向（多选，OR）。 */
  businessDirections: string[];
  schoolRegion: string;
}

/** 筛选草稿的空白初值（清除筛选与「回首页」共用，避免两处漂移）。 */
export const EMPTY_SEARCH_FILTER_DRAFT: SearchFilterDraft = {
  minYears: "", maxYears: "", minAge: "", maxAge: "", degree: "",
  locations: "", preferredLocations: "", schoolLevel: "", maxQsRank: "", excludeSkills: "",
  phone: "", gender: "", name: "", communicationNote: "", company: "", title: "", school: "",
  careerDirections: [], careerSpecializations: [], businessDirections: [],
  schoolRegion: "",
};

// 候选人表格缩放：缩小后单屏可容纳更多候选人，放大便于阅读。
const TABLE_ZOOM_STORAGE_KEY = "talentPool.tableZoom";
const TABLE_ZOOM_MIN = 0.5;
const TABLE_ZOOM_MAX = 1.5;
const TABLE_ZOOM_STEP = 0.1;

/** 每页条数档位：人才库列表走后端分页、搜索结果走前端切片，共用同一组档位。 */
const PAGE_SIZE_OPTIONS = [20, 50, 100];

function clampZoom(value: number): number {
  return Math.min(TABLE_ZOOM_MAX, Math.max(TABLE_ZOOM_MIN, Math.round(value * 10) / 10));
}

function readStoredZoom(): number {
  const raw = window.localStorage.getItem(TABLE_ZOOM_STORAGE_KEY);
  if (raw === null) return 1;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? clampZoom(parsed) : 1;
}

/** 生效条件的中文名：键同时兼容后端 `CandidateFilters` 字段名与旧的单值别名。 */
const CONDITION_LABELS: Record<string, string> = {
  location: "现居",
  preferred_location: "意向",
  locations: "现居",
  preferred_locations: "意向",
  min_years: "年限≥",
  max_years: "年限≤",
  min_age: "年龄≥",
  max_age: "年龄≤",
  highest_degree: "学历",
  max_qs_rank: "QS",
  school_level: "学校等级",
  school_region: "高校属地",
  exclude_skills: "排除",
  phone: "手机号",
  gender: "性别",
  name: "姓名",
  communication_note: "沟通文本",
  company: "公司",
  companies: "公司",
  title: "职位",
  school: "学校",
  career_directions: "职业方向",
  career_specializations: "职业细分",
  business_directions: "业务方向",
};

/** 生效条件来源的中文标注：面板手填 > AI 解析 > 规则解析。 */
const CONDITION_SOURCE_LABELS: Record<string, string> = {
  panel: "面板手填",
  llm: "AI 解析",
  rule: "规则解析",
};

const PLAN_SOURCE_LABELS: Record<string, string> = {
  llm: "AI 解析",
  mixed: "AI + 规则",
  rule: "规则解析",
};

function pageWindow(current: number, totalPages: number): (number | "…")[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
  const pages: (number | "…")[] = [1];
  if (current > 3) pages.push("…");
  for (let i = Math.max(2, current - 1); i <= Math.min(totalPages - 1, current + 1); i++) pages.push(i);
  if (current < totalPages - 2) pages.push("…");
  pages.push(totalPages);
  return pages;
}

export interface TalentPoolPageProps {
  query: string;
  onQueryChange: (value: string) => void;
  searchMode: "keyword" | "vector" | "hybrid";
  onSearchModeChange: (value: "keyword" | "vector" | "hybrid") => void;
  keywordOperator: CandidateKeywordOperator;
  onKeywordOperatorChange: (value: CandidateKeywordOperator) => void;
  rewriteEnabled: boolean;
  onRewriteEnabledChange: (value: boolean) => void;
  searchBody: boolean;
  onSearchBodyChange: (value: boolean) => void;
  /** AI 智能解析：默认关闭，三种模式均可用。 */
  parseEnabled: boolean;
  onParseEnabledChange: (value: boolean) => void;
  searching: boolean;
  searchFilterDraft: SearchFilterDraft;
  onSearchFilterChange: (draft: SearchFilterDraft) => void;
  batchProgress: { total: number; waiting: number; running: number; done: number; failed: number; percent: number } | null;
  batchTaskIds: string[];
  batchTimedOut: boolean;
  hasSearched: boolean;
  results: CandidateSearchItem[];
  searchConditions: SearchConditionView[];
  /**
   * 被判定「硬筛筛空」而退化为软排的条件字段名（后端 `query_plan.relaxed_conditions`）。
   * 这些条件仍在生效条件里显示，但只参与打分、不再过滤。
   */
  relaxedSearchFields: string[];
  /** AI 解析回显（词条 / 语义查询 / 未识别片段）；未开启解析时为 null。 */
  searchPlanEcho: SearchPlanEcho | null;
  /** 用户手动删掉的生效条件，可一键恢复。 */
  removedConditions: SearchConditionView[];
  onRemoveSearchCondition: (field: string, value: string) => void;
  onEditSearchCondition: (field: string, value: string) => void;
  onRestoreSearchCondition: (field: string) => void;
  /** 空结果自诊断：去掉全部推断条件并重搜（可能是解析误判）。 */
  onDropInferredConditions: () => void;
  candidates: CandidateListItem[];
  candidatePage: number;
  candidateTotal: number;
  candidateTotalPages: number;
  /** 人才库列表每页条数（后端分页，改变后重新拉第 1 页）。 */
  candidatePageSize: number;
  onCandidatePageSizeChange: (value: number) => void;
  visibleColumns: Record<CandidateColumnKey, boolean>;
  columnOrder: CandidateColumnKey[];
  columnsMenuOpen: boolean;
  candidateListRef: RefObject<HTMLDivElement | null>;
  onSubmitSearch: () => void;
  onContinueBatchPolling: () => void;
  onLoadCandidates: (page?: number) => void;
  onCloseSearchResults: () => void;
  onResetSearch: () => void;
  onToggleColumn: (key: CandidateColumnKey) => void;
  onMoveColumn: (fromKey: CandidateColumnKey, toKey: CandidateColumnKey) => void;
  onColumnsMenuOpenChange: (open: boolean) => void;
  onScrollTop: () => void;
  onUpdateField: (candidateId: string, field: string, value: unknown) => Promise<void>;
  /** 保存沟通记录（候选人级备注，不进画像与索引）。 */
  onSaveCommunicationNote: (candidateId: string, note: string) => Promise<void>;
  onEditEducation: (candidateId: string, educations: ParsedEducationData[]) => void;
  onEditProfile: (candidateId: string, summary: string | null, source: string | null, stale: boolean) => void;
  onMatch: (candidateId: string, name: string) => void;
  onPreview: (revisionId: string, name?: string, filename?: string) => void;
  onDownload: (revisionId: string, filename: string) => void;
  onCreateCase: (candidateId: string, name: string) => void;
  /** 建提醒：建好后出现在首页「今日待办」的「我的提醒」列。 */
  onCreateReminder: (candidateId: string, name: string) => void;
  /** 常规重新解析：正常页走文本/视觉解析，只有异常页 OCR。 */
  onReparse: (revisionId: string) => Promise<void>;
  /** 强制 OCR：整份走 OCR，扫描件/乱码简历专用。 */
  onForceReparse: (revisionId: string) => Promise<void>;
  onOpenReview: (revisionId: string) => void;
  onOpenParsed: (candidateId: string, parsed: ParsedResumeData | null) => void;
  onDelete: (candidateId: string) => void;
  selectedCandidateIds: ReadonlySet<string>;
  bulkBusy: boolean;
  onToggleCandidateSelect: (candidateId: string) => void;
  onToggleCandidateSelectAll: (checked: boolean, candidateIds: string[]) => void;
  onBulkDelete: () => void;
  onBulkReparse: (revisionIds: string[]) => void;
  onBulkForceOcr: (revisionIds: string[]) => void;
  onBulkMatch: () => void;
  onBulkDownload: () => void;
  /** 搜索侧 AI 复核（亮点/风险点）：只对搜索结果里的勾选人生效。 */
  searchReview: SearchReviewState | null;
  selectedSearchCount: number;
  onBulkSearchReview: () => void;
  onCancelSearchReview: () => void;
  onRetrySearchReview: () => void;
}

export function TalentPoolPage(props: TalentPoolPageProps) {
  const {
    query, onQueryChange, searchMode, onSearchModeChange, searching,
    keywordOperator, onKeywordOperatorChange, rewriteEnabled, onRewriteEnabledChange,
    searchBody, onSearchBodyChange,
    parseEnabled, onParseEnabledChange,
    searchFilterDraft, onSearchFilterChange,
    batchProgress, batchTaskIds, batchTimedOut, hasSearched, results, searchConditions, candidates,
    relaxedSearchFields, searchPlanEcho, removedConditions, onRemoveSearchCondition, onEditSearchCondition,
    onRestoreSearchCondition, onDropInferredConditions,
    candidatePage, candidateTotal, candidateTotalPages, visibleColumns, columnOrder, columnsMenuOpen,
    candidatePageSize, onCandidatePageSizeChange,
    candidateListRef, onSubmitSearch, onContinueBatchPolling,
    onLoadCandidates, onCloseSearchResults, onResetSearch, onToggleColumn, onMoveColumn,
    onColumnsMenuOpenChange, onScrollTop, onUpdateField, onSaveCommunicationNote, onEditEducation, onEditProfile,
    onMatch, onPreview, onDownload, onCreateCase, onCreateReminder, onReparse, onForceReparse, onOpenReview, onOpenParsed, onDelete,
    selectedCandidateIds, bulkBusy, onToggleCandidateSelect, onToggleCandidateSelectAll,
    onBulkDelete, onBulkReparse, onBulkForceOcr, onBulkMatch, onBulkDownload,
    searchReview, selectedSearchCount, onBulkSearchReview, onCancelSearchReview, onRetrySearchReview,
  } = props;

  const [filtersOpen, setFiltersOpen] = useState(false);
  const [resultsPage, setResultsPage] = useState(1);
  const [resultsPageSize, setResultsPageSize] = useState(PAGE_SIZE_OPTIONS[0]);
  const [tableZoom, setTableZoom] = useState(readStoredZoom);

  // 搜索侧复核状态：结论逐条落库，所以进度条与「已有多少条结论」可以同时看。
  const reviewItems = searchReview?.items ?? {};
  const reviewRunning = searchReview != null
    && !["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"].includes(searchReview.status);
  let reviewRecommend = 0;
  let reviewPending = 0;
  let reviewReject = 0;
  let reviewFailed = 0;
  for (const item of Object.values(reviewItems)) {
    if (item.verdict === "recommend") reviewRecommend += 1;
    else if (item.verdict === "reject") reviewReject += 1;
    else reviewPending += 1;
    if (item.failed) reviewFailed += 1;
  }

  useEffect(() => {
    setResultsPage(1);
  }, [results, resultsPageSize]);

  useEffect(() => {
    window.localStorage.setItem(TABLE_ZOOM_STORAGE_KEY, String(tableZoom));
  }, [tableZoom]);

  const resultsTotalPages = Math.max(1, Math.ceil(results.length / resultsPageSize));
  const pagedResults = results.slice((resultsPage - 1) * resultsPageSize, resultsPage * resultsPageSize);

  // 推断出来的条件（规则/AI 解析，非面板手填）：0 结果时最可疑，提供一键去掉重搜。
  const inferredConditions = searchConditions.filter(
    (condition) => condition.confidence === "inferred" && condition.source !== "panel");

  const resultsTable = pagedResults.length === 0 ? null : (
    <CandidateTable
      rows={pagedResults.map((item) => ({
        key: item.candidate_id,
        candidateId: item.candidate_id,
        name: item.name,
        phone: item.phone,
        revisionId: item.revision_id,
        filename: item.original_filename ?? "",
        parsed: item.parsed_data,
        highlights: reviewItems[item.candidate_id]?.highlights,
        risks: reviewItems[item.candidate_id]?.risks,
        communicationNote: item.communication_note ?? null,
      }))}
      visibleColumns={visibleColumns}
      columnOrder={columnOrder}
      onMoveColumn={onMoveColumn}
      onUpdateField={onUpdateField}
      onSaveCommunicationNote={onSaveCommunicationNote}
      onEditEducation={onEditEducation}
      onEditProfile={onEditProfile}
      onMatch={onMatch}
      onPreview={onPreview}
      onDownload={onDownload}
      onCreateCase={onCreateCase}
      onCreateReminder={onCreateReminder}
      onReparse={onReparse}
      onForceReparse={onForceReparse}
      onOpenReview={onOpenReview}
      onOpenParsed={onOpenParsed}
      onDelete={onDelete}
      selectable
      selectedIds={selectedCandidateIds}
      onToggleSelect={onToggleCandidateSelect}
      onToggleSelectAll={onToggleCandidateSelectAll}
    />
  );

  const candidatesTable = candidates.length === 0 ? null : (
    <CandidateTable
      rows={candidates.map((c) => ({
        key: c.candidate_id,
        candidateId: c.candidate_id,
        name: c.display_name,
        phone: c.phone,
        revisionId: c.revision_id,
        filename: c.original_filename ?? "",
        parsed: c.parsed_data,
        revisionStatus: c.revision_status,
        reviewError: c.error_message || c.error_code,
        communicationNote: c.communication_note ?? null,
      }))}
      visibleColumns={visibleColumns}
      columnOrder={columnOrder}
      onMoveColumn={onMoveColumn}
      onUpdateField={onUpdateField}
      onSaveCommunicationNote={onSaveCommunicationNote}
      onEditEducation={onEditEducation}
      onEditProfile={onEditProfile}
      onMatch={onMatch}
      onPreview={onPreview}
      onDownload={onDownload}
      onCreateCase={onCreateCase}
      onCreateReminder={onCreateReminder}
      onReparse={onReparse}
      onForceReparse={onForceReparse}
      onOpenReview={onOpenReview}
      onOpenParsed={onOpenParsed}
      onDelete={onDelete}
      selectable
      selectedIds={selectedCandidateIds}
      onToggleSelect={onToggleCandidateSelect}
      onToggleSelectAll={onToggleCandidateSelectAll}
    />
  );

  const selectedRevisionIds = Array.from(selectedCandidateIds)
    .map((id) => {
      const candidate = candidates.find((c) => c.candidate_id === id);
      const result = results.find((r) => r.candidate_id === id);
      return candidate?.revision_id ?? result?.revision_id;
    })
    .filter((x): x is string => !!x);

  return (
    <>
      {/* 工具栏控件较多（模式/逻辑/正文开关/改写/解析/筛选），窄屏必须允许换行：
          否则会横向溢出并把相邻按钮压在下面，点击事件被别的元素截走（e2e 实测）。 */}
      <form className="toolbar" style={{ flexWrap: "wrap" }} onSubmit={(event) => { event.preventDefault(); onSubmitSearch(); }}>
        <div className="search">
          <input
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="搜索人才、技能、公司或自然语言"
            aria-label="人才搜索"
          />
          <Button type="submit" variant="primary" disabled={searching}>{searching ? "搜索中" : "搜索"}</Button>
        </div>
        <div className="mode-switch" role="group" aria-label="搜索模式">
          {(["keyword", "vector", "hybrid"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              className={searchMode === mode ? "is-active" : ""}
              onClick={() => onSearchModeChange(mode)}
            >
              {mode === "keyword" ? "关键词" : mode === "vector" ? "向量" : "混合"}
            </button>
          ))}
        </div>
        {searchMode === "keyword" ? (
          <select
            className="keyword-operator"
            aria-label="关键词逻辑"
            value={keywordOperator}
            onChange={(event) => onKeywordOperatorChange(event.target.value as CandidateKeywordOperator)}
          >
            <option value="smart">智能排序</option>
            <option value="and">同时满足</option>
            <option value="or">满足任一</option>
          </select>
        ) : null}
        {searchMode !== "vector" ? (
          <span className="rewrite-toggle-group">
            <label className="rewrite-toggle">
              <input
                type="checkbox"
                aria-label="检索工作与项目经历正文"
                checked={searchBody}
                onChange={(event) => onSearchBodyChange(event.target.checked)}
              />
              <span>检索经历正文</span>
            </label>
            <InfoTip text="工作职责 + 项目描述（仅影响关键词通道）" />
          </span>
        ) : null}
        {searchMode !== "keyword" ? (
          <span className="rewrite-toggle-group">
            <label className="rewrite-toggle">
              <input
                type="checkbox"
                aria-label="AI 语义改写"
                checked={rewriteEnabled}
                onChange={(event) => onRewriteEnabledChange(event.target.checked)}
              />
              <span>AI 语义改写</span>
            </label>
            <InfoTip text="可能增加搜索时间" />
          </span>
        ) : null}
        <span className="rewrite-toggle-group">
          <label className="rewrite-toggle">
            <input
              type="checkbox"
              aria-label="AI 智能解析"
              checked={parseEnabled}
              onChange={(event) => onParseEnabledChange(event.target.checked)}
            />
            <span>AI 智能解析</span>
          </label>
          <InfoTip text="拆出硬条件 + 词条 + 语义查询" />
        </span>
        <div className="toolbar__divider" />
        <button type="button" className="btn btn-secondary" onClick={() => setFiltersOpen(!filtersOpen)}>精确筛选</button>
      </form>
      {hasSearched && (searchConditions.length > 0 || removedConditions.length > 0) && (
        <div className="search-conditions" role="list" aria-label="生效硬条件">
          <span className="muted">生效硬条件：</span>
          {searchConditions.map((condition, index) => {
            const label = CONDITION_LABELS[condition.field] ?? condition.field;
            const source = condition.source ? CONDITION_SOURCE_LABELS[condition.source] ?? condition.source : null;
            // 硬筛筛空 → 已退化为软排：条件照常展示（它仍是排序信号），但**不再过滤**。
            // 不标出来的话，用户会以为结果里每个人都满足这条条件。
            const relaxed = relaxedSearchFields.includes(condition.field);
            return (
              <span
                key={`${condition.field}-${condition.value}-${index}`}
                className={`condition-tag${condition.confidence === "inferred" ? " is-inferred" : ""}${relaxed ? " is-relaxed" : ""}`}
                role="listitem"
              >
                {label} {condition.value}
                {source ? <em className="muted">{source}</em> : null}
                {condition.confidence === "inferred" && !source ? <em className="muted">推断</em> : null}
                {relaxed ? (
                  <em className="muted" title="硬筛无匹配，已改为参与排序、不再过滤">已退化为排序</em>
                ) : null}
                <button
                  type="button"
                  className="condition-tag__action"
                  aria-label={`修改条件 ${label} ${condition.value}`}
                  title="回填到精确筛选面板"
                  onClick={() => { setFiltersOpen(true); onEditSearchCondition(condition.field, condition.value); }}
                >改</button>
                <button
                  type="button"
                  className="condition-tag__action"
                  aria-label={`移除条件 ${label} ${condition.value}`}
                  title="从生效条件里去掉"
                  onClick={() => onRemoveSearchCondition(condition.field, condition.value)}
                >×</button>
              </span>
            );
          })}
          {removedConditions.map((condition) => {
            const label = CONDITION_LABELS[condition.field] ?? condition.field;
            return (
              <button
                key={`removed-${condition.field}`}
                type="button"
                className="condition-tag is-removed"
                aria-label={`恢复条件 ${label} ${condition.value}`}
                title="恢复该条件"
                onClick={() => onRestoreSearchCondition(condition.field)}
              >
                {label} {condition.value}
                <em className="muted">已移除，点击恢复</em>
              </button>
            );
          })}
        </div>
      )}
      {hasSearched && searchPlanEcho && (
        <div className="search-parse-echo" role="group" aria-label="AI 解析回显">
          <span className="search-parse-echo__item">
            <em className="muted">解析来源</em>{PLAN_SOURCE_LABELS[searchPlanEcho.source] ?? searchPlanEcho.source}
          </span>
          <span className="search-parse-echo__item">
            <em className="muted">检索词条</em>{searchPlanEcho.keywordTerms || "—"}
          </span>
          <span className="search-parse-echo__item">
            <em className="muted">语义查询</em>{searchPlanEcho.semanticQuery || "—"}
          </span>
          <span className="search-parse-echo__item">
            <em className="muted">未识别片段</em>
            {searchPlanEcho.unparsedTerms.length ? searchPlanEcho.unparsedTerms.join("、") : "—"}
          </span>
        </div>
      )}
      {filtersOpen && (
        <div className="search-filters" onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); onSubmitSearch(); } }}>
          <div className="search-filter-grid">
            <label>最低工作年限<input aria-label="最低工作年限" type="number" min="0" max="80" value={searchFilterDraft.minYears} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, minYears: e.target.value })} /></label>
            <label>最高工作年限<input aria-label="最高工作年限" type="number" min="0" max="80" value={searchFilterDraft.maxYears} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, maxYears: e.target.value })} /></label>
            <label>最低年龄<input aria-label="最低年龄" type="number" min="16" max="80" value={searchFilterDraft.minAge} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, minAge: e.target.value })} /></label>
            <label>最高年龄<input aria-label="最高年龄" type="number" min="16" max="80" value={searchFilterDraft.maxAge} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, maxAge: e.target.value })} /></label>
            <label>学历<select aria-label="最低学历" value={searchFilterDraft.degree} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, degree: e.target.value })}>
              <option value="">由查询语句决定</option><option value="ASSOCIATE">大专</option><option value="BACHELOR">本科</option><option value="MASTER">硕士</option><option value="DOCTORATE">博士</option>
            </select></label>
            <label>现居城市<input aria-label="现居城市" placeholder="上海、苏州" value={searchFilterDraft.locations} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, locations: e.target.value })} /></label>
            <label>意向城市<input aria-label="意向城市" placeholder="北京、深圳" value={searchFilterDraft.preferredLocations} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, preferredLocations: e.target.value })} /></label>
            <label>学校等级<input aria-label="学校等级" placeholder="985 / 211" value={searchFilterDraft.schoolLevel} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, schoolLevel: e.target.value })} /></label>
            <label>QS最高排名<input aria-label="QS最高排名" type="number" min="1" value={searchFilterDraft.maxQsRank} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, maxQsRank: e.target.value })} /></label>
            <label>排除技能<input aria-label="排除技能" placeholder="外包、PHP" value={searchFilterDraft.excludeSkills} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, excludeSkills: e.target.value })} /></label>
            <label>手机号<input aria-label="手机号" placeholder="完整手机号" value={searchFilterDraft.phone} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, phone: e.target.value })} /></label>
            <label>性别<select aria-label="性别" value={searchFilterDraft.gender} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, gender: e.target.value })}>
              <option value="">全部</option>
              <option value="男">男</option>
              <option value="女">女</option>
            </select></label>
            <label>姓名<input aria-label="姓名" placeholder="字段内关键词" value={searchFilterDraft.name} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, name: e.target.value })} /></label>
            <label>公司<input aria-label="公司" placeholder="匹配全部工作经历" value={searchFilterDraft.company} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, company: e.target.value })} /></label>
            <label>职位<input aria-label="职位" placeholder="匹配全部工作经历" value={searchFilterDraft.title} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, title: e.target.value })} /></label>
            <label>学校<input aria-label="学校" placeholder="学校或教育经历" value={searchFilterDraft.school} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, school: e.target.value })} /></label>
            <label>沟通文本<input aria-label="沟通文本" placeholder="备注子串，如：沟通主动" value={searchFilterDraft.communicationNote} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, communicationNote: e.target.value })} /></label>
            {/* 级联下拉：一级选大类，二级在该大类右侧弹出。 */}
            <label>职业方向
              <CareerDirectionFilter
                ariaLabel="职业方向"
                directions={searchFilterDraft.careerDirections}
                specializations={searchFilterDraft.careerSpecializations}
                onChange={(next) => onSearchFilterChange({
                  ...searchFilterDraft,
                  careerDirections: next.directions,
                  careerSpecializations: next.specializations,
                })}
              />
            </label>
            <label>业务方向<select aria-label="业务方向" value={searchFilterDraft.businessDirections[0] ?? ""} onChange={(e) => {
              const value = e.target.value;
              onSearchFilterChange({ ...searchFilterDraft, businessDirections: value ? [value] : [] });
            }}>
              <option value="">不限</option>
              {BUSINESS_DIRECTION_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select></label>
            <label>国内外高校<select aria-label="国内外高校" value={searchFilterDraft.schoolRegion} onChange={(e) => onSearchFilterChange({ ...searchFilterDraft, schoolRegion: e.target.value })}>
              <option value="">全部</option>
              <option value="domestic">国内</option>
              <option value="overseas">国外</option>
            </select></label>
          </div>
          <button type="button" className="btn btn-ghost btn-xs" onClick={() => onSearchFilterChange({ ...EMPTY_SEARCH_FILTER_DRAFT })}>清除筛选</button>
        </div>
      )}

      {batchProgress && (
        <div className="batch-progress">
          <span>
            解析进度：{batchProgress.percent}%（总数 {batchProgress.total} · 等待 {batchProgress.waiting} · 运行 {batchProgress.running} · 成功 {batchProgress.done} · 失败 {batchProgress.failed}）
          </span>
          <progress max={batchProgress.total || 1} value={batchProgress.done + batchProgress.failed} />
          {batchTimedOut && (
            <p role="status" className="muted">
              批次仍在后台运行。{batchTaskIds.length > 0 && (
                <button type="button" className="btn btn-ghost btn-xs" onClick={onContinueBatchPolling}>继续刷新</button>
              )}
            </p>
          )}
        </div>
      )}

      {searchReview && (
        <div className="batch-progress" role="status" aria-label="AI 复核状态">
          <span>
            {reviewRunning
              ? `AI 复核中 ${searchReview.progress}%`
              : `AI 复核完成（${searchReview.status}）`}
            ：推荐 {reviewRecommend} / 待核 {reviewPending} / 不推荐 {reviewReject}
            {reviewFailed > 0 && `（复核失败 ${reviewFailed}）`}
          </span>
          {reviewRunning && (
            <Button variant="ghost" size="sm" onClick={onCancelSearchReview}>取消复核</Button>
          )}
          {["FAILED", "DEAD_LETTER", "CANCELLED"].includes(searchReview.status) && (
            <Button variant="ghost" size="sm" onClick={onRetrySearchReview}>重试复核</Button>
          )}
          {searchReview.error_message && <p className="muted">{searchReview.error_message}</p>}
        </div>
      )}

      {selectedCandidateIds.size > 0 && (
        <div className="bulk-toolbar" role="toolbar" aria-label="批量操作">
          <span className="bulk-toolbar__count">已选 {selectedCandidateIds.size} 位</span>
          {hasSearched && results.length > 0 && (
            <Button variant="secondary" size="sm" disabled={bulkBusy || selectedSearchCount === 0} onClick={onBulkSearchReview}>AI 复核</Button>
          )}
          <Button variant="secondary" size="sm" disabled={bulkBusy} onClick={onBulkDownload}>下载</Button>
          <Button variant="secondary" size="sm" disabled={bulkBusy || selectedRevisionIds.length === 0} onClick={() => onBulkReparse(selectedRevisionIds)}>重新解析</Button>
          <Button variant="secondary" size="sm" disabled={bulkBusy || selectedRevisionIds.length === 0} onClick={() => onBulkForceOcr(selectedRevisionIds)}>强制 OCR</Button>
          <Button variant="secondary" size="sm" disabled={bulkBusy} onClick={onBulkMatch}>匹配</Button>
          <Button variant="danger" size="sm" disabled={bulkBusy} onClick={onBulkDelete}>删除</Button>
          <Button variant="ghost" size="sm" onClick={() => onToggleCandidateSelectAll(false, Array.from(selectedCandidateIds))}>取消选择</Button>
        </div>
      )}

      <div className="card" ref={candidateListRef}>
        <div className="card__head">
          <div className="card__title">
            候选人
            {hasSearched && <span className="card__count"> {results.length} 条搜索结果</span>}
          </div>
          <div className="card__tools">
            <div className="table-zoom-controls" role="group" aria-label="表格缩放">
              <button className="btn btn-ghost btn-xs" aria-label="缩小表格" title="缩小（显示更多候选人）" disabled={tableZoom <= TABLE_ZOOM_MIN} onClick={() => setTableZoom((z) => clampZoom(z - TABLE_ZOOM_STEP))}>−</button>
              <span className="table-zoom-value">{Math.round(tableZoom * 100)}%</span>
              <button className="btn btn-ghost btn-xs" aria-label="放大表格" title="放大" disabled={tableZoom >= TABLE_ZOOM_MAX} onClick={() => setTableZoom((z) => clampZoom(z + TABLE_ZOOM_STEP))}>＋</button>
              <button className="btn btn-ghost btn-xs" aria-label="重置表格缩放" title="重置缩放" disabled={tableZoom === 1} onClick={() => setTableZoom(1)}>↺</button>
            </div>
            <div style={{ position: "relative" }}>
              <button className="btn btn-ghost btn-xs" onClick={() => onColumnsMenuOpenChange(!columnsMenuOpen)}>列设置</button>
              {columnsMenuOpen && (
                <div style={{ position: "absolute", top: "100%", right: 0, zIndex: 20, background: "#fff", border: "1px solid #e5e7eb", borderRadius: 8, padding: 6, boxShadow: "0 4px 12px rgba(15,23,42,0.08)", minWidth: 160 }}>
                  {CANDIDATE_COLUMNS.map((col) => (
                    <label key={col.key} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, padding: "5px 8px", cursor: "pointer" }}>
                      <input type="checkbox" checked={visibleColumns[col.key]} onChange={() => onToggleColumn(col.key)} />
                      {col.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <button className="btn btn-ghost btn-xs" onClick={() => onLoadCandidates(1)}>显示候选人</button>
            {results.length > 0 && (
              <button className="btn btn-ghost btn-xs" onClick={onCloseSearchResults}>关闭结果</button>
            )}
            <button className="btn btn-ghost btn-xs" onClick={onResetSearch}>清空搜索</button>
          </div>
        </div>
        {results.length > 0 ? (
          <>
            <div className="table-zoom" style={{ zoom: tableZoom }}>
              <div className="table-scroll">{resultsTable}</div>
            </div>
            <div className="pagination">
              <button className="page" aria-label="上一页" disabled={resultsPage <= 1} onClick={() => setResultsPage((p) => p - 1)}>‹</button>
              {pageWindow(resultsPage, resultsTotalPages).map((page, index) =>
                typeof page === "number" ? (
                  <button
                    key={`results-page-${page}-${index}`}
                    className={page === resultsPage ? "page is-current" : "page"}
                    onClick={() => setResultsPage(page)}
                  >
                    {page}
                  </button>
                ) : (
                  <span key={`results-ellipsis-${index}`} className="spacer">…</span>
                )
              )}
              <button className="page" aria-label="下一页" disabled={resultsPage >= resultsTotalPages} onClick={() => setResultsPage((p) => p + 1)}>›</button>
              <span className="hint">
                每页
                <select className="page-size-select" aria-label="搜索结果每页条数"
                  value={resultsPageSize}
                  onChange={(e) => setResultsPageSize(Number(e.target.value))}>
                  {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
                </select>
                条 · 共 {results.length} 条
              </span>
            </div>
          </>
        ) : hasSearched ? (
          <div className="empty-state">
            <strong>没有符合条件的候选人</strong>
            <p>可调整搜索词或筛选条件后重试。</p>
            {inferredConditions.length > 0 ? (
              <p className="empty-state__hint">
                可能是解析误判：
                {inferredConditions
                  .map((condition) => `${CONDITION_LABELS[condition.field] ?? condition.field} ${condition.value}`)
                  .join("、")}
                是推断出来的条件。
                <Button type="button" variant="secondary" onClick={onDropInferredConditions}>
                  去掉推断条件重搜
                </Button>
              </p>
            ) : null}
          </div>
        ) : candidates.length > 0 ? (
          <>
            <div className="table-zoom" style={{ zoom: tableZoom }}>
              <div className="table-scroll">{candidatesTable}</div>
            </div>
            <div className="pagination">
              <button className="page" aria-label="上一页" disabled={candidatePage <= 1} onClick={() => onLoadCandidates(candidatePage - 1)}>‹</button>
              {pageWindow(candidatePage, candidateTotalPages).map((page, index) =>
                typeof page === "number" ? (
                  <button
                    key={`page-${page}-${index}`}
                    className={page === candidatePage ? "page is-current" : "page"}
                    onClick={() => onLoadCandidates(page)}
                  >
                    {page}
                  </button>
                ) : (
                  <span key={`ellipsis-${index}`} className="spacer">…</span>
                )
              )}
              <button className="page" aria-label="下一页" disabled={candidatePage >= candidateTotalPages} onClick={() => onLoadCandidates(candidatePage + 1)}>›</button>
              <span className="hint">
                每页
                <select className="page-size-select" aria-label="人才库每页条数"
                  value={candidatePageSize}
                  onChange={(e) => onCandidatePageSizeChange(Number(e.target.value))}>
                  {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
                </select>
                条
              </span>
            </div>
          </>
        ) : (
          <div className="empty-state">
            <strong>从一次搜索开始</strong>
            <p>输入技能、经历或自然语言条件查找人才，或点击「显示候选人」查看已导入人才。</p>
          </div>
        )}
      </div>
    </>
  );
}
