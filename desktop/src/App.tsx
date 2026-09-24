import { FormEvent, Fragment, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { waitForTask } from "./tasks/polling";
import { invoke } from "@tauri-apps/api/core";

import type {
  AiCatalog,
  AiConfig,
  AiConfigUpdate,
  AiStatus,
  ConnectionProbeReport,
} from "./ai/types";
import { mergeAiConnection } from "./ai/merge";

import "./styles.css";
import "./cases/workflow.css";
import { CaseDrawer } from "./cases/CaseDrawer";
import { CandidateReminderDialog } from "./reminders/CandidateReminderDialog";
import { ResumeReviewDrawer } from "./resumes/ResumeReviewDrawer";
import { CandidateEducationEditor } from "./components/CandidateEducationEditor";
import { CandidateProfileEditor } from "./components/CandidateProfileEditor";
import { CandidateParsedEditor } from "./components/CandidateParsedEditor";
import { JdParsedEditor } from "./components/JdParsedEditor";
import { JdProfileEditor, type JdExactConstraint, type JdProfileYears } from "./components/JdProfileEditor";
import { CANDIDATE_COLUMNS_DEFAULT, CANDIDATE_COLUMNS_ORDER_DEFAULT, type CandidateColumnKey } from "./components/CandidateTable";
import { HoverText } from "./components/ui";
import { CareerDirectionPicker } from "./components/DirectionPicker";
import { EMPTY_SEARCH_FILTER_DRAFT, TalentPoolPage, type SearchFilterDraft } from "./pages/TalentPoolPage";
import type { CandidateKeywordOperator, CandidateSearchOptions, ProfileGenerationProgress } from "./api/client";
import { JdManagementPage, reviewMatchLines, reviewVerdictCounts, reviewVerdictLabel, reviewVerdictsByResultId, sortReviewItems, type ReviewVerdictItem } from "./pages/JdManagementPage";
import { RecruitmentPage } from "./pages/RecruitmentPage";
import { DashboardPage } from "./pages/DashboardPage";
import { BdAssistantPage } from "./pages/BdAssistantPage";
import { MappingPage } from "./pages/MappingPage";
import { SettingsPage } from "./pages/SettingsPage";


const DEGRADED_REASON_LABELS: Record<string, string> = {
  TIMEOUT: "检索超时",
  SEARCH_UNAVAILABLE: "检索服务不可用",
  EMBEDDING_UNAVAILABLE: "语义向量模型无法调用，已降级为关键词匹配",
  VECTOR_UNAVAILABLE: "向量检索不可用",
  FTS_UNAVAILABLE: "关键词检索不可用",
  RERANKER_UNAVAILABLE: "重排模型无法调用，结果排序可能不够精准",
  EXCLUSION_UNVERIFIED: "排除技能未能完全校验",
  INDEX_SYNC_PENDING: "部分候选人索引尚未同步",
  LIVE_VALIDATION_UNAVAILABLE: "实时校验暂不可用",
};

/** 与后端 `search/service.py` 里 `f"FILTER_RELAXED:{name}"` 的前缀保持一致。 */
const RELAXED_REASON_PREFIX = "FILTER_RELAXED:";

/** 硬筛筛空后退化为软排的字段名 → 中文；与后端 `RELAXABLE_FIELDS` 同名。 */
const RELAXED_FIELD_LABELS: Record<string, string> = {
  company: "公司", companies: "公司", title: "职位",
  career_directions: "职业方向", career_specializations: "职业细分",
  business_directions: "业务方向",
};

function describeDegraded(reasons: string[]): string {
  const labels = reasons
    .map((reason) => {
      // 「硬筛筛空 → 退化为软排」带字段名，逐条说清是哪条条件不再过滤。
      if (reason.startsWith(RELAXED_REASON_PREFIX)) {
        const field = reason.slice(RELAXED_REASON_PREFIX.length);
        return `${RELAXED_FIELD_LABELS[field] ?? field}条件无匹配，已改为参与排序（不再过滤）`;
      }
      return DEGRADED_REASON_LABELS[reason] ?? reason;
    })
    .filter(Boolean);
  return labels.length > 0 ? labels.join("、") : reasons.join("、");
}


async function openExternal(url: string) {
  try {
    await invoke("open_external", { url });
  } catch (error) {
    // 非 http/https 或系统浏览器打开失败时，静默回退为提示。
    console.warn("打开链接失败", error);
  }
}

async function copyLink(url: string) {
  try {
    await navigator.clipboard.writeText(url);
  } catch (error) {
    console.warn("复制链接失败", error);
  }
}


export interface ImportedResume {
  action: string;
  candidate_id: string | null;
  document_id: string | null;
  revision_id: string | null;
  blob_id: string | null;
  task_id: string | null;
  message?: string;
  conflict_candidate_ids?: string[];
  created_task?: boolean;
}

export interface TaskStatus {
  id: string;
  task_type: string;
  status: string;
  progress: number;
  error_message: string | null;
}

export type TaskAction = "cancel" | "retry" | "pause" | "resume";

export interface CandidateSearchItem {
  candidate_id: string;
  revision_id: string;
  name: string;
  phone: string | null;
  reasons: string[];
  parsed_data: ParsedResumeData | null;
  content: string;
  score: number;
  matched_channels: string[];
  total_years: number | null;
  highest_degree: string | null;
  location: string | null;
  result_id?: string | null;
  original_filename?: string | null;
  matched_skills?: string[];
  missing_skills?: string[];
  eligibility?: string;
  match_tier?: string;
  /** 沟通记录：候选人级自由文本，不在 parsed_data 里，也不参与画像与索引。 */
  communication_note?: string | null;
  direction_reason?: string;
  evidence?: string[];
  // 业务方向是否一致（True/False/None）：一致者按 80/20 置顶，不淘汰。
  business_match?: boolean | null;
}

export interface CandidateSearchResult {
  items: CandidateSearchItem[];
  degraded_reasons: string[];
  empty_reason?: string | null;
  status?: string | null;
  has_more?: boolean;
  query_plan?: {
    operator: "smart" | "and" | "or";
    rewrite_requested: boolean;
    rewrite_status: "disabled" | "not_applicable" | "unchanged" | "success" | "rejected" | "unavailable";
    semantic_query: string | null;
    // 附加诊断字段：applied 表示改写向量是否参与召回，fallback_reason 只描述技术性原因。
    rewrite_applied?: boolean;
    rewrite_fallback_reason?: "provider_error" | null;
    parsed_conditions?: { field: string; value: string; confidence: string }[];
    retained_keywords?: string;
    // AI 解析回显：解析来源、交给 FTS 的词条、未识别残句、以及合并后的最终生效条件。
    parsed_plan_source?: "rule" | "llm" | "mixed";
    keyword_terms?: string;
    unparsed_terms?: string[];
    effective_conditions?: SearchConditionView[];
    /**
     * 被判定「硬筛筛空」而退化为软排的条件字段名：它们仍在生效条件里，但只参与
     * 排序、不再过滤。界面需要据此说明「为什么结果比条件看起来更宽」。
     */
    relaxed_conditions?: string[];
  } | null;
}

/** 生效硬条件的来源：面板手填 > AI 解析 > 规则解析。 */
export type SearchConditionSource = "panel" | "llm" | "rule";

export interface SearchConditionView {
  field: string;
  value: string;
  confidence: string;
  source?: SearchConditionSource;
}

/** AI 解析的可解释回显：让用户看到「AI 有没有读懂」。 */
export interface SearchPlanEcho {
  source: "rule" | "llm" | "mixed";
  keywordTerms: string;
  semanticQuery: string | null;
  unparsedTerms: string[];
}

/** 搜索侧 AI 复核：按搜索条件产出亮点 / 风险点，结论按查询+候选人落库。 */
export interface SearchReviewVerdictItem {
  candidate_id: string;
  verdict: string;
  highlights: string[];
  risks: string[];
  failed: boolean;
  error: string | null;
}

export interface SearchReviewStatus {
  status: string;
  progress: number;
  error_message: string | null;
  query_key: string | null;
  items: SearchReviewVerdictItem[];
}

export interface CandidateSearchFilters {
  min_years?: number; max_years?: number; highest_degree?: string;
  degree_exact?: boolean; locations?: string[]; preferred_locations?: string[];
  candidate_status?: string; max_qs_rank?: number; school_level?: string;
  exclude_skills?: string[];
  phone?: string;
  gender?: string;
  name?: string;
  /** 沟通文本：子串匹配候选人沟通记录（后端在 SQLite 层过滤，不进索引）。 */
  communication_note?: string;
  company?: string;
  title?: string;
  school?: string;
  direction?: string;
  school_region?: string;
  /** 旧版专长枚举，仅存量兼容；新筛选请用 careerSpecializations。 */
  specializations?: string[];
  /** 职业方向大类（多选，OR）。 */
  career_directions?: string[];
  /** 职业方向细分（多选，OR）。 */
  career_specializations?: string[];
  /** 业务方向（多选，OR）。 */
  business_directions?: string[];
  min_age?: number;
  max_age?: number;
}

export interface CandidateListItem {
  candidate_id: string;
  revision_id: string;
  display_name: string;
  total_years: number | null;
  highest_degree: string | null;
  location: string | null;
  status: string;
  revision_status: string | null;
  phone: string | null;
  original_filename: string | null;
  parsed_data: ParsedResumeData | null;
  error_code?: string | null;
  error_message?: string | null;
  /** 沟通记录：候选人级自由文本，不在 parsed_data 里，也不参与画像与索引。 */
  communication_note?: string | null;
}

export interface CandidatePage {
  items: CandidateListItem[]; total: number; page: number; page_size: number; has_more: boolean;
}

export interface CasePage {
  items: CaseItem[]; total: number; page: number; page_size: number; has_more: boolean;
}

export interface JdPage {
  items: JdListItem[]; total: number; page: number; page_size: number; has_more: boolean;
}

export interface CandidateContact {
  email: string | null;
  phone: string | null;
  email_confidence: number | null;
  phone_confidence: number | null;
}

export interface ResumeRevision {
  revision_id: string;
  display_name: string | null;
  original_filename: string;
  status: string;
  is_current: boolean;
  created_at: string;
  parsed_data: ParsedResumeData | null;
}

export interface ParsedExperienceData {
  company: string | null;
  title: string | null;
  start_date?: string | null;
  end_date?: string | null;
  location?: string | null;
  summary: string;
  industry?: string | null;
}

export interface ParsedProjectData {
  name: string | null;
  summary: string;
  tech_stack?: string | null;
  business_scene?: string | null;
}

export interface ParsedEducationData {
  school?: string | null;
  degree?: string | null;
  major?: string | null;
  graduation_year?: number | null;
  country_region?: string | null;
  school_tags?: string[];
  qs_year?: number | null;
  qs_rank?: number | null;
}

export interface DirectionAssessment {
  primary?: string | null;
  secondary?: string | null;
  confidence?: string;
  evidence_paths?: string[];
  management?: boolean;
  /** 职业方向细分（新词表；旧数据里是旧版专长枚举）。 */
  specializations?: string[];
  /** 多值职业大类（≤2，JD ≤3）。 */
  career_directions?: string[];
  /** 多值业务方向（≤2）。 */
  business_directions?: string[];
  taxonomy_version?: string;
}

export interface ProfilePointData {
  text: string;
  evidence_paths?: string[];
}

export interface ParsedResumeData {
  name?: string | null;
  total_years?: number | null;
  highest_degree?: string | null;
  location?: string | null;
  preferred_location?: string | null;
  preferred_locations?: string[];
  school?: string | null;
  school_level?: string | null;
  qs_rank?: number | null;
  school_tier?: string | null;
  graduation_year?: number | null;
  birth_year?: number | null;
  age?: number | null;
  gender?: string | null;
  salary?: string | null;
  job_level?: string | null;
  industry?: string | null;
  current_industry?: string | null;
  longest_industry?: string | null;
  skills?: string[];
  summary?: string;
  experiences?: ParsedExperienceData[];
  projects?: ParsedProjectData[];
  educations?: ParsedEducationData[];
  current_company?: string | null;
  current_title?: string | null;
  location_source?: string | null;
  age_source?: string | null;
  ai_profile_summary?: string | null;
  ai_profile_source?: string | null;
  ai_profile_input_hash?: string | null;
  ai_profile_stale?: boolean;
  ai_profile_narrative?: string | null;
  ai_profile_points?: ProfilePointData[];
  ai_profile_compact?: string | null;
  direction?: string | null;
  direction_assessment?: DirectionAssessment | null;
  /** 多值职业大类（≤2）。 */
  career_directions?: string[];
  /** 职业方向细分（≤4，每个大类下 ≤2）。 */
  career_specializations?: string[];
  /** 业务方向（≤2）。 */
  business_directions?: string[];
  career_taxonomy_version?: string;
}

export interface IndexRebuildState {
  mode: "inplace" | "reset";
  reason: string;
  total: number;
  started_at: string;
  finished_at: string | null;
  archived: string | null;
}

export interface IndexSyncStatus {
  pending: number;
  failed: number;
  items: { entity_type: string; entity_id: string; status: string; attempts: number; error: string | null }[];
  indexes?: { entity_type: string; compatible: boolean; error: string | null }[];
  rebuild?: IndexRebuildState | null;
}

export interface ResumeReview {
  revision_id: string;
  candidate_id: string;
  status: string;
  review_required: boolean;
  raw_text: string | null;
  parsed_data: ParsedResumeData | null;
  review_data: ParsedResumeData | null;
  manual_overrides: Record<string, unknown> | null;
  extraction_diagnostics: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
}

export interface ImportedJd {
  jd_id: string;
  revision_id: string;
}

export interface JdParsedData {
  title?: string;
  company?: string;
  department?: string | null;
  location?: string | null;
  salary?: string | null;
  ai_category?: string | null;
  industry?: string | null;
  min_years?: number | null;
  highest_degree?: string | null;
  qs_level?: string | null;
  core_duties?: string[];
  required_skills?: string[];
  plus_skills?: string[];
  plus_industry?: string[];
  plus_project_types?: string[];
  summary?: string;
  candidate_profile?: string | null;
  candidate_profile_narrative?: string | null;
  candidate_profile_points?: ProfilePointData[];
  candidate_profile_compact?: string | null;
  requirements?: { kind: string; label: string; value: string }[];
  direction?: string | null;
  direction_assessment?: DirectionAssessment | null;
  /** 多值职业大类（≤3）。 */
  career_directions?: string[];
  /** 职业方向细分（每个大类下 ≤2）。 */
  career_specializations?: string[];
  /** 业务方向（≤2）。 */
  business_directions?: string[];
  career_taxonomy_version?: string;
  exact_constraints?: JdExactConstraint[];
}

export interface JdListItem {
  jd_id: string;
  revision_id: string;
  company: string;
  title: string;
  status: string;
  jd_status: string;
  ai_category: string | null;
  location: string | null;
  min_years: number | null;
  parsed_data: JdParsedData | null;
  source_text: string | null;
}

export interface MatchRun {
  run_id: string | null;
  items: CandidateSearchItem[];
  status?: string;
  empty_reason?: string | null;
  degraded_reasons?: string[];
  /** 已下推为检索过滤的 JD 硬条件（含原文依据）。 */
  hard_filters?: { kind: string; alternatives: string[]; source_text: string }[];
  /** 安全阀触发放宽掉的硬条件；非空表示结果不满足这些条件。 */
  relaxed?: string[];
}

export interface BatchMatchResult {
  revision_id: string;
  run_id: string;
  items: CandidateSearchItem[];
}

export type MatchMarkStatus = "未处理" | "保留";

export interface MatchResultItem {
  result_id: string;
  candidate_id: string;
  name: string;
  score: number;
  status: MatchMarkStatus;
  total_years: number | null;
  highest_degree: string | null;
  location: string | null;
  matched_skills: string[];
  missing_skills: string[];
}

export interface MatchResultGroup {
  jd_id: string;
  revision_id: string;
  company: string;
  title: string;
  items: MatchResultItem[];
}

export type JdStatus = "OPEN" | "FILLED" | "CANCELLED";

export interface MatchCandidateItem {
  result_id: string;
  jd_id: string;
  revision_id: string;
  resume_revision_id?: string | null;
  company: string;
  title: string;
  score: number;
  status: MatchMarkStatus;
  case_id: string | null;
  jd_status: string;
  ai_category: string | null;
  parsed_data: JdParsedData | null;
  source_text: string | null;
  // 业务方向是否一致：一致者按 80/20 置顶展示（不淘汰）。
  business_match?: boolean | null;
}

export interface CandidateMatchResult {
  run_id: string | null;
  items: MatchCandidateItem[];
}

/** 抽屉里的岗位行：批量匹配时带上来源候选人与该候选人的 run，供按行建流程/复核。 */
export interface MatchDrawerJdItem extends MatchCandidateItem {
  candidate_id?: string;
  candidate_name?: string;
  run_id?: string | null;
}

/** 批量匹配结果里按 JD 修订去重的岗位详情（parsed_data 不做 N 份重复传输）。 */
export interface JdMatchDetail {
  revision_id: string;
  jd_id: string;
  company: string;
  title: string;
  jd_status: string;
  ai_category: string | null;
  parsed_data: JdParsedData | null;
  source_text: string | null;
}

export interface BulkCandidateMatchItem {
  candidate_id: string;
  name: string | null;
  run_id: string | null;
  matched_jobs: number;
  error: string | null;
  items: MatchCandidateItem[];
}

export interface BulkCandidateMatchResult {
  results: BulkCandidateMatchItem[];
  jd_details: JdMatchDetail[];
}

export interface AiReviewState {
  review_id: string | null;
  status: string;
  progress: number;
  result_ref: string | null;
  error_message: string | null;
}

/** 搜索侧 AI 复核的运行状态：结论按 candidate_id 归拢，逐条落库边跑边可读。 */
export interface SearchReviewState {
  review_id: string;
  status: string;
  progress: number;
  error_message: string | null;
  items: Record<string, SearchReviewVerdictItem>;
}

function searchReviewStateFrom(reviewId: string, status: SearchReviewStatus): SearchReviewState {
  return {
    review_id: reviewId,
    status: status.status,
    progress: status.progress,
    error_message: status.error_message,
    items: Object.fromEntries(status.items.map((item) => [item.candidate_id, item])),
  };
}

export type MatchDrawerSource = { candidate_id: string; name: string };

export type MatchDrawerState =
  | { mode: "candidates"; title: string; items: CandidateSearchItem[]; statuses: Record<string, MatchMarkStatus> }
  | { mode: "jds"; title: string; items: MatchDrawerJdItem[]; run_id: string | null; statuses: Record<string, MatchMarkStatus>;
      /** 批量匹配来源：非空时抽屉是「多人 × 岗位」的扁平列表，可按候选人筛选。 */
      sources?: MatchDrawerSource[] };

export interface DiagnosticsData {
  sqlite_version: string;
  database_path: string;
  database_size_bytes: number;
  counts: Record<string, number>;
  pragmas: Record<string, string>;
}

export interface MappingProject {
  id: string;
  name: string;
  description: string | null;
}

export interface MappingSnapshot {
  id: string;
  label: string;
  is_current: boolean;
}

export interface MappingTreeNode {
  id: string;
  name: string;
  sort_order: number;
  children: MappingTreeNode[];
}

export interface OrgCompany {
  id: string;
  name: string;
}

export interface OrgDepartment {
  id: string;
  company_id: string;
  parent_id: string | null;
  name: string;
  leader_id: string | null;
  leader_report_to: string | null;
  team_size: number | null;
  business_direction: string | null;
  tech_stack: string | null;
  office_location: string | null;
  hc_status: string | null;
  hc_internal_note: string | null;
}

export interface OrgEmployee {
  id: string;
  company_id: string;
  department_id: string | null;
  candidate_id: string | null;
  candidate_name: string | null;
  current_revision_id: string | null;
  name: string;
  title: string | null;
  job_level: string | null;
  report_to: string | null;
  subordinate_count: number | null;
  tenure_years: number | null;
  business_module: string | null;
  status: string | null;
  intention: string | null;
  remark: string | null;
  contact: string | null;
  is_key: boolean;
}

export interface CreateOrgDepartmentInput {
  company_id: string;
  name: string;
  parent_id?: string | null;
  leader_id?: string | null;
  leader_report_to?: string | null;
  team_size?: number | null;
  business_direction?: string | null;
  tech_stack?: string | null;
  office_location?: string | null;
  hc_status?: string | null;
  hc_internal_note?: string | null;
}

export interface CreateOrgEmployeeInput {
  company_id: string;
  name: string;
  department_id?: string | null;
  title?: string | null;
  job_level?: string | null;
  report_to?: string | null;
  subordinate_count?: number | null;
  tenure_years?: number | null;
  business_module?: string | null;
  status?: string | null;
  intention?: string | null;
  remark?: string | null;
  contact?: string | null;
  is_key?: boolean;
}

export type UpdateOrgDepartmentInput = Partial<Omit<CreateOrgDepartmentInput, "company_id">>;
export type UpdateOrgEmployeeInput = Partial<Omit<CreateOrgEmployeeInput, "company_id">>;

export interface OrgTreeNode {
  id: string;
  kind: "company" | "department" | "employee";
  name: string;
  title: string | null;
  job_level: string | null;
  team_size: number | null;
  is_key: boolean;
  children: OrgTreeNode[];
}

export interface OrgImportEmployee {
  name: string;
  alias: string | null;
  title: string | null;
  job_level: string | null;
  report_to_name: string | null;
  department_name: string | null;
  subordinate_count: number | null;
  team_size: number | null;
  remark: string | null;
}

export interface OrgImportDepartment {
  name: string;
  parent_name: string | null;
  leader_name: string | null;
  team_size: number | null;
  business_direction: string | null;
}

export interface OrgImportDraft {
  company_name: string;
  departments: OrgImportDepartment[];
  employees: OrgImportEmployee[];
}

export interface OrgClarificationQuestion {
  question: string;
  field: string | null;
  hint: string | null;
}

export interface OrgParseResult {
  draft: OrgImportDraft | null;
  questions: OrgClarificationQuestion[];
}

export interface OrgImportWordResponse {
  result: OrgParseResult;
  source_text: string;
}

export interface BindEmployeeResult {
  employee_id: string;
  matched: boolean;
  candidate_id: string | null;
  candidate_name: string | null;
  name_mismatch: boolean;
  current_revision_id?: string | null;
}

export interface BdLead {
  id: string;
  source: string;
  company_name: string;
  job_title: string | null;
  raw_snippet: string | null;
  url: string | null;
  status: string;
}

export interface BdEvidenceItem {
  claim: string | null;
  quote: string | null;
  source_url: string | null;
}

export interface BdPoolCandidate {
  candidate_id: string;
  name: string;
  phone: string | null;
  revision_id: string;
}

export interface BdAgentLead {
  id: string;
  source: string;
  company_name: string;
  job_title: string | null;
  posted_time: string | null;
  salary_range: string | null;
  level: string | null;
  requirements: string[];
  summary: string | null;
  url: string | null;
  status: string;
  confidence: number | null;
  is_hiring: boolean | null;
  evidence: BdEvidenceItem[];
}

export interface BdAgentQueryResult {
  session_id: string;
  leads: BdAgentLead[];
  /** 非空表示这一轮被降级/失败（如线索综合超时）；空结果据此与「确实没搜到」区分。 */
  degraded_reason: string | null;
}

export interface BdProgress {
  stage: string;
  message: string;
}

export interface CaseItem {
  id: string;
  // 候选人或岗位被物理删除后置空，此时名称回落到快照字段。
  candidate_id: string | null;
  jd_id: string | null;
  stage: string;
  note: string | null;
  candidate_name?: string | null;
  company?: string | null;
  jd_title?: string | null;
  can_advance?: boolean;
  blocked_reason?: string | null;
  candidate_deleted?: boolean;
  jd_deleted?: boolean;
  last_event?: string | null;
  last_event_at?: string | null;
}

export interface CaseActionInput {
  occurred_at?: string;
  note?: string;
  idempotency_key?: string;
}

export interface DashboardFilters {
  company?: string;
  jd_id?: string;
  date_from?: string;
  date_to?: string;
}

export interface CaseRoundItem {
  id: string;
  round_no: number;
  round_name: string;
  round_type: string | null;
  skipped: boolean;
}

export interface CaseEventItem {
  id: string;
  event_type: string;
  case_round_id: string | null;
  round_name: string | null;
  occurred_at: string;
  recorded_at: string;
  result: string | null;
  note: string | null;
  status: string;
}

export interface CaseDetail extends CaseItem {
  rounds: CaseRoundItem[];
  events: CaseEventItem[];
  process_rounds?: { round_no: number; round_name: string; round_type?: string | null }[];
  template_version?: number | null;
}

export interface DashboardOverview {
  recommendation_total: number;
  offer_total: number;
  active_offer_total: number;
  onboarded_total: number;
  candidate_total: number;
  monthly_new_candidates: { month: string; count: number }[];
}

export interface DashboardRound {
  round_key?: string;
  round_no: number;
  round_name: string;
  entered: number;
  judged: number;
  passed: number;
  failed: number;
  pending: number;
  skipped: number;
  exited: number;
  cancelled: number;
  pass_rate: number | null;
}

export interface DashboardByJd {
  jd_id: string;
  company: string;
  title: string;
  recommendation_total: number;
  offer_total: number;
  final_offer_rate: number | null;
  rounds: DashboardRound[];
}

export interface DashboardTrendItem {
  period: string;
  recommendation: number;
  offer: number;
}

export interface DailyFollowupItem {
  name: string;
  company: string;
  title: string;
  date?: string;
  time?: string;
  /** 勾选记录的稳定键（形如 `followup:<case_id>`），与展示字段无关。 */
  item_key: string;
  /** 今天是否已处理；不代表任务完成，次日自动重置。 */
  done: boolean;
}

export interface DailyFollowupToday {
  followup: DailyFollowupItem[];
  interview: DailyFollowupItem[];
  /** 「我的提醒」：使用者从人才库建立的待办，无日期，勾选即完成并移出。 */
  reminders: CandidateReminderItem[];
}

export interface CandidateReminderItem {
  id: string;
  candidate_id: string;
  /** 建立时的人名快照：候选人改名或删除后仍显示这个名字。 */
  name: string;
  content: string;
  done: boolean;
}

export interface ReverseMatchItem {
  jd_id: string;
  revision_id: string;
  company: string;
  title: string;
  score: number;
}

export interface CorrectionRecord {
  correction_id: string;
  entity_type: string;
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  reverted: boolean;
}

export interface VendorPreset {
  key: string;
  label: string;
  base_url: string;
  text_model?: string;
  vision_model?: string;
  embedding_model?: string;
  rerank_model?: string;
}

export interface AppSettings {
  deepseek_api_key?: string;
  deepseek_base_url?: string;
  siliconflow_api_key?: string;
  siliconflow_base_url?: string;
  siliconflow_embedding_model?: string;
  siliconflow_reranker_model?: string;
  tavily_api_key?: string;
  tavily_base_url?: string;
  text_base_url?: string;
  text_model?: string;
  text_api_key?: string;
  vision_base_url?: string;
  vision_model?: string;
  vision_api_key?: string;
  embedding_base_url?: string;
  embedding_model?: string;
  embedding_api_key?: string;
  rerank_base_url?: string;
  rerank_model?: string;
  rerank_api_key?: string;
  imap_host?: string;
  imap_account?: string;
  imap_auth_code?: string;
  imap_whitelist?: string;
  mail_auto_sync?: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_account?: string;
  smtp_auth_code?: string;
  smtp_ssl?: boolean;
  reminder_to?: string;
  daily_followup_enabled?: boolean;
}

export interface BackupSnapshot {
  filename: string;
  path: string;
  size_bytes: string;
  created: string;
}

export interface ReminderItem {
  id: string;
  title: string;
  note: string | null;
  remind_at: string;
  dismissed: boolean;
  dismissed_at: string | null;
  case_id?: string | null;
  paused_by_workflow?: boolean;
}

export interface CreateReminderInput {
  title: string;
  remind_at: string;
  note?: string;
  case_id?: string | null;
}

export interface MigrationReport {
  target_root: string;
  files_copied: number;
  files_verified: number;
  candidate_count: number;
  ok: boolean;
}

export interface OnboardingStatus {
  data_root: string;
  llm_enabled: boolean;
  search_enabled: boolean;
  bd_search_enabled: boolean;
  mail_enabled: boolean;
  smtp_enabled: boolean;
  health: Record<string, { status: string; message?: string }>;
}

export interface ProviderCheck {
  name: string;
  ok: boolean;
  message: string;
}

export interface RecruitmentApi {
  importResume(file: File): Promise<ImportedResume>;
  importFolder(directory: string): Promise<{ imported: ImportedResume[]; skipped: string[]; errors: string[] }>;
  getTask(taskId: string): Promise<TaskStatus>;
  listTasks(): Promise<TaskStatus[]>;
  getTaskStatusBatch(taskIds: string[]): Promise<{ found: TaskStatus[]; missing_ids: string[] }>;
  controlTask(taskId: string, action: TaskAction): Promise<TaskStatus>;
  triggerBackfill(kind: "school-mappings" | "candidate-profiles" | "jd-profiles" | "reparse-failed"): Promise<{ task_id: string; task_type: string }>;
  listResumeRevisions(candidateId: string): Promise<ResumeRevision[]>;
  switchResumeRevision(revisionId: string): Promise<ResumeRevision>;
  reparseResume(revisionId: string, forceOcr: boolean, useVision: boolean): Promise<{ revision_id: string; task_id: string }>;
  getResumeReview(revisionId: string): Promise<ResumeReview>;
  indexStatus(): Promise<IndexSyncStatus>;
  retryIndexSync(): Promise<IndexSyncStatus>;
  downloadResume(revisionId: string, filename: string): Promise<void>;
  previewResume(revisionId: string): Promise<string>;
  viewResume(revisionId: string): Promise<{ kind: "opened" | "preview"; filename: string; url?: string }>;
  searchCandidates(query: string, filters?: CandidateSearchFilters, options?: CandidateSearchOptions): Promise<CandidateSearchResult>;
  listCandidates(): Promise<CandidateListItem[]>;
  listCandidatesPage?(page: number, pageSize: number): Promise<CandidatePage>;
  listDirectionPending(): Promise<CandidateListItem[]>;
  importJd(input: { company: string; title: string; sourceText: string }): Promise<ImportedJd>;
  importJdFile(file: File, company: string, title: string): Promise<ImportedJd>;
  importJdBatch(sourceText: string): Promise<{ imported: ImportedJd[] }>;
  importJdBatchFile(file: File): Promise<{ imported: ImportedJd[] }>;
  listJds(): Promise<JdListItem[]>;
  listJdsPage(page: number, pageSize: number, filter?: { title?: string; company?: string; status?: string }): Promise<JdPage>;
  updateJdStatus(jdId: string, status: string): Promise<{ jd_id: string; status: string }>;
  updateJdField(jdId: string, field: string, value: unknown, extra?: { points?: ProfilePointData[]; compact?: string | null }): Promise<{ jd_id: string; revision_id: string; field: string; value: unknown }>;
  updateJdParsed(jdId: string, parsedData: JdParsedData): Promise<{ jd_id: string; revision_id: string; field: string; value: unknown }>;
  regenerateJdProfile(jdId: string, instruction?: string, onStage?: (progress: ProfileGenerationProgress) => void): Promise<{ generated: boolean; summary?: string; points?: ProfilePointData[]; compact?: string | null; input_hash?: string; constraints?: JdExactConstraint[] }>;
  parseJdConstraints(sourceText: string): Promise<{ constraints: JdExactConstraint[]; min_years: number | null; years_stated: boolean }>;
  matchJd(revisionId: string, limit?: number, mode?: "keyword" | "vector" | "hybrid"): Promise<MatchRun>;
  startAiReview(runId: string, reasoning?: boolean): Promise<{ review_id: string; status: string }>;
  getAiReview(runId: string): Promise<{ status: string; progress: number; result_ref: string | null; error_message: string | null }>;
  startSearchReview(payload: { query: string; filters?: CandidateSearchFilters; candidate_ids: string[]; reasoning?: boolean }): Promise<{ review_id: string; status: string; query_key: string }>;
  getSearchReview(reviewId: string): Promise<SearchReviewStatus>;
  matchBatch(revisionIds: string[], limit?: number, mode?: "keyword" | "vector" | "hybrid"): Promise<{ results: BatchMatchResult[] }>;
  markMatchResult(resultId: string, status: MatchMarkStatus): Promise<{ result_id: string; status: MatchMarkStatus }>;
  listMatchResults(): Promise<{ groups: MatchResultGroup[] }>;
  listMatchResultsForCandidate(candidateId: string): Promise<MatchCandidateItem[]>;
  matchCandidate(candidateId: string, mode?: "keyword" | "vector" | "hybrid"): Promise<CandidateMatchResult>;
  createCaseFromMatchResult(resultId: string): Promise<{ case_id: string; result_id: string; status: string }>;
  exportMatchJd(revisionId: string): Promise<void>;
  health(): Promise<Record<string, { status: string; message?: string }>>;
  diagnostics(): Promise<DiagnosticsData>;
  exportDiagnostics(): Promise<void>;
  listMappingProjects(): Promise<MappingProject[]>;
  createMappingProject(name: string, description?: string): Promise<MappingProject>;
  buildMappingTree(projectId: string, text: string, label?: string): Promise<MappingSnapshot>;
  listMappingSnapshots(projectId: string): Promise<MappingSnapshot[]>;
  getMappingTree(snapshotId: string): Promise<MappingTreeNode[]>;
  searchBdLeads(query: string, limit?: number): Promise<BdLead[]>;
  searchLeadsForCandidate(candidateId: string, limit?: number): Promise<BdLead[]>;
  updateLeadStatus(leadId: string, status: string, note?: string): Promise<BdLead>;
  runBdAgent(query: string, kind?: string, limit?: number): Promise<BdAgentQueryResult>;
  runBdAgentStream(query: string, kind?: string, limit?: number, onProgress?: (progress: BdProgress) => void, onLeads?: (leads: BdAgentLead[]) => void): Promise<BdAgentQueryResult>;
  followUpBdAgent(sessionId: string, query: string, limit?: number): Promise<BdAgentQueryResult>;
  lookupPool(leadId: string): Promise<BdPoolCandidate[]>;
  createCase(candidateId: string, jdId: string): Promise<CaseItem>;
  listCasesPage(page: number, pageSize: number, jdId?: string): Promise<CasePage>;
  getCase(caseId: string): Promise<CaseDetail>;
  deleteCase(caseId: string): Promise<{ deleted: string }>;
  recommendCase(caseId: string, payload?: CaseActionInput): Promise<CaseEventItem>;
  enterInterview(caseId: string, payload?: CaseActionInput & { round_name?: string; round_type?: string }): Promise<CaseEventItem>;
  recordResult(caseId: string, caseRoundId: string, result: string, payload?: CaseActionInput): Promise<CaseEventItem>;
  passAndAdvance(caseId: string, caseRoundId: string, payload?: CaseActionInput & { next_round_name?: string }): Promise<CaseEventItem[]>;
  offerCase(caseId: string, payload?: CaseActionInput): Promise<CaseEventItem>;
  onboardCase(caseId: string, payload?: CaseActionInput): Promise<CaseEventItem>;
  exitCase(caseId: string, result?: string, payload?: CaseActionInput): Promise<CaseEventItem>;
  voidEvent(eventId: string, payload?: CaseActionInput): Promise<{ deleted: string }>;
  dashboardOverview(filters?: { company?: string; jd_id?: string; date_from?: string; date_to?: string }): Promise<DashboardOverview>;
  dashboardByJd(filters?: { company?: string; jd_id?: string; date_from?: string; date_to?: string }): Promise<DashboardByJd[]>;
  dashboardTrend(granularity: string, filters?: { company?: string; jd_id?: string; date_from?: string; date_to?: string }): Promise<DashboardTrendItem[]>;
  dashboardExport(filters?: { company?: string; jd_id?: string; date_from?: string; date_to?: string }): Promise<void>;
  dailyFollowupToday(): Promise<DailyFollowupToday>;
  checkDailyTodo(input: { item_key: string; done: boolean }): Promise<{ date: string; item_key: string; done: boolean }>;
  createCandidateReminder(input: { candidate_id: string; content: string }): Promise<CandidateReminderItem>;
  completeCandidateReminder(reminderId: string): Promise<CandidateReminderItem>;
  reverseMatch(candidateId: string, mode?: "keyword" | "vector" | "hybrid"): Promise<ReverseMatchItem[]>;
  getCandidateContact(candidateId: string): Promise<CandidateContact>;
  updateCandidateContact(candidateId: string, input: { email: string | null; phone: string | null }): Promise<CandidateContact>;
  updateCandidateField(candidateId: string, field: string, value: unknown, extra?: { points?: ProfilePointData[]; compact?: string | null }): Promise<{ candidate_id: string; revision_id: string; field: string; value: unknown }>;
  updateCommunicationNote(candidateId: string, note: string): Promise<{ candidate_id: string; communication_note: string | null }>;
  updateCandidateParsed(candidateId: string, parsedData: ParsedResumeData): Promise<{ candidate_id: string; revision_id: string; updated_fields: string[] }>;
  regenerateCandidateProfile(candidateId: string, instruction?: string, onStage?: (progress: ProfileGenerationProgress) => void): Promise<{ generated: boolean; summary?: string; points?: ProfilePointData[]; compact?: string | null; input_hash?: string }>;
  deleteCandidate(candidateId: string): Promise<{ candidate_id: string; deleted: boolean }>;
  deleteJd(jdId: string): Promise<{ jd_id: string; deleted: boolean }>;
  bulkDeleteCandidates(candidateIds: string[]): Promise<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>;
  bulkForceOcr(revisionIds: string[]): Promise<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>;
  bulkReparse(revisionIds: string[]): Promise<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>;
  bulkDownloadCandidates(candidateIds: string[]): Promise<void>;
  bulkMatchCandidates(candidateIds: string[], mode?: "keyword" | "vector" | "hybrid"): Promise<BulkCandidateMatchResult>;
  bulkDeleteJds(jdIds: string[]): Promise<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>;
  bulkDeleteCases(caseIds: string[]): Promise<{ results: { entity_id: string; ok: boolean; error: string | null }[]; succeeded: number; failed: number }>;
  applyCorrection(input: { entityType: string; entityId: string; fieldName: string; newValue: string | null; reason?: string }): Promise<CorrectionRecord>;
  undoCorrection(correctionId: string): Promise<CorrectionRecord>;
  exportMappingTree(snapshotId: string): Promise<void>;
  exportMappingTreePdf(snapshotId: string): Promise<void>;
  getSettings(): Promise<AppSettings>;
  getVendors(): Promise<VendorPreset[]>;
  getAiCatalog(): Promise<AiCatalog>;
  refreshAiCatalog(): Promise<{ status: string; active_version: number; message: string }>;
  getAiConfig(): Promise<AiConfig>;
  updateAiConfig(update: AiConfigUpdate): Promise<AiConfig>;
  probeAiConnection(input: { provider_id: string; api_key?: string; connection_id?: string | null; base_url_override?: string | null; parameter_style?: string | null; models?: Record<string, string> }): Promise<ConnectionProbeReport>;
  getAiStatus(): Promise<AiStatus>;
  updateSettings(values: Partial<AppSettings>): Promise<AppSettings>;
  testMail(): Promise<{ imap: { ok: boolean; message: string }; smtp: { ok: boolean; message: string } }>;
  sendMailConfirmation(): Promise<{ sent: boolean; to: string; message: string }>;
  sendFollowupTest(): Promise<{ sent: boolean; to: string; message: string }>;
  syncMail(): Promise<{ ingested: number; revision_ids: string[] }>;
  mailStatus(): Promise<{ configured: boolean; last_uid: number }>;
  exportMatchRun(runId: string): Promise<void>;
  listBackups(): Promise<BackupSnapshot[]>;
  createBackup(label?: string): Promise<{ filename: string; path: string }>;
  restoreBackup(filename: string): Promise<{ restored_from: string; safety_backup: string; restart_required?: boolean; status?: string }>;
  createPortableBackup(targetPath: string, passphrase: string): Promise<{ path: string; same_volume: boolean }>;
  restorePortableBackup(backupPath: string, targetRoot: string, passphrase: string): Promise<{ target_root: string; files_restored: number; files_verified: number; ok: boolean }>;
  listReminders(): Promise<ReminderItem[]>;
  createReminder(input: CreateReminderInput): Promise<ReminderItem>;
  dismissReminder(id: string): Promise<ReminderItem>;
  migrateData(targetRoot: string): Promise<MigrationReport>;
  setDataRoot(path: string): Promise<string>;
  onboardingStatus(): Promise<OnboardingStatus>;
  testProviders(): Promise<ProviderCheck[]>;
  listCompanies(): Promise<OrgCompany[]>;
  createCompany(name: string): Promise<OrgCompany>;
  updateCompany(companyId: string, name: string): Promise<OrgCompany>;
  listDepartments(companyId: string): Promise<OrgDepartment[]>;
  createDepartment(input: CreateOrgDepartmentInput): Promise<OrgDepartment>;
  listEmployees(companyId: string): Promise<OrgEmployee[]>;
  createEmployee(input: CreateOrgEmployeeInput): Promise<OrgEmployee>;
  getOrgTree(companyId: string): Promise<OrgTreeNode>;
  exportOrgInternal(companyId: string): Promise<void>;
  exportOrgClient(companyId: string): Promise<void>;
  exportOrgArchPdf(companyId: string): Promise<void>;
  updateDepartment(departmentId: string, changes: UpdateOrgDepartmentInput): Promise<OrgDepartment>;
  deleteDepartment(departmentId: string): Promise<void>;
  updateEmployee(employeeId: string, changes: UpdateOrgEmployeeInput): Promise<OrgEmployee>;
  deleteEmployee(employeeId: string): Promise<void>;
  deleteCompany(companyId: string): Promise<void>;
  parseOrgImport(text: string): Promise<OrgParseResult>;
  parseOrgWord(file: File): Promise<OrgImportWordResponse>;
  answerOrgImport(text: string, answers: string[]): Promise<OrgParseResult>;
  commitOrgImport(companyId: string, draft: OrgImportDraft, sourceText?: string | null): Promise<{ departments: number; employees: number }>;
  reviseOrgImport(draft: OrgImportDraft, instruction: string): Promise<OrgImportDraft>;
  getCompanySource(companyId: string): Promise<{ company_id: string; source_text: string | null }>;
  bindEmployee(employeeId: string, phone: string, name?: string | null): Promise<BindEmployeeResult>;
}


const navigation = ["人才库", "JD 管理", "流程中", "数据看板", "Mapping", "BD 助手", "设置"];

const CANDIDATE_PAGE_SIZE_DEFAULT = 20;

/** AI 复核轮询的退避档位（毫秒）与总时长上限：避免「一秒一次」刷出上千个请求。 */
const REVIEW_POLL_BACKOFF_MS = [1000, 2000, 5000];
const REVIEW_POLL_LIMIT_MS = 15 * 60 * 1000;

const STAGES = [
  "待评估", "待联系", "已联系", "有意向", "已推荐",
  "初试", "复试", "终试", "Offer", "入职",
  "客户拒绝", "候选人拒绝", "暂缓", "岗位关闭"
];


function sortLeadsByConfidence(leads: BdAgentLead[]): BdAgentLead[] {
  return [...leads].sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1));
}


function filterOrgTree(
  root: OrgTreeNode,
  search: string,
  filterKind: string,
  filterKey: boolean,
): OrgTreeNode | null {
  const q = search.trim().toLowerCase();
  if (!q && !filterKind && !filterKey) return root;

  function matches(node: OrgTreeNode): boolean {
    if (filterKind && node.kind !== "company" && node.kind !== filterKind) return false;
    if (filterKey && node.kind === "employee" && !node.is_key) return false;
    if (!q) return true;
    const hay = [node.name, node.title, node.job_level].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  }

  function walk(node: OrgTreeNode): OrgTreeNode | null {
    const children = node.children
      .map(walk)
      .filter((child): child is OrgTreeNode => child !== null);
    if (matches(node) || children.length > 0) {
      return { ...node, children };
    }
    return null;
  }

  return walk(root);
}

function qsBand(rank: number | null | undefined): string {
  if (rank == null) return "";
  if (rank <= 50) return "前50";
  if (rank <= 100) return "前100";
  if (rank <= 150) return "前150";
  if (rank <= 200) return "前200";
  if (rank <= 300) return "前300";
  return "前300之后";
}

function schoolLevelLabel(level: string | null | undefined): string {
  const map: Record<string, string> = {
    "985": "985",
    "211": "211",
    "双一流": "双一流",
    "普通": "普通",
    "海外": "海外",
  };
  return level ? (map[level] ?? level) : "";
}

function eduLabel(p: ParsedResumeData | null | undefined): string {
  if (!p) return "";
  const year = p.graduation_year ? String(p.graduation_year).slice(-2) : "";
  const levelMap: Record<string, string> = { "985": "9", "211": "2", "双一流": "双", "普通": "普", "海外": "海" };
  const level = p.school_level ? (levelMap[p.school_level] ?? p.school_level) : "";
  const degreeMap: Record<string, string> = {
    "博士": "博", "硕士": "硕", "本科": "本", "大专": "专",
    "DOCTORATE": "博", "MASTER": "硕", "BACHELOR": "本", "ASSOCIATE": "专",
  };
  const degree = p.highest_degree ? (degreeMap[p.highest_degree] ?? p.highest_degree) : "";
  const qs = qsBand(p.qs_rank);
  const core = [year, level + degree].filter(Boolean).join("-");
  return qs ? `${core}+qs${qs}` : core;
}

function splitList(text: string): string[] {
  return text.split(/[、,，/]/).map((s) => s.trim()).filter(Boolean);
}

/** 生效条件字段 → 精确筛选面板草稿字段；缺失表示面板没有对应项（如多值公司），只能整条移除。 */
const CONDITION_DRAFT_KEYS: Record<string, keyof SearchFilterDraft> = {
  min_years: "minYears", max_years: "maxYears", min_age: "minAge", max_age: "maxAge",
  highest_degree: "degree", locations: "locations", preferred_locations: "preferredLocations",
  school_level: "schoolLevel", max_qs_rank: "maxQsRank", exclude_skills: "excludeSkills",
  phone: "phone", gender: "gender", name: "name", company: "company", title: "title",
  communication_note: "communicationNote", school: "school", school_region: "schoolRegion", career_directions: "careerDirections",
  career_specializations: "careerSpecializations", business_directions: "businessDirections",
};

/** 后端以列表形式接收的筛选字段：压掉它们时要下发空数组而不是 null。 */
const CONDITION_LIST_FIELDS = new Set([
  "locations", "preferred_locations", "exclude_skills", "specializations",
  "career_directions", "career_specializations", "business_directions", "companies",
]);

function patchParsed(parsed: ParsedResumeData | null, field: string, value: unknown): ParsedResumeData {
  const next = { ...(parsed ?? {}) } as ParsedResumeData;
  (next as Record<string, unknown>)[field] = value;
  // 人工编辑画像后，本地同步后端状态：manual、清空哈希、不视为过期。
  if (field === "ai_profile_summary") {
    next.ai_profile_source = "manual";
    next.ai_profile_input_hash = null;
    next.ai_profile_stale = false;
  }
  return next;
}

function patchCandidate(item: CandidateListItem, field: string, value: unknown): CandidateListItem {
  const next: CandidateListItem = { ...item, parsed_data: patchParsed(item.parsed_data, field, value) };
  if (field === "name" && value) next.display_name = value as string;
  if (field === "phone") next.phone = (value as string) || null;
  return next;
}

function patchSearchItem(item: CandidateSearchItem, field: string, value: unknown): CandidateSearchItem {
  const next: CandidateSearchItem = { ...item, parsed_data: patchParsed(item.parsed_data, field, value) };
  if (field === "name") next.name = (value as string) || "";
  if (field === "phone") next.phone = (value as string) || null;
  if (field === "total_years") next.total_years = typeof value === "number" ? value : null;
  if (field === "highest_degree") next.highest_degree = (value as string) || null;
  if (field === "location") next.location = (value as string) || null;
  return next;
}

export function App({ api }: { api: RecruitmentApi }) {
  const [activeNav, setActiveNav] = useState(0);

  // 人才库
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState<"keyword" | "vector" | "hybrid">(() => {
    const saved = localStorage.getItem("search-mode:v1");
    return saved === "keyword" || saved === "vector" || saved === "hybrid" ? saved : "hybrid";
  });
  useEffect(() => {
    localStorage.setItem("search-mode:v1", searchMode);
  }, [searchMode]);
  const [keywordOperator, setKeywordOperator] = useState<CandidateKeywordOperator>(() => {
    const saved = localStorage.getItem("search-keyword-operator:v1");
    return saved === "and" || saved === "or" ? saved : "smart";
  });
  useEffect(() => {
    localStorage.setItem("search-keyword-operator:v1", keywordOperator);
  }, [keywordOperator]);
  const [rewriteEnabled, setRewriteEnabled] = useState<boolean>(() => {
    return localStorage.getItem("search-rewrite-enabled:v1") === "true";
  });
  useEffect(() => {
    localStorage.setItem("search-rewrite-enabled:v1", String(rewriteEnabled));
  }, [rewriteEnabled]);
  const [searchBody, setSearchBody] = useState<boolean>(() => {
    return localStorage.getItem("search-body:v1") === "true";
  });
  useEffect(() => {
    localStorage.setItem("search-body:v1", String(searchBody));
  }, [searchBody]);
  const [parseEnabled, setParseEnabled] = useState<boolean>(() => {
    return localStorage.getItem("search-parse-enabled:v1") === "true";
  });
  useEffect(() => {
    localStorage.setItem("search-parse-enabled:v1", String(parseEnabled));
  }, [parseEnabled]);
  const [visibleColumns, setVisibleColumns] = useState<Record<CandidateColumnKey, boolean>>(() => {
    try {
      const saved = localStorage.getItem("candidate-table-columns:v1");
      if (saved) return { ...CANDIDATE_COLUMNS_DEFAULT, ...(JSON.parse(saved) as Record<CandidateColumnKey, boolean>) };
    } catch { /* ignore corrupted settings */ }
    return { ...CANDIDATE_COLUMNS_DEFAULT };
  });
  const [columnOrder, setColumnOrder] = useState<CandidateColumnKey[]>(() => {
    try {
      const saved = localStorage.getItem("candidate-table-column-order:v1");
      if (saved) {
        const parsed = JSON.parse(saved) as CandidateColumnKey[];
        // 合并新增的默认列：旧缓存缺少新列时，把新列追加到末尾，避免新列不显示。
        return [...parsed, ...CANDIDATE_COLUMNS_ORDER_DEFAULT.filter((key) => !parsed.includes(key))];
      }
    } catch { /* ignore corrupted settings */ }
    return [...CANDIDATE_COLUMNS_ORDER_DEFAULT];
  });
  const [columnsMenuOpen, setColumnsMenuOpen] = useState(false);
  const [educationEditor, setEducationEditor] = useState<{ candidateId: string; educations: ParsedEducationData[] } | null>(null);
  const [profileEditor, setProfileEditor] = useState<{ candidateId: string; summary: string | null; source: string | null; stale: boolean; points?: ProfilePointData[]; compact?: string | null } | null>(null);
  const [jdProfileEditor, setJdProfileEditor] = useState<{ jdId: string; title: string; profile: string | null; constraints: JdExactConstraint[]; points?: ProfilePointData[]; compact?: string | null } | null>(null);
  // JD 列表页「重新生成」拿到的是真双形态，但稍后才在弹窗里保存；
  // 这里按 jdId 暂存，保存时若画像正文未被改动则一并透传，避免退化为按标点伪拆点。
  const jdDualFormRef = useRef(new Map<string, { narrative: string | null; points: ProfilePointData[]; compact: string | null; constraints?: JdExactConstraint[] }>());
  const [candidateParsedEditor, setCandidateParsedEditor] = useState<{ candidateId: string; parsed: ParsedResumeData | null } | null>(null);
  const [jdParsedEditor, setJdParsedEditor] = useState<{ jdId: string; title: string; parsed: JdParsedData | null } | null>(null);

  async function saveEducations(educations: ParsedEducationData[]) {
    if (!educationEditor) return;
    const candidateId = educationEditor.candidateId;
    // API 成功后才关闭弹窗；失败抛异常，由编辑器保留输入并显示错误。
    await updateCandidateFieldValue(candidateId, "educations", educations);
    setEducationEditor(null);
  }

  async function saveProfile(summary: string) {
    if (!profileEditor) return;
    const candidateId = profileEditor.candidateId;
    // 若画像是刚通过「重新生成」得到的真双形态且正文未被改动，则把同源分点/浓缩一并透传落库。
    const dual = profileEditor.points?.length && summary === profileEditor.summary
      ? { points: profileEditor.points, compact: profileEditor.compact ?? null }
      : undefined;
    await updateCandidateFieldValue(candidateId, "ai_profile_summary", summary || null, dual);
    // 同步更新「匹配候选人」表格（jdMatch）中的画像，避免从 JD 匹配表格打开时保存后显示不刷新。
    setJdMatch((cur) => cur ? {
      ...cur,
      items: cur.items.map((it) => it.candidate_id === candidateId
        ? {
            ...it,
            parsed_data: {
              ...(it.parsed_data ?? {}),
              ai_profile_summary: summary || null,
              ai_profile_source: "manual",
              ai_profile_input_hash: null,
              ai_profile_stale: false,
            },
          }
        : it),
    } : cur);
    setProfileEditor(null);
  }

  async function regenerateProfile(instruction = "", onStage?: (progress: ProfileGenerationProgress) => void) {
    if (!profileEditor) return null;
    const result = await api.regenerateCandidateProfile(profileEditor.candidateId, instruction, onStage);
    const summary = result.summary ?? null;
    // 仅更新本地预览，不落库、不重建索引、不更新列表；由「保存」按钮提交。
    setProfileEditor((cur) => (cur ? { ...cur, summary, source: "ai", stale: false, points: result.points, compact: result.compact ?? null } : cur));
    return summary;
  }

  function toggleCandidateColumn(key: CandidateColumnKey) {
    setVisibleColumns((current) => {
      const next = { ...current, [key]: !current[key] };
      localStorage.setItem("candidate-table-columns:v1", JSON.stringify(next));
      return next;
    });
  }

  function moveCandidateColumn(fromKey: CandidateColumnKey, toKey: CandidateColumnKey) {
    setColumnOrder((current) => {
      const fromIndex = current.indexOf(fromKey);
      const toIndex = current.indexOf(toKey);
      if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return current;
      const next = [...current];
      next.splice(fromIndex, 1);
      // 删除后，若目标在被删元素右侧，其下标左移一位；统一为「插入到目标列之前」。
      const insertIndex = fromIndex < toIndex ? toIndex - 1 : toIndex;
      next.splice(insertIndex, 0, fromKey);
      localStorage.setItem("candidate-table-column-order:v1", JSON.stringify(next));
      return next;
    });
  }
  const [results, setResults] = useState<CandidateSearchItem[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [searchConditions, setSearchConditions] = useState<SearchConditionView[]>([]);
  /** 硬筛筛空、已退化为软排的条件字段名（后端 `query_plan.relaxed_conditions`）。 */
  const [relaxedSearchFields, setRelaxedSearchFields] = useState<string[]>([]);
  /** AI 解析回显（词条 / 语义查询 / 未识别片段），仅作排障与调优展示。 */
  const [searchPlanEcho, setSearchPlanEcho] = useState<SearchPlanEcho | null>(null);
  /**
   * 用户手动删掉的生效条件：面板里没有值时随请求下发显式空值，
   * 让后端「显式值优先」把文本/AI 解析出的同类条件一起压掉（否则下次搜索会重新冒出来）。
   */
  const [removedConditions, setRemovedConditions] = useState<SearchConditionView[]>([]);
  const [candidates, setCandidates] = useState<CandidateListItem[]>([]);
  const [candidatePage, setCandidatePage] = useState(1);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [directionPending, setDirectionPending] = useState<CandidateListItem[]>([]);
  const [directionPendingOpen, setDirectionPendingOpen] = useState(false);
  /** 方向待核面板里每人暂存的大类多选结果（原生多选逐个触发 change，需显式确认后提交）。 */
  const [directionPendingDraft, setDirectionPendingDraft] = useState<Record<string, string[]>>({});
  const [candidatePageSize, setCandidatePageSize] = useState(CANDIDATE_PAGE_SIZE_DEFAULT);
  const candidateTotalPages = Math.max(1, Math.ceil(candidateTotal / candidatePageSize));
  const candidateListRef = useRef<HTMLDivElement | null>(null);
  const [searchFilterDraft, setSearchFilterDraft] = useState<SearchFilterDraft>({
    ...EMPTY_SEARCH_FILTER_DRAFT,
  });
  const [tasks, setTasks] = useState<TaskStatus[]>([]);
  const taskPolls = useRef(new Map<string, { controller: AbortController; promise: Promise<TaskStatus> }>());
  /** 复核轮询按 run 去重：重复点「AI 复核」复用同一个轮询，不再叠加请求。 */
  const reviewPolls = useRef(new Map<string, { controller: AbortController; promise: Promise<void> }>());
  /** 搜索侧复核轮询：同一个 review_id 只保留一个轮询。 */
  const searchReviewPolls = useRef(new Map<string, { controller: AbortController; promise: Promise<void> }>());
  const [folderPath, setFolderPath] = useState("");
  const [batchProgress, setBatchProgress] = useState<{ total: number; waiting: number; running: number; done: number; failed: number; percent: number } | null>(null);
  const [batchTaskIds, setBatchTaskIds] = useState<string[]>([]);
  const [batchTimedOut, setBatchTimedOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [indexRebuild, setIndexRebuild] = useState<{ state: IndexRebuildState; pending: number } | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewName, setPreviewName] = useState("");
  const [searching, setSearching] = useState(false);
  /** 搜索侧 AI 复核（亮点/风险点）：一次搜索条件对应一次复核。 */
  const [searchReview, setSearchReview] = useState<SearchReviewState | null>(null);

  // JD 管理
  const [jdSource, setJdSource] = useState("");
  const [jdResult, setJdResult] = useState<ImportedJd[]>([]);
  const [jds, setJds] = useState<JdListItem[]>([]);
  const [jdPageItems, setJdPageItems] = useState<JdListItem[]>([]);
  const [jdPage, setJdPage] = useState(1);
  const [jdTotal, setJdTotal] = useState(0);
  const jdTotalPages = Math.max(1, Math.ceil(jdTotal / 10));
  const [openJdSource, setOpenJdSource] = useState<Record<string, boolean>>({});
  const [jdFilter, setJdFilter] = useState({ title: "", company: "", status: "" });
  const [jdMatchMode, setJdMatchMode] = useState<"keyword" | "vector" | "hybrid">("hybrid");
  const [jdImportOpen, setJdImportOpen] = useState(false);

  // 匹配结果抽屉
  const [matchDrawer, setMatchDrawer] = useState<MatchDrawerState | null>(null);
  const [matchPage, setMatchPage] = useState(1);
  /** 批量抽屉里按候选人筛选；空串＝全部。 */
  const [drawerCandidateFilter, setDrawerCandidateFilter] = useState("");
  const [jdMatch, setJdMatch] = useState<{ title: string; company: string; location: string; run_id: string | null; items: CandidateSearchItem[]; hardFilters: { kind: string; alternatives: string[]; source_text: string }[]; relaxed: string[] } | null>(null);
  const [jdReview, setJdReview] = useState<AiReviewState | null>(null);
  const [jdReviewReasoning, setJdReviewReasoning] = useState(false);
  const [jdMatchingId, setJdMatchingId] = useState<string | null>(null);
  /** 抽屉里每个 run 的复核状态：批量匹配时每位候选人一个 run，按行触发、按 run 记录。 */
  const [runReviews, setRunReviews] = useState<Record<string, AiReviewState>>({});
  const [candidateReviewReasoning, setCandidateReviewReasoning] = useState(false);
  const [matchCaseIds, setMatchCaseIds] = useState<Record<string, string>>({});
  const [creatingCase, setCreatingCase] = useState(false);
  const creatingCaseRef = useRef(false);
  const [createCasePicker, setCreateCasePicker] = useState<{ candidateId: string; name: string } | null>(null);

  // 抽屉派生态：批量匹配时每行带自己的 run（按行复核），单人匹配时共用抽屉级 run。
  const drawerSources: MatchDrawerSource[] = matchDrawer?.mode === "jds" ? matchDrawer.sources ?? [] : [];
  const drawerRunIds = matchDrawer?.mode === "jds"
    ? Array.from(new Set(matchDrawer.items
        .map((item) => item.run_id ?? matchDrawer.run_id)
        .filter((id): id is string => Boolean(id))))
    : [];
  // 复核结论按 result_id 合并：批量时多个 run 的结论要一起参与排序与亮点/风险点展示。
  const drawerVerdicts: Record<string, ReviewVerdictItem> = {};
  for (const runId of drawerRunIds) {
    Object.assign(drawerVerdicts, reviewVerdictsByResultId(runReviews[runId]?.result_ref ?? null));
  }
  /** 复核进行中的 run：批量时逐行显示进度，单人时显示在抽屉头部。 */
  const reviewingRunIds = drawerRunIds.filter((runId) => {
    const review = runReviews[runId];
    return review != null && !["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"].includes(review.status);
  });
  // 单人匹配时抽屉只有一个 run：复核按钮放头部；批量时 run_id 为空，改由每行触发。
  const drawerSingleRunId = matchDrawer?.mode === "jds" ? matchDrawer.run_id : null;
  const drawerSingleReview = drawerSingleRunId ? runReviews[drawerSingleRunId] ?? null : null;
  const drawerItems: MatchDrawerJdItem[] = matchDrawer?.mode !== "jds"
    ? []
    : drawerCandidateFilter
      ? matchDrawer.items.filter((item) => item.candidate_id === drawerCandidateFilter)
      : matchDrawer.items;
  const drawerVerdictCounts = reviewVerdictCounts(drawerVerdicts);
  /** 复核失败的条数：单列出来，避免「全是待核」看不出复核有没有跑成功。 */
  const drawerVerdictFailed = drawerVerdictCounts.failed;

  // 多选批量操作（候选人按 candidate_id 去重；搜索、翻页、换每页条数时清空，见 submitSearch / loadCandidates）。
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  /** 勾选的人里落在当前搜索结果内的部分：搜索侧 AI 复核只对这些人生效。 */
  const selectedSearchCandidateIds = results
    .filter((item) => selectedCandidateIds.has(item.candidate_id))
    .map((item) => item.candidate_id);

  // 设置 / 健康
  const [health, setHealth] = useState<Record<string, { status: string; message?: string }> | null>(null);

  // 数据看板
  const [diagnostics, setDiagnostics] = useState<DiagnosticsData | null>(null);

  // Mapping（组织架构）
  const [companies, setCompanies] = useState<OrgCompany[]>([]);
  const [selectedCompanyId, setSelectedCompanyId] = useState<string | null>(null);
  const [companyName, setCompanyName] = useState("");
  const [departments, setDepartments] = useState<OrgDepartment[]>([]);
  const [employees, setEmployees] = useState<OrgEmployee[]>([]);
  const [orgTree, setOrgTree] = useState<OrgTreeNode | null>(null);
  const [selectedOrgNodeId, setSelectedOrgNodeId] = useState<string | null>(null);
  const [orgSearch, setOrgSearch] = useState("");
  const [orgFilterKind, setOrgFilterKind] = useState("");
  const [orgFilterKey, setOrgFilterKey] = useState(false);
  const [pendingEditId, setPendingEditId] = useState<string | null>(null);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [orgImportText, setOrgImportText] = useState("");
  const [orgImportFileName, setOrgImportFileName] = useState("");
  const [orgImportDraft, setOrgImportDraft] = useState<OrgImportDraft | null>(null);
  const [orgImportQuestions, setOrgImportQuestions] = useState<OrgClarificationQuestion[]>([]);
  const [orgImportAnswers, setOrgImportAnswers] = useState<string[]>([]);
  const [orgImportBusy, setOrgImportBusy] = useState(false);
  const [orgImportMessage, setOrgImportMessage] = useState("");
  const [orgReviseInstruction, setOrgReviseInstruction] = useState("");
  const [orgSource, setOrgSource] = useState<{ companyId: string; text: string | null } | null>(null);
  const undoStackRef = useRef<{ undo: () => Promise<void>; redo: () => Promise<void> }[]>([]);
  const redoStackRef = useRef<{ undo: () => Promise<void>; redo: () => Promise<void> }[]>([]);

  // BD 助手
  const [bdQuery, setBdQuery] = useState("");
  const [bdFollowUp, setBdFollowUp] = useState("");
  const [bdSessionId, setBdSessionId] = useState<string | null>(null);
  const [bdLeads, setBdLeads] = useState<BdAgentLead[]>([]);
  const [bdDegradedReason, setBdDegradedReason] = useState<string | null>(null);
  const [bdLoading, setBdLoading] = useState(false);
  const [bdProgress, setBdProgress] = useState<BdProgress | null>(null);
  const [bdPoolByLead, setBdPoolByLead] = useState<Record<string, BdPoolCandidate[]>>({});
  const [bdPoolBusyId, setBdPoolBusyId] = useState<string | null>(null);
  const [collapsedPool, setCollapsedPool] = useState<Record<string, boolean>>({});

  // 看板
  const [dashboard, setDashboard] = useState<DashboardOverview | null>(null);
  const [dashboardByJdData, setDashboardByJdData] = useState<DashboardByJd[]>([]);
  const [dashboardTrend, setDashboardTrend] = useState<DashboardTrendItem[]>([]);
  const [trendGranularity, setTrendGranularity] = useState("month");
  const [dashboardDraft, setDashboardDraft] = useState<DashboardFilters>({});
  const [dashboardFilters, setDashboardFilters] = useState<DashboardFilters>({});
  const [dashboardBusy, setDashboardBusy] = useState(false);
  const dashboardRequest = useRef(0);
  const [dailyFollowup, setDailyFollowup] = useState<DailyFollowupToday | null>(null);
  // 建提醒：目标候选人 + 保存中状态（提醒内容由弹窗自己维护）。
  const [reminderTarget, setReminderTarget] = useState<{ candidateId: string; name: string } | null>(null);
  const [reminderBusy, setReminderBusy] = useState(false);

  // 招聘流程面板
  const [caseDrawer, setCaseDrawer] = useState<CaseDetail | null>(null);
  const [resumeReview, setResumeReview] = useState<ResumeReview | null>(null);
  const [cases, setCases] = useState<CaseItem[]>([]);
  const [casePage, setCasePage] = useState(1);
  const [caseTotal, setCaseTotal] = useState(0);
  const caseTotalPages = Math.max(1, Math.ceil(caseTotal / 20));
  const [caseJdFilter, setCaseJdFilter] = useState("");

  // 设置
  const [settings, setSettings] = useState<AppSettings>({});
  const [vendors, setVendors] = useState<VendorPreset[]>([]);
  const [providerChecks, setProviderChecks] = useState<ProviderCheck[]>([]);
  const [aiApiMessage, setAiApiMessage] = useState("");
  // 生成式 AI（双服务）状态
  const [aiCatalog, setAiCatalog] = useState<AiCatalog | null>(null);
  const [aiConfig, setAiConfig] = useState<AiConfig | null>(null);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiMessage, setAiMessage] = useState("");
  const [aiWizard, setAiWizard] = useState<null | "primary" | "secondary">(null);
  const [aiAdvanced, setAiAdvanced] = useState(false);
  const [mailMessage, setMailMessage] = useState("");
  const [mailTestResult, setMailTestResult] = useState<{ imap: { ok: boolean; message: string }; smtp: { ok: boolean; message: string } } | null>(null);
  const [mailSyncMessage, setMailSyncMessage] = useState("");
  const [mailStatus, setMailStatus] = useState<{ configured: boolean; last_uid: number } | null>(null);
  const [mailWhitelistInput, setMailWhitelistInput] = useState("");
  const [dataRootInput, setDataRootInput] = useState("");
  const [dataRootMessage, setDataRootMessage] = useState("");

  // 备份与恢复
  const [backups, setBackups] = useState<BackupSnapshot[]>([]);
  const [portableBackupPath, setPortableBackupPath] = useState("");
  const [portableRestorePath, setPortableRestorePath] = useState("");
  const [portableRestoreTarget, setPortableRestoreTarget] = useState("");
  const [portablePassphrase, setPortablePassphrase] = useState("");
  const [portableMessage, setPortableMessage] = useState("");
  const [backupBusy, setBackupBusy] = useState(false);
  const [portableBusy, setPortableBusy] = useState(false);

  // 数据迁移
  const [migrationTarget, setMigrationTarget] = useState("");
  const [migrationReport, setMigrationReport] = useState<MigrationReport | null>(null);
  const [migrationBusy, setMigrationBusy] = useState(false);
  const [migrationMessage, setMigrationMessage] = useState("");
  const [backfillMessage, setBackfillMessage] = useState("");

  // 启动检查
  const [onboarding, setOnboarding] = useState<OnboardingStatus | null>(null);

  /** 由筛选草稿构造请求条件：搜索与「搜索侧 AI 复核」必须用同一套条件。 */
  function buildSearchFilters(extraRemoved: SearchConditionView[] = []): CandidateSearchFilters {
    const filters: CandidateSearchFilters = {};
    if (searchFilterDraft.minYears !== "") filters.min_years = Number(searchFilterDraft.minYears);
    if (searchFilterDraft.maxYears !== "") filters.max_years = Number(searchFilterDraft.maxYears);
    if (searchFilterDraft.minAge !== "") filters.min_age = Number(searchFilterDraft.minAge);
    if (searchFilterDraft.maxAge !== "") filters.max_age = Number(searchFilterDraft.maxAge);
    if (searchFilterDraft.degree) filters.highest_degree = searchFilterDraft.degree;
    if (searchFilterDraft.locations.trim()) filters.locations = splitList(searchFilterDraft.locations);
    if (searchFilterDraft.preferredLocations.trim()) filters.preferred_locations = splitList(searchFilterDraft.preferredLocations);
    if (searchFilterDraft.schoolLevel.trim()) filters.school_level = searchFilterDraft.schoolLevel.trim();
    if (searchFilterDraft.maxQsRank !== "") filters.max_qs_rank = Number(searchFilterDraft.maxQsRank);
    if (searchFilterDraft.excludeSkills.trim()) filters.exclude_skills = splitList(searchFilterDraft.excludeSkills);
    if (searchFilterDraft.phone.trim()) filters.phone = searchFilterDraft.phone.trim();
    if (searchFilterDraft.gender) filters.gender = searchFilterDraft.gender;
    if (searchFilterDraft.name.trim()) filters.name = searchFilterDraft.name.trim();
    if (searchFilterDraft.communicationNote.trim()) filters.communication_note = searchFilterDraft.communicationNote.trim();
    if (searchFilterDraft.company.trim()) filters.company = searchFilterDraft.company.trim();
    if (searchFilterDraft.title.trim()) filters.title = searchFilterDraft.title.trim();
    if (searchFilterDraft.school.trim()) filters.school = searchFilterDraft.school.trim();
    if (searchFilterDraft.schoolRegion) filters.school_region = searchFilterDraft.schoolRegion;
    if (searchFilterDraft.careerDirections.length) filters.career_directions = searchFilterDraft.careerDirections;
    if (searchFilterDraft.careerSpecializations.length) filters.career_specializations = searchFilterDraft.careerSpecializations;
    if (searchFilterDraft.businessDirections.length) filters.business_directions = searchFilterDraft.businessDirections;
    // 用户删掉的生效条件：面板没有值时补一个显式空值，让后端 `_merge_filters` 的
    // 「显式值优先」把文本/AI 解析出的同类条件一起压掉（后端用 exclude_unset 判断显式覆盖，
    // 所以键必须真的写进 filters）。
    for (const removed of [...removedConditions, ...extraRemoved]) {
      if (removed.field in filters) continue;
      (filters as Record<string, unknown>)[removed.field] = CONDITION_LIST_FIELDS.has(removed.field) ? [] : null;
    }
    return filters;
  }

  /** 删除一条生效条件：先清面板草稿里的对应值，面板清空后再下发显式空值压住解析结果。 */
  function removeSearchCondition(field: string, value: string) {
    const key = CONDITION_DRAFT_KEYS[field];
    const nextDraft = { ...searchFilterDraft };
    let keepsPanelValue = false;
    if (key) {
      const current = nextDraft[key];
      const entries = Array.isArray(current) ? current : splitList(current);
      const remaining = entries.filter((entry) => entry !== value);
      if (remaining.length !== entries.length) {
        nextDraft[key] = (Array.isArray(current) ? remaining : remaining.join("、")) as never;
      }
      keepsPanelValue = remaining.length > 0;
    }
    if (key && !keepsPanelValue) {
      setRemovedConditions((current) => (
        current.some((entry) => entry.field === field) ? current : [...current, { field, value, confidence: "inferred" }]
      ));
    }
    setSearchFilterDraft(nextDraft);
  }

  /** 恢复一条被删掉的生效条件：撤销显式空值，下次搜索按解析结果重新生效。 */
  function restoreSearchCondition(field: string) {
    setRemovedConditions((current) => current.filter((entry) => entry.field !== field));
  }

  /** 修改一条生效条件：把值回填到精确筛选面板草稿（面板优先语义不变），并撤销该字段的移除。 */
  function editSearchCondition(field: string, value: string) {
    const key = CONDITION_DRAFT_KEYS[field];
    if (!key) return;
    const current = searchFilterDraft[key];
    if (Array.isArray(current)) {
      if (!current.includes(value)) setSearchFilterDraft({ ...searchFilterDraft, [key]: [...current, value] as never });
    } else if (!splitList(current).includes(value)) {
      setSearchFilterDraft({ ...searchFilterDraft, [key]: [...splitList(current), value].join("、") as never });
    }
    setRemovedConditions((list) => list.filter((entry) => entry.field !== field));
  }

  function submitSearch() {
    return runSearch([]);
  }

  /**
   * 空结果自诊断：把推断出来的条件（规则/AI 解析，非面板手填）全部去掉后重搜。
   * 这些条件最可能是解析误判，去掉它们等于把「人明明在库里、条件把人筛没了」这一种空结果排掉。
   */
  function dropInferredConditionsAndSearch() {
    const dropped = new Map<string, SearchConditionView>();
    for (const condition of searchConditions) {
      if (condition.confidence === "inferred" && condition.source !== "panel" && !dropped.has(condition.field)) {
        dropped.set(condition.field, condition);
      }
    }
    const extraRemoved = [...dropped.values()];
    if (!extraRemoved.length) return;
    setRemovedConditions((current) => {
      const known = new Set(current.map((entry) => entry.field));
      return [...current, ...extraRemoved.filter((entry) => !known.has(entry.field))];
    });
    void runSearch(extraRemoved);
  }

  async function runSearch(extraRemoved: SearchConditionView[]) {
    if (!query.trim() && !Object.values(searchFilterDraft).some((value) => value !== "")) {
      setNotice("请输入搜索词或选择筛选条件。");
      return;
    }
    setSearching(true);
    setError(null);
    setNotice(null);
    try {
      const filters = buildSearchFilters(extraRemoved);
      const response = await api.searchCandidates(query.trim(), filters, {
        mode: searchMode,
        operator: keywordOperator,
        rewriteEnabled,
        searchBody,
        parseEnabled,
      });
      setHasSearched(true);
      setResults(response.items);
      // 结果集变了，旧的多选不再对应当前列表，清空避免误删/误操作。
      setSelectedCandidateIds(new Set());
      // 结果集与旧复核结论不再对应，清掉避免把上一次的亮点/风险点挂到新人身上。
      setSearchReview(null);
      const plan = response.query_plan;
      // 展示**合并后**的最终生效条件（含面板手填项）；旧 sidecar 没有该字段时退回解析侧结果。
      setSearchConditions(plan?.effective_conditions ?? plan?.parsed_conditions ?? []);
      setRelaxedSearchFields(plan?.relaxed_conditions ?? []);
      // 解析回显只在开启 AI 解析时展示，避免把规则解析的默认结果当成 AI 产物。
      setSearchPlanEcho(parseEnabled && plan ? {
        source: plan.parsed_plan_source ?? "rule",
        keywordTerms: plan.keyword_terms ?? plan.retained_keywords ?? "",
        semanticQuery: plan.semantic_query ?? null,
        unparsedTerms: plan.unparsed_terms ?? [],
      } : null);
      if (response.empty_reason === "index_not_ready") {
        setNotice("索引尚未就绪，请先导入并解析简历。");
      } else if (response.empty_reason === "service_error") {
        setError("检索服务暂不可用，请稍后重试。");
      }
      if (response.degraded_reasons.length > 0) {
        setNotice(`部分检索能力已降级：${describeDegraded(response.degraded_reasons)}`);
      } else if (response.query_plan?.rewrite_status === "unavailable") {
        setNotice("AI 改写不可用，已使用原搜索词。");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "搜索失败，请稍后重试");
    } finally {
      setSearching(false);
    }
  }

  function pollTask(taskId: string): Promise<TaskStatus> {
    const existing = taskPolls.current.get(taskId);
    if (existing) return existing.promise;
    const controller = new AbortController();
    const promise = waitForTask(() => api.getTask(taskId), (task) => {
      setTasks((current) => [task, ...current.filter((entry) => entry.id !== task.id)]);
    }, {
      signal: controller.signal,
      onError: () => setNotice("任务状态暂时无法获取，正在自动重试；后台任务不会因此停止。"),
    }).then(async (task) => {
      if (task.task_type === "PARSE_RESUME") await loadCandidates(1);
      if (task.task_type === "PARSE_JD") await loadJds();
      return task;
    }).finally(() => taskPolls.current.delete(taskId));
    taskPolls.current.set(taskId, { controller, promise });
    // Background callers need no rejection handler when the app unmounts.
    void promise.catch(() => {});
    return promise;
  }

  async function loadTasks() {
    setError(null);
    try {
      setTasks(await api.listTasks());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务列表加载失败");
    }
  }

  async function runBackfill(kind: "school-mappings" | "candidate-profiles" | "jd-profiles" | "reparse-failed") {
    setError(null);
    setBackfillMessage("");
    try {
      const triggered = await api.triggerBackfill(kind);
      setBackfillMessage(`已创建回填任务 ${triggered.task_id}，正在后台执行…`);
      await pollTask(triggered.task_id);
      setBackfillMessage(`回填任务 ${triggered.task_id} 已完成。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "回填任务创建失败");
    }
  }

  async function runReparseFailed() {
    setError(null);
    setNotice(null);
    try {
      const triggered = await api.triggerBackfill("reparse-failed");
      await pollTask(triggered.task_id);
      setNotice("已重新生成不合格项解析任务，正在后台执行，稍后请刷新查看。");
      await loadCandidates();
      await loadJdsPage();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重新生成不合格项任务创建失败");
    }
  }

  async function loadCandidates(page = 1, pageSize = candidatePageSize) {
    setError(null);
    try {
      if (api.listCandidatesPage) {
        const response = await api.listCandidatesPage(page, pageSize);
        setCandidates(response.items);
        setCandidatePage(response.page);
        setCandidateTotal(response.total);
      } else {
        const items = await api.listCandidates();
        setCandidates(items);
        setCandidatePage(1);
        setCandidateTotal(items.length);
      }
      // 翻页/换每页条数后列表内容已变，清空多选（批量删除保留失败项的路径不走这里）。
      setSelectedCandidateIds(new Set());
      candidateListRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "候选人列表加载失败");
    }
  }

  function scrollToCandidateListTop() {
    candidateListRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function changeCandidatePageSize(size: number) {
    setCandidatePageSize(size);
    // 改变每页条数后回到第 1 页，并用新条数立刻拉取（state 尚未更新，需显式传参）。
    void loadCandidates(1, size);
  }

  async function loadDirectionPending() {
    setError(null);
    try {
      setDirectionPending(await api.listDirectionPending());
      setDirectionPendingOpen(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "方向待核列表加载失败");
    }
  }

  async function setCandidateCareerDirections(candidateId: string, careerDirections: string[]) {
    if (careerDirections.length === 0) return;
    try {
      await updateCandidateFieldValue(candidateId, "career_directions", careerDirections);
      setDirectionPending((list) => list.filter((c) => c.candidate_id !== candidateId));
      setDirectionPendingDraft((draft) => {
        const next = { ...draft };
        delete next[candidateId];
        return next;
      });
    } catch {
      // updateCandidateFieldValue 已设置错误提示
    }
  }

  function closeSearchResults() {
    setResults([]);
    setHasSearched(false);
    setSearchConditions([]);
    setSearchPlanEcho(null);
    setRemovedConditions([]);
  }

  function resetSearchAndGoHome() {
    setQuery("");
    setResults([]);
    setHasSearched(false);
    setSearchConditions([]);
    setSearchPlanEcho(null);
    setRemovedConditions([]);
    setSearchFilterDraft({ ...EMPTY_SEARCH_FILTER_DRAFT });
    void loadCandidates(1);
  }

  async function updateCandidateFieldValue(
    candidateId: string,
    field: string,
    value: unknown,
    extra?: { points?: ProfilePointData[]; compact?: string | null }
  ) {
    setError(null);
    try {
      await api.updateCandidateField(candidateId, field, value, extra);
      setCandidates((list) => list.map((c) => (c.candidate_id === candidateId ? patchCandidate(c, field, value) : c)));
      setResults((list) => list.map((r) => (r.candidate_id === candidateId ? patchSearchItem(r, field, value) : r)));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "修改失败");
      throw caught;
    }
  }

  /** 保存沟通记录：只更新候选人级备注，不触碰 parsed_data/画像/索引。 */
  async function saveCommunicationNote(candidateId: string, note: string) {
    setError(null);
    try {
      const saved = await api.updateCommunicationNote(candidateId, note);
      const written = saved.communication_note ?? null;
      setCandidates((list) => list.map((c) => (c.candidate_id === candidateId ? { ...c, communication_note: written } : c)));
      setResults((list) => list.map((r) => (r.candidate_id === candidateId ? { ...r, communication_note: written } : r)));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "沟通记录保存失败");
      throw caught;
    }
  }

  async function deleteCandidateRow(candidateId: string) {
    setError(null);
    try {
      await api.deleteCandidate(candidateId);
      setCandidates((list) => list.filter((c) => c.candidate_id !== candidateId));
      setResults((list) => list.filter((r) => r.candidate_id !== candidateId));
      setCandidateTotal((total) => Math.max(0, total - 1));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除失败");
    }
  }

  function toggleCandidateSelect(candidateId: string) {
    setSelectedCandidateIds((current) => {
      const next = new Set(current);
      if (next.has(candidateId)) next.delete(candidateId);
      else next.add(candidateId);
      return next;
    });
  }

  function toggleCandidateSelectAll(checked: boolean, candidateIds: string[]) {
    setSelectedCandidateIds((current) => {
      const next = new Set(current);
      for (const id of candidateIds) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }

  async function bulkDeleteSelectedCandidates() {
    const ids = Array.from(selectedCandidateIds);
    if (ids.length === 0) return;
    if (!window.confirm(`确定永久删除选中的 ${ids.length} 位候选人吗？此操作不可撤销，删除后其简历与候选人数据将被永久清除（流程历史以快照保留）。`)) return;
    setBulkBusy(true);
    setError(null);
    try {
      const result = await api.bulkDeleteCandidates(ids);
      const failedIds = new Set(result.results.filter((r) => !r.ok).map((r) => r.entity_id));
      setSelectedCandidateIds(new Set(failedIds));
      setCandidates((list) => list.filter((c) => !result.results.some((r) => r.ok && r.entity_id === c.candidate_id)));
      setResults((list) => list.filter((r) => !result.results.some((x) => x.ok && x.entity_id === r.candidate_id)));
      setNotice(`批量删除完成：成功 ${result.succeeded}，失败 ${result.failed}（失败项已保留选中）`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量删除失败");
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkForceOcrSelected(revisionIds: string[]) {
    if (revisionIds.length === 0) return;
    setBulkBusy(true);
    setError(null);
    try {
      const result = await api.bulkForceOcr(revisionIds);
      setNotice(`强制 OCR 已入队：成功 ${result.succeeded}，失败 ${result.failed}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量强制 OCR 失败");
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkReparseSelected(revisionIds: string[]) {
    if (revisionIds.length === 0) return;
    setBulkBusy(true);
    setError(null);
    try {
      const result = await api.bulkReparse(revisionIds);
      setNotice(`重新解析已入队：成功 ${result.succeeded}，失败 ${result.failed}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量重新解析失败");
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkMatchSelectedCandidates() {
    const ids = Array.from(selectedCandidateIds);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setError(null);
    try {
      const result = await api.bulkMatchCandidates(ids);
      // 岗位详情按 JD 修订去重返回，这里合并回行内，直接复用单人匹配的岗位展示。
      const details = new Map(result.jd_details.map((detail) => [detail.revision_id, detail]));
      const items: MatchDrawerJdItem[] = [];
      const sources: MatchDrawerSource[] = [];
      for (const one of result.results) {
        const label = one.name || "未命名";
        sources.push({ candidate_id: one.candidate_id, name: label });
        for (const item of one.items) {
          const detail = details.get(item.revision_id);
          items.push({
            ...item,
            candidate_id: one.candidate_id,
            candidate_name: label,
            run_id: one.run_id,
            company: detail?.company ?? item.company,
            title: detail?.title ?? item.title,
            jd_status: detail?.jd_status ?? item.jd_status,
            ai_category: detail?.ai_category ?? item.ai_category,
            parsed_data: detail?.parsed_data ?? item.parsed_data,
            source_text: detail?.source_text ?? item.source_text,
          });
        }
      }
      const failed = result.results.filter((r) => r.error).length;
      setNotice(`批量匹配完成：${result.results.length - failed} 人共命中 ${items.length} 个岗位${failed ? `，${failed} 人失败` : ""}`);
      setRunReviews({});
      setDrawerCandidateFilter("");
      setMatchPage(1);
      setMatchDrawer({
        mode: "jds",
        title: `批量匹配（${result.results.length} 人）`,
        items,
        // 批量时不给抽屉级 run：AI 复核按行触发，每行用自己候选人的 run。
        run_id: null,
        statuses: {},
        sources,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量匹配失败");
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkDownloadSelectedCandidates() {
    const ids = Array.from(selectedCandidateIds);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setError(null);
    try {
      await api.bulkDownloadCandidates(ids);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量下载失败");
    } finally {
      setBulkBusy(false);
    }
  }

  async function forceReparse(revisionId: string) {
    setError(null);
    try {
      const result = await api.reparseResume(revisionId, true, false);
      const completed = await pollTask(result.task_id);
      if (completed.status !== "SUCCESS") throw new Error(completed.error_message || "重新解析未完成，请检查任务状态后重试。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "强制 OCR 重新解析失败");
      throw caught;
    }
  }

  /** 常规重新解析：只有异常页走 OCR，正常页仍走文本/视觉解析（多为默认选择）。 */
  async function reparseResumeSoft(revisionId: string) {
    setError(null);
    try {
      const result = await api.reparseResume(revisionId, false, true);
      const completed = await pollTask(result.task_id);
      if (completed.status !== "SUCCESS") throw new Error(completed.error_message || "重新解析未完成，请检查任务状态后重试。");
      await loadCandidates();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重新解析失败");
      throw caught;
    }
  }

  async function saveCandidateParsed(candidateId: string, parsed: ParsedResumeData) {
    setError(null);
    try {
      await api.updateCandidateParsed(candidateId, parsed);
      setCandidateParsedEditor(null);
      setCandidates((list) => list.map((c) => (c.candidate_id === candidateId ? { ...c, parsed_data: parsed } : c)));
      setResults((list) => list.map((r) => (r.candidate_id === candidateId ? { ...r, parsed_data: parsed } : r)));
      void loadCandidates(candidatePage);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "解析数据保存失败");
      throw caught;
    }
  }

  async function saveJdParsed(jdId: string, parsed: JdParsedData) {
    setError(null);
    try {
      await api.updateJdParsed(jdId, parsed);
      setJdParsedEditor(null);
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "解析数据保存失败");
      throw caught;
    }
  }

  async function loadJds() {
    setError(null);
    try {
      setJds(await api.listJds());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "JD 列表加载失败");
    }
  }

  async function loadJdsPage(page = 1, filter = jdFilter) {
    setError(null);
    try {
      const response = await api.listJdsPage(page, 10, {
        title: filter.title.trim() || undefined,
        company: filter.company.trim() || undefined,
        status: filter.status || undefined,
      });
      setJdPageItems(response.items);
      setJdTotal(response.total);
      setJdPage(response.page);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "JD 列表加载失败");
    }
  }

  // JD 筛选变更（标题/公司/状态）触发服务端分页重载；跳过首次挂载。
  const jdFilterMounted = useRef(false);
  useEffect(() => {
    if (!jdFilterMounted.current) {
      jdFilterMounted.current = true;
      return;
    }
    const timer = setTimeout(() => {
      void loadJdsPage(1, jdFilter);
    }, 300);
    return () => clearTimeout(timer);
  }, [jdFilter]);

  async function updateJdStatus(jdId: string, status: string) {
    setError(null);
    try {
      await api.updateJdStatus(jdId, status);
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "岗位状态更新失败");
    }
  }

  async function updateJdFieldValue(jdId: string, field: string, value: unknown) {
    setError(null);
    try {
      await api.updateJdField(jdId, field, value);
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
      // 同步更新「候选人匹配岗位」抽屉（matchDrawer）中的岗位字段，避免保存后显示不刷新。
      setMatchDrawer((cur) => {
        if (!cur || cur.mode !== "jds") return cur;
        return {
          ...cur,
          items: cur.items.map((it) => it.jd_id === jdId
            ? { ...it, parsed_data: { ...(it.parsed_data ?? {}), [field]: value } as JdParsedData }
            : it),
        };
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "岗位信息修改失败");
      throw caught;
    }
  }

  async function regenerateJdProfile(jdId: string, instruction = "", onStage?: (progress: ProfileGenerationProgress) => void) {
    setError(null);
    try {
      const result = await api.regenerateJdProfile(jdId, instruction, onStage);
      const profile = result.summary ?? null;
      if (result.points?.length) {
        // 硬条件与画像同一次调用产出；一起缓存，保存时不必再调一次模型。
        jdDualFormRef.current.set(jdId, {
          narrative: profile,
          points: result.points,
          compact: result.compact ?? null,
          constraints: result.constraints,
        });
      } else {
        jdDualFormRef.current.delete(jdId);
      }
      // 仅更新本地预览，不落库、不重建索引、不刷新列表；由「保存」按钮提交。
      setJdProfileEditor((cur) => (cur && cur.jdId === jdId ? { ...cur, profile, points: result.points, compact: result.compact ?? null } : cur));
      return profile;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像重新生成失败");
      throw caught;
    }
  }

  /** JD 列表内联保存画像：文本变了就按模型重解析硬条件与年限并覆盖，一次提交后重建索引。 */
  async function saveJdProfileInline(jdId: string, profileText: string) {
    setError(null);
    const jd = jdPageItems.find((item) => item.jd_id === jdId) ?? jds.find((item) => item.jd_id === jdId);
    const saved = profileText.trim() || null;
    const current = (jd?.parsed_data?.candidate_profile ?? "").trim() || null;
    try {
      let constraints: JdExactConstraint[] | undefined;
      let years: { min_years: number | null; years_stated: boolean } | undefined;
      if (saved !== current) {
        // 「AI 重新生成」已产出过硬条件时直接复用；否则按新文本重解析（画像一变就重解析覆盖）。
        // 年限只能来自重解析：重新生成链路没有年限输出，此时交给后端的规则兜底。
        const cached = jdDualFormRef.current.get(jdId);
        if (cached && cached.narrative === saved && cached.constraints) {
          constraints = cached.constraints;
        } else {
          const parsedRequirements = await api.parseJdConstraints(saved ?? "");
          constraints = parsedRequirements.constraints;
          years = { min_years: parsedRequirements.min_years, years_stated: parsedRequirements.years_stated };
        }
      }
      const parsed: JdParsedData = { candidate_profile: saved };
      if (constraints) parsed.exact_constraints = constraints;
      // 画像没提年限时不提交 min_years，让后端保留 JD 解析出的原值。
      if (years?.years_stated) parsed.min_years = years.min_years;
      await api.updateJdParsed(jdId, parsed);
      jdDualFormRef.current.delete(jdId);
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
      setNotice("画像已保存");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像保存失败");
      throw caught;
    }
  }

  async function saveJdProfileAndConstraints(
    jdId: string, profile: string, constraints: JdExactConstraint[], years: JdProfileYears,
  ) {
    setError(null);
    try {
      // 文字画像与结构化硬条件一次原子提交，避免两次请求部分成功造成不一致。
      // 若画像来自「重新生成」，把同源分点/浓缩一并透传，后端原样落库（不再按标点伪拆点）。
      const saved = profile || null;
      let generated = jdProfileEditor && jdProfileEditor.jdId === jdId && jdProfileEditor.points?.length
        ? { points: jdProfileEditor.points, compact: jdProfileEditor.compact ?? null }
        : null;
      if (!generated) {
        // 列表页「重新生成」后经弹窗保存：仅在正文未被改动时透传，避免分点与正文不同源。
        const cached = jdDualFormRef.current.get(jdId);
        if (cached && cached.narrative === saved) {
          generated = { points: cached.points, compact: cached.compact };
        }
      }
      await api.updateJdParsed(jdId, {
        candidate_profile: saved,
        exact_constraints: constraints,
        // 画像没提年限时不提交 min_years，让后端保留 JD 解析出的原值。
        ...(years.yearsStated ? { min_years: years.minYears } : {}),
        ...(generated ? { candidate_profile_points: generated.points, candidate_profile_compact: generated.compact } : {}),
      });
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
      setMatchDrawer((cur) => {
        if (!cur || cur.mode !== "jds") return cur;
        return {
          ...cur,
          items: cur.items.map((it) => it.jd_id === jdId
            ? { ...it, parsed_data: { ...(it.parsed_data ?? {}), candidate_profile: profile || null, exact_constraints: constraints } as JdParsedData }
            : it),
        };
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像保存失败");
      throw caught;
    }
  }

  async function runJdMatch(jd: JdListItem) {
    setError(null);
    setJdMatchingId(jd.jd_id);
    try {
      const result = await api.matchJd(jd.revision_id, 1000);
      setJdMatch({
        title: jd.title || "岗位",
        company: jd.company || "",
        location: jd.location || jd.parsed_data?.location || "",
        run_id: result.run_id,
        items: result.items,
        hardFilters: result.hard_filters ?? [],
        relaxed: result.relaxed ?? [],
      });
      setJdReview(null);
      // 安全阀触发：下推的硬条件把候选池清空了，系统已自动回退，必须让用户知道
      // 「这一次的硬门槛没有生效」，否则会把放宽后的结果误当成满足硬条件的推荐。
      if ((result.relaxed ?? []).length > 0) {
        setNotice(`岗位硬条件（${(result.relaxed ?? []).join("、")}）未匹配到候选人，已自动放宽后重新检索，本页结果不满足这些硬条件。`);
      }
      if (result.items.length === 0) {
        if (result.empty_reason === "jd_direction_pending") {
          setError("该岗位的职业方向尚未确认，请先在岗位详情中确认方向后再匹配候选人。");
        } else if (result.empty_reason === "candidate_direction_pending") {
          setError("匹配到的候选人均为方向待核，请在人才库「方向待核」中确认方向后再匹配。");
        } else {
          setError("该岗位未匹配到候选人：请确认候选人已导入且解析完成，并满足岗位的年限/学历等硬性要求。");
        }
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "匹配失败");
    } finally {
      setJdMatchingId(null);
    }
  }

  /**
   * 复核轮询：走 ``waitForTask`` 的退避档位 + 总时长上限。
   *
   * 同一 run 只保留一个轮询（重复点击复用同一个 Promise），避免「每秒一次」在长复核里
   * 刷出上千个请求；停止轮询不等于任务失败，超时会把原因写进 error_message 让用户看到。
   */
  function pollReview(runId: string, reviewId: string, setReview: (s: AiReviewState) => void): Promise<void> {
    const existing = reviewPolls.current.get(runId);
    if (existing) return existing.promise;
    const controller = new AbortController();
    const promise = waitForTask(
      () => api.getAiReview(runId),
      (status) => setReview({ review_id: reviewId, ...status }),
      {
        signal: controller.signal,
        backoff: REVIEW_POLL_BACKOFF_MS,
        timeoutMs: REVIEW_POLL_LIMIT_MS,
        // 超时不代表任务结束：明确提示用户已停止自动刷新，避免界面一直停在「复核中」。
        onTimeout: (status) => setReview({
          review_id: reviewId,
          ...status,
          error_message: status.error_message
            ?? "复核耗时较长，已停止自动刷新；任务可能仍在后台运行，稍后可重新打开查看。",
        }),
      },
    ).then(() => undefined).finally(() => reviewPolls.current.delete(runId));
    reviewPolls.current.set(runId, { controller, promise });
    return promise;
  }

  async function startAndPollReview(runId: string, setReview: (s: AiReviewState) => void, reasoning = false) {
    const started = await api.startAiReview(runId, reasoning);
    await pollReview(runId, started.review_id, setReview);
  }

  async function runJdAiReview() {
    const runId = jdMatch?.run_id;
    if (!runId) return;
    setError(null);
    try {
      await startAndPollReview(runId, setJdReview, jdReviewReasoning);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "AI 复核失败");
    }
  }

  /** 复核状态按 run 分桶写入：批量匹配时每个候选人一个 run，互不覆盖。 */
  function reviewSetterFor(runId: string) {
    return (state: AiReviewState | null) => setRunReviews((prev) => {
      if (state === null) {
        const next = { ...prev };
        delete next[runId];
        return next;
      }
      return { ...prev, [runId]: state };
    });
  }

  async function runAiReview(runId: string | null) {
    if (!runId) return;
    setError(null);
    try {
      await startAndPollReview(runId, reviewSetterFor(runId), candidateReviewReasoning);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "AI 复核失败");
    }
  }

  /** 批量抽屉里每行自己的复核入口：进度/重试/发起都按该行的 run 走。 */
  function renderRunReviewAction(runId: string | null) {
    if (!runId) return null;
    const review = runReviews[runId];
    if (review && !["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"].includes(review.status)) {
      return <span className="muted" style={{ fontSize: 12 }}>复核 {review.progress}%</span>;
    }
    if (review && ["FAILED", "DEAD_LETTER", "CANCELLED"].includes(review.status)) {
      return (
        <button className="detail-button" onClick={() => void retryAiReview(runId, review, reviewSetterFor(runId))}>
          重试复核
        </button>
      );
    }
    return (
      <button className="detail-button" onClick={() => void runAiReview(runId)}>
        {review ? "重新复核" : "AI 复核"}
      </button>
    );
  }

  async function cancelAiReview(review: AiReviewState | null, setReview: (s: AiReviewState | null) => void) {
    if (!review?.review_id) return;
    setError(null);
    try {
      const updated = await api.controlTask(review.review_id, "cancel");
      setReview({ ...review, status: updated.status });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "取消复核失败");
    }
  }

  async function retryAiReview(runId: string | null, review: AiReviewState | null, setReview: (s: AiReviewState) => void) {
    if (!runId || !review?.review_id) return;
    setError(null);
    try {
      await api.controlTask(review.review_id, "retry");
      await pollReview(runId, review.review_id, setReview);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重试复核失败");
    }
  }

  /**
   * 搜索侧复核轮询：与匹配复核共用退避档位与总时长上限。
   * 结论逐条落库，所以长任务也能边跑边看到已出的亮点/风险点。
   */
  function pollSearchReview(reviewId: string): Promise<void> {
    const existing = searchReviewPolls.current.get(reviewId);
    if (existing) return existing.promise;
    const controller = new AbortController();
    const promise = waitForTask(
      () => api.getSearchReview(reviewId),
      (status) => setSearchReview(searchReviewStateFrom(reviewId, status)),
      {
        signal: controller.signal,
        backoff: REVIEW_POLL_BACKOFF_MS,
        timeoutMs: REVIEW_POLL_LIMIT_MS,
        onTimeout: (status) => setSearchReview({
          ...searchReviewStateFrom(reviewId, status),
          error_message: status.error_message
            ?? "复核耗时较长，已停止自动刷新；任务可能仍在后台运行，稍后可重新打开查看。",
        }),
      },
    ).then(() => undefined).finally(() => searchReviewPolls.current.delete(reviewId));
    searchReviewPolls.current.set(reviewId, { controller, promise });
    return promise;
  }

  async function runSearchReview(candidateIds: string[]) {
    if (candidateIds.length === 0) return;
    setError(null);
    try {
      const started = await api.startSearchReview({
        query: query.trim(),
        filters: buildSearchFilters(),
        candidate_ids: candidateIds,
      });
      // 触发了复核就把两列展开；用户仍可在「列设置」里关掉。
      setVisibleColumns((prev) => ({ ...prev, highlights: true, risks: true }));
      await pollSearchReview(started.review_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "AI 复核失败");
    }
  }

  async function cancelSearchReview() {
    if (!searchReview?.review_id) return;
    setError(null);
    try {
      const updated = await api.controlTask(searchReview.review_id, "cancel");
      setSearchReview({ ...searchReview, status: updated.status });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "取消复核失败");
    }
  }

  async function retrySearchReview() {
    if (!searchReview?.review_id) return;
    setError(null);
    try {
      await api.controlTask(searchReview.review_id, "retry");
      await pollSearchReview(searchReview.review_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重试复核失败");
    }
  }

  async function runCandidateMatch(candidateId: string, name: string) {
    setError(null);
    try {
      const result = await api.matchCandidate(candidateId);
      setRunReviews({});
      setDrawerCandidateFilter("");
      setMatchDrawer({
        mode: "jds",
        title: name || "候选人",
        items: result.items,
        run_id: result.run_id,
        statuses: {},
      });
      setMatchPage(1);
      if (result.items.length === 0) {
        setError("该候选人未匹配到岗位：请确认已有处于「开放」状态的岗位。");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "匹配失败");
    }
  }

  async function createCaseFromDrawer(resultId: string) {
    if (creatingCaseRef.current) return;
    const existingCaseId = matchCaseIds[resultId];
    if (existingCaseId) { await openCaseDrawer(existingCaseId); return; }
    creatingCaseRef.current = true;
    setCreatingCase(true);
    setError(null);
    try {
      const created = await api.createCaseFromMatchResult(resultId);
      setMatchCaseIds((current) => ({ ...current, [resultId]: created.case_id }));
      setMatchDrawer((current) => current ? {
        ...current,
        statuses: { ...current.statuses, [resultId]: "保留" },
      } : current);
      await openCaseDrawer(created.case_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建流程失败");
    } finally {
      creatingCaseRef.current = false;
      setCreatingCase(false);
    }
  }

  function openCreateCasePicker(candidateId: string, name: string) {
    setError(null);
    setCreateCasePicker({ candidateId, name });
    void loadJds();
  }

  async function confirmCreateCase(jdId: string) {
    const picker = createCasePicker;
    if (!picker || creatingCaseRef.current) return;
    creatingCaseRef.current = true;
    setCreatingCase(true);
    setError(null);
    try {
      const created = await api.createCase(picker.candidateId, jdId);
      setCreateCasePicker(null);
      await openCaseDrawer(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建流程失败");
    } finally {
      creatingCaseRef.current = false;
      setCreatingCase(false);
    }
  }

  async function openCaseDrawer(caseId: string) {
    setError(null);
    try {
      setCaseDrawer(await api.getCase(caseId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "加载流程失败");
    }
  }

  async function loadCases(page = 1, jdId = caseJdFilter) {
    setError(null);
    try {
      const response = await api.listCasesPage(page, 20, jdId || undefined);
      setCases(response.items);
      setCaseTotal(response.total);
      setCasePage(response.page);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "加载流程列表失败");
    }
  }

  async function deleteCase(caseId: string) {
    setError(null);
    try {
      await api.deleteCase(caseId);
      await loadCases();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除流程失败");
    }
  }

  async function deleteJd(jd: JdListItem) {
    setError(null);
    // 岗位删除不可撤销，必须确认；文案要说明流程会被保留（岗位信息转快照）。
    if (!window.confirm(`确定永久删除岗位「${jd.title || jd.jd_id}」吗？关联的招聘流程会保留（岗位信息转为快照），此操作不可撤销。`)) {
      return;
    }
    try {
      await api.deleteJd(jd.jd_id);
      // 删除后刷新分页列表（管理页显示的是 jdPageItems）；若当前页被删空则回退到上一页。
      const nextPage = jdPageItems.length === 1 && jdPage > 1 ? jdPage - 1 : jdPage;
      await loadJdsPage(nextPage, jdFilter);
      await loadJds();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除岗位失败");
    }
  }

  async function bulkDeleteJdsHandler(ids: string[]) {
    setError(null);
    try {
      const result = await api.bulkDeleteJds(ids);
      await loadJdsPage(jdPage, jdFilter);
      await loadJds();
      setNotice(`批量删除岗位完成：成功 ${result.succeeded}，失败 ${result.failed}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量删除岗位失败");
    }
  }

  async function bulkDeleteCasesHandler(ids: string[]) {
    setError(null);
    try {
      const result = await api.bulkDeleteCases(ids);
      await loadCases();
      setNotice(`批量删除流程完成：成功 ${result.succeeded}，失败 ${result.failed}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批量删除流程失败");
    }
  }

  function toggleJdSource(revisionId: string) {
    setOpenJdSource((current) => ({ ...current, [revisionId]: !current[revisionId] }));
  }

  async function downloadResumeFile(revisionId: string, filename: string) {
    setError(null);
    try {
      await api.downloadResume(revisionId, filename);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "简历下载失败");
    }
  }

  async function previewResumeFile(revisionId: string, name?: string, filename?: string) {
    setError(null);
    setNotice(null);
    const file = filename || name || "";
    try {
      const target = await api.viewResume(revisionId);
      if (target.kind === "opened") {
        setNotice("已交给系统默认应用打开 Word 文档。");
      } else if (target.url) {
        setPreviewName(target.filename || file || "简历预览");
        setPreviewUrl(target.url);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "简历预览失败");
    }
  }

  async function openResumeReview(revisionId: string) {
    setError(null);
    try { setResumeReview(await api.getResumeReview(revisionId)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "加载复核资料失败"); }
  }

  async function controlTask(task: TaskStatus, action: TaskAction) {
    setError(null);
    try {
      const updated = await api.controlTask(task.id, action);
      setTasks((current) => [updated, ...current.filter((entry) => entry.id !== updated.id)]);
      if (action === "resume" || action === "retry") pollTask(updated.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务操作失败");
    }
  }

  async function uploadResume(file: File | undefined, files?: FileList | null) {
    const selected = files && files.length > 0 ? Array.from(files) : file ? [file] : [];
    if (selected.length === 0) return;
    setError(null);
    for (const item of selected) {
      try {
        const imported = await api.importResume(item);
        if (imported.task_id) {
          const task = await api.getTask(imported.task_id);
          setTasks((current) => [task, ...current.filter((entry) => entry.id !== task.id)]);
          await loadCandidates(1);
          pollTask(imported.task_id);
        } else if (imported.action === "DUPLICATE_CONFLICT") {
          setError(imported.message || "相同文件已关联到多个候选人，请人工处理");
        } else {
          setNotice(imported.message || "文件已导入过");
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "导入失败，请检查文件");
      }
    }
  }

  async function importFolderPath(event: FormEvent) {
    event.preventDefault();
    if (!folderPath.trim()) return;
    setError(null);
    try {
      const result = await api.importFolder(folderPath.trim());
      const taskIds = result.imported.filter((item) => item.task_id).map((item) => item.task_id as string);
      pollBatchTasks(taskIds, taskIds.length);
      if (result.skipped.length > 0 || result.errors.length > 0) {
        setError(`已导入 ${result.imported.length} 个，跳过 ${result.skipped.length} 个，失败 ${result.errors.length} 个`);
      }
      setFolderPath("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "文件夹导入失败");
    }
  }

  const BATCH_CHUNK = 200;
  const BATCH_POLL_MS = 1500;
  const BATCH_TIMEOUT_MS = 300_000;
  const BATCH_TERMINAL = ["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"];

  function summarizeBatch(taskIds: string[], statusMap: Record<string, string>, total: number) {
    let waiting = 0, running = 0, done = 0, failed = 0;
    for (const id of taskIds) {
      const status = statusMap[id];
      if (status === "SUCCESS") done += 1;
      else if (status === "FAILED" || status === "DEAD_LETTER" || status === "CANCELLED") failed += 1;
      else if (status === "RUNNING" || status === "RETRY_WAIT") running += 1;
      else waiting += 1;
    }
    const finished = done + failed;
    return { total, waiting, running, done, failed, percent: total ? Math.round((finished / total) * 100) : 100 };
  }

  async function fetchBatchStatus(taskIds: string[]) {
    const found: TaskStatus[] = [];
    const missing: string[] = [];
    for (let i = 0; i < taskIds.length; i += BATCH_CHUNK) {
      const result = await api.getTaskStatusBatch(taskIds.slice(i, i + BATCH_CHUNK));
      found.push(...result.found);
      missing.push(...result.missing_ids);
    }
    return { found, missing };
  }

  function pollBatchTasks(taskIds: string[], total: number) {
    setBatchTaskIds(taskIds);
    setBatchTimedOut(false);
    void runBatchPolling(taskIds, total);
  }

  async function runBatchPolling(taskIds: string[], total: number) {
    const deadline = Date.now() + BATCH_TIMEOUT_MS;
    const statusMap: Record<string, string> = {};
    while (Date.now() < deadline) {
      try {
        const { found } = await fetchBatchStatus(taskIds);
        for (const task of found) statusMap[task.id] = task.status;
        const summary = summarizeBatch(taskIds, statusMap, total);
        setBatchProgress(summary);
        if (summary.done + summary.failed >= total) {
          setBatchTimedOut(false);
          await loadCandidates();
          return;
        }
      } catch {
        setBatchTimedOut(true);
        setBatchProgress(summarizeBatch(taskIds, statusMap, total));
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, BATCH_POLL_MS));
    }
    setBatchTimedOut(true);
    setBatchProgress(summarizeBatch(taskIds, statusMap, total));
  }

  async function continueBatchPolling() {
    if (batchTaskIds.length === 0 || !batchProgress) return;
    setBatchTimedOut(false);
    await runBatchPolling(batchTaskIds, batchProgress.total);
  }

  async function submitJd(event: FormEvent) {
    event.preventDefault();
    if (!jdSource.trim()) return;
    setError(null);
    try {
      const result = await api.importJdBatch(jdSource.trim());
      setJdResult(result.imported);
      // 导入后必须**同时**刷新分页列表：只刷 loadJds() 会更新头部「共 N 个在招岗位」，
      // 而「岗位列表 N 个」仍停在导入前的旧值（表现为头部 1、列表 0，e2e 实测）。
      await Promise.all([loadJds(), loadJdsPage(1, jdFilter)]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "JD 导入失败");
    }
  }

  async function uploadJd(file: File | undefined) {
    if (!file) return;
    setError(null);
    try {
      const result = await api.importJdBatchFile(file);
      setJdResult(result.imported);
      await Promise.all([loadJds(), loadJdsPage(1, jdFilter)]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "JD 文件导入失败");
    }
  }

  async function checkHealth() {
    setError(null);
    try {
      setHealth(await api.health());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "健康检测失败");
    }
  }

  async function loadDiagnostics() {
    setError(null);
    try {
      setDiagnostics(await api.diagnostics());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "诊断信息加载失败");
    }
  }

  async function exportDiagnostics() {
    setError(null);
    try {
      await api.exportDiagnostics();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "诊断信息导出失败");
    }
  }

  async function loadCompanies() {
    setError(null);
    try {
      setCompanies(await api.listCompanies());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "公司列表加载失败");
    }
  }

  async function reloadOrgTree(companyId: string) {
    try {
      const [tree, deps, emps] = await Promise.all([
        api.getOrgTree(companyId),
        api.listDepartments(companyId),
        api.listEmployees(companyId),
      ]);
      setOrgTree(tree);
      setDepartments(deps);
      setEmployees(emps);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "组织数据加载失败");
    }
  }

  async function createCompany(event: FormEvent) {
    event.preventDefault();
    if (!companyName.trim()) return;
    setError(null);
    try {
      const company = await api.createCompany(companyName.trim());
      setCompanies((current) => [...current, company]);
      setCompanyName("");
      setSelectedCompanyId(company.id);
      setSelectedOrgNodeId(null);
      setOrgTree(null);
      setDepartments([]);
      setEmployees([]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建公司失败");
    }
  }

  async function selectCompany(companyId: string) {
    setError(null);
    setSelectedCompanyId(companyId);
    setSelectedOrgNodeId(null);
    await reloadOrgTree(companyId);
  }

  async function addOrgChild(node: OrgTreeNode) {
    if (!selectedCompanyId) return;
    const companyId = selectedCompanyId;
    setError(null);
    try {
      let kind: "department" | "employee";
      let createdId: string;
      let deptArgs: CreateOrgDepartmentInput | null = null;
      let empArgs: CreateOrgEmployeeInput | null = null;
      if (node.kind === "company") {
        deptArgs = { company_id: companyId, name: "新部门" };
        const dept = await api.createDepartment(deptArgs);
        kind = "department";
        createdId = dept.id;
      } else if (node.kind === "department") {
        deptArgs = { company_id: companyId, name: "新部门", parent_id: node.id };
        const dept = await api.createDepartment(deptArgs);
        kind = "department";
        createdId = dept.id;
      } else {
        const emp = employees.find((e) => e.id === node.id);
        empArgs = {
          company_id: companyId,
          department_id: emp?.department_id ?? null,
          name: "新人员",
          report_to: node.id,
        };
        const created = await api.createEmployee(empArgs);
        kind = "employee";
        createdId = created.id;
      }
      await reloadOrgTree(companyId);
      setSelectedOrgNodeId(createdId);
      setPendingEditId(createdId);
      recordUndo({
        undo: async () => {
          if (kind === "department") await api.deleteDepartment(createdId);
          else await api.deleteEmployee(createdId);
          await reloadOrgTree(companyId);
        },
        redo: async () => {
          if (kind === "department" && deptArgs) await api.createDepartment(deptArgs);
          else if (empArgs) await api.createEmployee(empArgs);
          await reloadOrgTree(companyId);
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "新增节点失败");
    }
  }

  async function addOrgSibling(node: OrgTreeNode) {
    if (!selectedCompanyId) return;
    const companyId = selectedCompanyId;
    setError(null);
    try {
      let kind: "department" | "employee";
      let createdId: string;
      let deptArgs: CreateOrgDepartmentInput | null = null;
      let empArgs: CreateOrgEmployeeInput | null = null;
      if (node.kind === "department") {
        const dept = departments.find((d) => d.id === node.id);
        deptArgs = { company_id: companyId, name: "新部门", parent_id: dept?.parent_id ?? null };
        const created = await api.createDepartment(deptArgs);
        kind = "department";
        createdId = created.id;
      } else if (node.kind === "employee") {
        const emp = employees.find((e) => e.id === node.id);
        empArgs = {
          company_id: companyId,
          department_id: emp?.department_id ?? null,
          name: "新人员",
          report_to: emp?.report_to ?? null,
        };
        const created = await api.createEmployee(empArgs);
        kind = "employee";
        createdId = created.id;
      } else {
        return;
      }
      await reloadOrgTree(companyId);
      setSelectedOrgNodeId(createdId);
      setPendingEditId(createdId);
      recordUndo({
        undo: async () => {
          if (kind === "department") await api.deleteDepartment(createdId);
          else await api.deleteEmployee(createdId);
          await reloadOrgTree(companyId);
        },
        redo: async () => {
          if (kind === "department" && deptArgs) await api.createDepartment(deptArgs);
          else if (empArgs) await api.createEmployee(empArgs);
          await reloadOrgTree(companyId);
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "新增节点失败");
    }
  }

  async function addOrgPerson(node: OrgTreeNode) {
    if (!selectedCompanyId) return;
    const companyId = selectedCompanyId;
    setError(null);
    try {
      let empArgs: CreateOrgEmployeeInput;
      if (node.kind === "department") {
        empArgs = { company_id: companyId, department_id: node.id, name: "新人员" };
      } else if (node.kind === "employee") {
        const sourceEmp = employees.find((e) => e.id === node.id);
        empArgs = { company_id: companyId, department_id: sourceEmp?.department_id ?? null, name: "新人员", report_to: node.id };
      } else {
        return;
      }
      const created = await api.createEmployee(empArgs);
      await reloadOrgTree(companyId);
      setSelectedOrgNodeId(created.id);
      setPendingEditId(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "新增人员失败");
    }
  }

  async function renameOrgNode(node: OrgTreeNode, name: string) {
    const companyId = selectedCompanyId;
    const oldName = node.name;
    setError(null);
    try {
      if (node.kind === "company") {
        await api.updateCompany(node.id, name);
        setCompanies((current) => current.map((c) => (c.id === node.id ? { ...c, name } : c)));
      } else if (node.kind === "employee") {
        await api.updateEmployee(node.id, { name });
      } else if (node.kind === "department") {
        await api.updateDepartment(node.id, { name });
      }
      if (companyId) await reloadOrgTree(companyId);
      recordUndo({
        undo: async () => {
          if (node.kind === "company") {
            await api.updateCompany(node.id, oldName);
            setCompanies((current) => current.map((c) => (c.id === node.id ? { ...c, name: oldName } : c)));
          } else if (node.kind === "employee") {
            await api.updateEmployee(node.id, { name: oldName });
          } else if (node.kind === "department") {
            await api.updateDepartment(node.id, { name: oldName });
          }
          if (companyId) await reloadOrgTree(companyId);
        },
        redo: async () => {
          if (node.kind === "company") {
            await api.updateCompany(node.id, name);
            setCompanies((current) => current.map((c) => (c.id === node.id ? { ...c, name } : c)));
          } else if (node.kind === "employee") {
            await api.updateEmployee(node.id, { name });
          } else if (node.kind === "department") {
            await api.updateDepartment(node.id, { name });
          }
          if (companyId) await reloadOrgTree(companyId);
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重命名失败");
    }
  }

  async function deleteOrgNode(node: OrgTreeNode) {
    const companyId = selectedCompanyId;
    setError(null);
    try {
      const savedEmployee = node.kind === "employee" ? employees.find((e) => e.id === node.id) ?? null : null;
      const savedDepartment = node.kind === "department" ? departments.find((d) => d.id === node.id) ?? null : null;

      if (node.kind === "employee") await api.deleteEmployee(node.id);
      else if (node.kind === "department") await api.deleteDepartment(node.id);
      setSelectedOrgNodeId(null);
      if (companyId) await reloadOrgTree(companyId);

      recordUndo({
        undo: async () => {
          if (savedEmployee) {
            await api.createEmployee({
              company_id: savedEmployee.company_id,
              name: savedEmployee.name,
              department_id: savedEmployee.department_id,
              title: savedEmployee.title,
              job_level: savedEmployee.job_level,
              report_to: savedEmployee.report_to,
              subordinate_count: savedEmployee.subordinate_count,
              tenure_years: savedEmployee.tenure_years,
              business_module: savedEmployee.business_module,
              status: savedEmployee.status,
              intention: savedEmployee.intention,
              remark: savedEmployee.remark,
              contact: savedEmployee.contact,
              is_key: savedEmployee.is_key,
            });
          } else if (savedDepartment) {
            await api.createDepartment({
              company_id: savedDepartment.company_id,
              name: savedDepartment.name,
              parent_id: savedDepartment.parent_id,
              leader_id: savedDepartment.leader_id,
              leader_report_to: savedDepartment.leader_report_to,
              team_size: savedDepartment.team_size,
              business_direction: savedDepartment.business_direction,
              tech_stack: savedDepartment.tech_stack,
              office_location: savedDepartment.office_location,
              hc_status: savedDepartment.hc_status,
              hc_internal_note: savedDepartment.hc_internal_note,
            });
          }
          if (companyId) await reloadOrgTree(companyId);
        },
        redo: async () => {
          if (node.kind === "employee") await api.deleteEmployee(node.id);
          else if (node.kind === "department") await api.deleteDepartment(node.id);
          if (companyId) await reloadOrgTree(companyId);
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除失败");
    }
  }

  async function updateOrgEmployeeField(id: string, changes: UpdateOrgEmployeeInput) {
    setError(null);
    try {
      await api.updateEmployee(id, changes);
      if (selectedCompanyId) await reloadOrgTree(selectedCompanyId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    }
  }

  async function bindOrgEmployee(employeeId: string, phone: string, name: string): Promise<BindEmployeeResult> {
    const result = await api.bindEmployee(employeeId, phone, name);
    if (selectedCompanyId) await reloadOrgTree(selectedCompanyId);
    return result;
  }

  async function updateOrgDepartmentField(id: string, changes: UpdateOrgDepartmentInput) {
    setError(null);
    try {
      await api.updateDepartment(id, changes);
      if (selectedCompanyId) await reloadOrgTree(selectedCompanyId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    }
  }

  async function deleteCompany(companyId: string) {
    setError(null);
    try {
      await api.deleteCompany(companyId);
      setCompanies((current) => current.filter((c) => c.id !== companyId));
      if (selectedCompanyId === companyId) {
        setSelectedCompanyId(null);
        setSelectedOrgNodeId(null);
        setOrgTree(null);
        setDepartments([]);
        setEmployees([]);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "删除公司失败");
    }
  }

  function applyOrgParseResult(result: OrgParseResult) {
    if (result.draft) setOrgImportDraft(result.draft);
    const questions = result.questions ?? [];
    setOrgImportQuestions(questions);
    setOrgImportAnswers(questions.map(() => ""));
    setOrgImportMessage(questions.length > 0 ? "解析有疑问，请回答下方问题后继续解析" : "解析完成，请核对下方结果后导入");
  }

  async function parseOrgImportText() {
    if (!orgImportText.trim()) return;
    setError(null);
    setOrgImportMessage("");
    setOrgImportBusy(true);
    try {
      applyOrgParseResult(await api.parseOrgImport(orgImportText.trim()));
    } catch (caught) {
      setOrgImportMessage(caught instanceof Error ? caught.message : "解析失败");
    } finally {
      setOrgImportBusy(false);
    }
  }

  async function parseOrgImportFile(file: File) {
    setError(null);
    setOrgImportMessage("");
    setOrgImportBusy(true);
    try {
      const payload = await api.parseOrgWord(file);
      applyOrgParseResult(payload.result);
      setOrgImportText(payload.source_text);
      setOrgImportFileName(file.name);
    } catch (caught) {
      setOrgImportMessage(caught instanceof Error ? caught.message : "解析失败");
    } finally {
      setOrgImportBusy(false);
    }
  }

  async function answerOrgImportDraft() {
    if (!orgImportText.trim()) return;
    setError(null);
    setOrgImportMessage("");
    setOrgImportBusy(true);
    try {
      const answers = orgImportAnswers.map((answer) => answer.trim()).filter(Boolean);
      applyOrgParseResult(await api.answerOrgImport(orgImportText.trim(), answers));
    } catch (caught) {
      setOrgImportMessage(caught instanceof Error ? caught.message : "解析失败");
    } finally {
      setOrgImportBusy(false);
    }
  }

  async function commitOrgImport() {
    if (!orgImportDraft) return;
    if (!selectedCompanyId) {
      setOrgImportMessage("请先在左侧「公司」列表选择或新建目标公司，再导入");
      return;
    }
    setError(null);
    setOrgImportMessage("");
    setOrgImportBusy(true);
    try {
      const result = await api.commitOrgImport(selectedCompanyId, orgImportDraft, orgImportText);
      setOrgImportMessage(`已导入 ${result.departments} 个部门、${result.employees} 名人员`);
      setOrgImportDraft(null);
      setOrgImportText("");
      setOrgImportFileName("");
      await reloadOrgTree(selectedCompanyId);
    } catch (caught) {
      setOrgImportMessage(caught instanceof Error ? caught.message : "导入失败");
    } finally {
      setOrgImportBusy(false);
    }
  }

  async function reviseOrgImportDraft() {
    if (!orgImportDraft || !orgReviseInstruction.trim()) return;
    setOrgImportBusy(true);
    setOrgImportMessage("");
    try {
      const revised = await api.reviseOrgImport(orgImportDraft, orgReviseInstruction.trim());
      setOrgImportDraft(revised);
      setOrgReviseInstruction("");
      setOrgImportMessage("已按指令修订，请核对后导入");
    } catch (caught) {
      setOrgImportMessage(caught instanceof Error ? caught.message : "修订失败");
    } finally {
      setOrgImportBusy(false);
    }
  }

  async function toggleOrgSource(companyId: string) {
    if (orgSource?.companyId === companyId) {
      setOrgSource(null);
      return;
    }
    setError(null);
    try {
      const result = await api.getCompanySource(companyId);
      setOrgSource({ companyId, text: result.source_text });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "读取导入原文失败");
    }
  }

  function updateOrgImportCompanyName(value: string) {
    setOrgImportDraft((current) => (current ? { ...current, company_name: value } : current));
  }

  function updateOrgImportDepartment(index: number, field: string, value: string | number | null) {
    setOrgImportDraft((current) => {
      if (!current) return current;
      const departments = [...current.departments];
      departments[index] = { ...departments[index], [field]: value };
      return { ...current, departments };
    });
  }

  function updateOrgImportEmployee(index: number, field: string, value: string | number | null) {
    setOrgImportDraft((current) => {
      if (!current) return current;
      const employees = [...current.employees];
      employees[index] = { ...employees[index], [field]: value };
      return { ...current, employees };
    });
  }

  async function moveOrgNode(source: OrgTreeNode, target: OrgTreeNode) {
    if (!selectedCompanyId) return;
    const companyId = selectedCompanyId;
    setError(null);
    try {
      const sourceEmployee = source.kind === "employee" ? employees.find((e) => e.id === source.id) ?? null : null;
      const sourceDepartment = source.kind === "department" ? departments.find((d) => d.id === source.id) ?? null : null;
      const oldReportTo = sourceEmployee?.report_to ?? null;
      const oldDepartmentId = sourceEmployee?.department_id ?? null;
      const oldParentId = sourceDepartment?.parent_id ?? null;

      if (source.kind === "employee" && target.kind === "employee") {
        await api.updateEmployee(source.id, { report_to: target.id });
      } else if (source.kind === "employee" && target.kind === "department") {
        await api.updateEmployee(source.id, { department_id: target.id });
      } else if (source.kind === "department" && target.kind === "department") {
        await api.updateDepartment(source.id, { parent_id: target.id });
      } else if (source.kind === "department" && target.kind === "company") {
        // 拖到公司根节点＝提升为顶层部门（parent_id 置空即挂在公司下）。
        await api.updateDepartment(source.id, { parent_id: null });
      } else {
        return;
      }
      await reloadOrgTree(companyId);

      recordUndo({
        undo: async () => {
          if (source.kind === "employee") {
            const changes: UpdateOrgEmployeeInput = {};
            if (target.kind === "employee") changes.report_to = oldReportTo;
            else if (target.kind === "department") changes.department_id = oldDepartmentId;
            await api.updateEmployee(source.id, changes);
          } else if (source.kind === "department") {
            await api.updateDepartment(source.id, { parent_id: oldParentId });
          }
          await reloadOrgTree(companyId);
        },
        redo: async () => {
          if (source.kind === "employee" && target.kind === "employee") {
            await api.updateEmployee(source.id, { report_to: target.id });
          } else if (source.kind === "employee" && target.kind === "department") {
            await api.updateEmployee(source.id, { department_id: target.id });
          } else if (source.kind === "department" && target.kind === "department") {
            await api.updateDepartment(source.id, { parent_id: target.id });
          }
          await reloadOrgTree(companyId);
        },
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "移动失败");
    }
  }

  function recordUndo(entry: { undo: () => Promise<void>; redo: () => Promise<void> }) {
    undoStackRef.current.push(entry);
    redoStackRef.current = [];
  }

  async function undo() {
    const entry = undoStackRef.current.pop();
    if (!entry) return;
    setError(null);
    try {
      await entry.undo();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "撤销失败");
    }
    redoStackRef.current.push(entry);
  }

  async function redo() {
    const entry = redoStackRef.current.pop();
    if (!entry) return;
    setError(null);
    try {
      await entry.redo();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重做失败");
    }
    undoStackRef.current.push(entry);
  }

  async function searchBd(event: FormEvent) {
    event.preventDefault();
    if (!bdQuery.trim()) return;
    setError(null);
    setBdLoading(true);
    setBdProgress(null);
    setBdLeads([]);
    setBdDegradedReason(null);
    try {
      // 每轮综合完就收到一批线索，先渲染出来，不必等整批检索结束。
      const result = await api.runBdAgentStream(
        bdQuery.trim(),
        "text",
        10,
        (p) => setBdProgress(p),
        (leads) => setBdLeads((prev) => sortLeadsByConfidence([...prev, ...leads])),
      );
      setBdSessionId(result.session_id);
      setBdLeads(sortLeadsByConfidence(result.leads));
      setBdDegradedReason(result.degraded_reason ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "线索搜索失败");
    } finally {
      setBdLoading(false);
      setBdProgress(null);
    }
  }

  async function followUpBd(event: FormEvent) {
    event.preventDefault();
    if (!bdFollowUp.trim() || !bdSessionId) return;
    setError(null);
    setBdLoading(true);
    try {
      const result = await api.followUpBdAgent(bdSessionId, bdFollowUp.trim(), 10);
      setBdLeads((prev) => sortLeadsByConfidence([...prev, ...result.leads]));
      setBdDegradedReason(result.degraded_reason ?? null);
      setBdFollowUp("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "追问失败");
    } finally {
      setBdLoading(false);
    }
  }

  async function lookupPool(leadId: string) {
    setError(null);
    setBdPoolBusyId(leadId);
    setCollapsedPool((prev) => ({ ...prev, [leadId]: false }));
    try {
      const matches = await api.lookupPool(leadId);
      setBdPoolByLead((prev) => ({ ...prev, [leadId]: matches }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "反查人才库失败");
    } finally {
      setBdPoolBusyId(null);
    }
  }

  function togglePoolCollapse(leadId: string) {
    setCollapsedPool((prev) => ({ ...prev, [leadId]: !prev[leadId] }));
  }

  async function loadDashboard(filters = dashboardFilters, granularity = trendGranularity) {
    if (filters.date_from && filters.date_to && filters.date_from > filters.date_to) {
      setError("开始日期不能晚于结束日期");
      return;
    }
    const request = ++dashboardRequest.current;
    setError(null);
    setDashboardBusy(true);
    try {
      api.dailyFollowupToday().then(setDailyFollowup).catch(() => setDailyFollowup(null));
      const [overview, byJd, trend] = await Promise.all([
        api.dashboardOverview(filters),
        api.dashboardByJd(filters),
        api.dashboardTrend(granularity, filters),
      ]);
      if (request !== dashboardRequest.current) return;
      setDashboardFilters(filters);
      setTrendGranularity(granularity);
      setDashboard(overview);
      setDashboardByJdData(byJd);
      setDashboardTrend(trend);
    } catch (caught) {
      if (request === dashboardRequest.current) setError(caught instanceof Error ? caught.message : "看板加载失败");
    } finally {
      if (request === dashboardRequest.current) setDashboardBusy(false);
    }
  }

  async function reloadTrend(granularity: string) {
    await loadDashboard(dashboardFilters, granularity);
  }

  /** 以服务端为准回读今日待办，避免本地乐观更新与服务端不一致（例如失败或跨日）。 */
  function refreshDailyFollowup() {
    api.dailyFollowupToday().then(setDailyFollowup).catch(() => undefined);
  }

  /** 勾选今日待办的一项：只记录「今天处理过」，不代表任务完成；次日自动重置。 */
  async function toggleDailyTodo(item: DailyFollowupItem) {
    const done = !item.done;
    setDailyFollowup((prev) => (prev === null ? prev : {
      followup: prev.followup.map((row) => (row.item_key === item.item_key ? { ...row, done } : row)),
      interview: prev.interview.map((row) => (row.item_key === item.item_key ? { ...row, done } : row)),
      reminders: prev.reminders,
    }));
    try {
      await api.checkDailyTodo({ item_key: item.item_key, done });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "待办勾选保存失败");
    } finally {
      refreshDailyFollowup();
    }
  }

  function openCandidateReminder(candidateId: string, name: string) {
    setReminderTarget({ candidateId, name });
  }

  /** 建提醒：建好后出现在「今日待办」的「我的提醒」列。 */
  async function saveCandidateReminder(content: string): Promise<string | null> {
    if (reminderTarget === null) return "未选择候选人";
    setReminderBusy(true);
    try {
      await api.createCandidateReminder({ candidate_id: reminderTarget.candidateId, content });
      setReminderTarget(null);
      refreshDailyFollowup();
      return null;
    } catch (caught) {
      return caught instanceof Error ? caught.message : "保存失败";
    } finally {
      setReminderBusy(false);
    }
  }

  /** 我的提醒的勾选是「任务完成」：勾上即移出列表。 */
  async function completeCandidateReminder(reminder: CandidateReminderItem) {
    setDailyFollowup((prev) => (prev === null ? prev : {
      ...prev,
      reminders: prev.reminders.filter((row) => row.id !== reminder.id),
    }));
    try {
      await api.completeCandidateReminder(reminder.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "提醒完成失败");
    } finally {
      refreshDailyFollowup();
    }
  }

  async function loadSettings() {
    setError(null);
    try {
      setSettings(await api.getSettings());
      setVendors(await api.getVendors());
      const [catalog, config, status] = await Promise.all([
        api.getAiCatalog(),
        api.getAiConfig(),
        api.getAiStatus(),
      ]);
      setAiCatalog(catalog);
      setAiConfig(config);
      setAiStatus(status);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "设置加载失败");
    }
  }

  async function restartApp() {
    try {
      await invoke("restart_app");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重启失败");
    }
  }

  async function saveAiConnection(connection: AiConfigUpdate["connections"][number]): Promise<string | null> {
    setAiBusy(true);
    setAiMessage("");
    try {
      const current = aiConfig?.connections ?? [];
      const mapped = current.map((c) => ({
        connection_id: c.connection_id,
        provider_id: c.provider_id,
        display_name: c.display_name,
        base_url_override: c.base_url_override,
        parameter_style: c.parameter_style,
        models: c.models,
        enabled: c.enabled,
      }));
      const updates = mergeAiConnection(mapped, connection);
      const updated = await api.updateAiConfig({ connections: updates });
      setAiConfig(updated);
      setAiMessage("AI 配置已保存并生效");
      setAiWizard(null);
      return null;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "保存失败";
      setAiMessage(message);
      return message;
    } finally {
      setAiBusy(false);
    }
  }

  async function testProviders() {
    setError(null);
    try {
      setProviderChecks(await api.testProviders());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "API 测试失败");
    }
  }

  async function updateAiConnections(connections: AiConfigUpdate["connections"]): Promise<string | null> {
    setAiBusy(true);
    setAiMessage("");
    try {
      const updated = await api.updateAiConfig({ connections });
      setAiConfig(updated);
      setAiMessage("AI 配置已保存并生效");
      return null;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "保存失败";
      setAiMessage(message);
      return message;
    } finally {
      setAiBusy(false);
    }
  }

  async function refreshAiCatalog() {
    setAiBusy(true);
    setAiMessage("");
    try {
      const result = await api.refreshAiCatalog();
      setAiCatalog(await api.getAiCatalog());
      setAiMessage(result.message || "目录已刷新");
    } catch (caught) {
      setAiMessage(caught instanceof Error ? caught.message : "刷新目录失败");
    } finally {
      setAiBusy(false);
    }
  }

  async function saveAiApiSettings(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const updated = await api.updateSettings({
        siliconflow_api_key: settings.siliconflow_api_key,
        tavily_api_key: settings.tavily_api_key,
      });
      setSettings((prev) => ({ ...prev, ...updated }));
      setAiApiMessage("检索服务配置已保存，重启应用后生效");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "检索服务配置保存失败");
    }
  }

  async function saveMailSettings(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const updated = await api.updateSettings({
        imap_host: settings.imap_host,
        imap_account: settings.imap_account,
        imap_auth_code: settings.imap_auth_code,
        imap_whitelist: settings.imap_whitelist,
        mail_auto_sync: settings.mail_auto_sync,
        smtp_host: settings.smtp_host,
        smtp_port: settings.smtp_port,
        smtp_account: settings.smtp_account,
        smtp_auth_code: settings.smtp_auth_code,
        smtp_ssl: settings.smtp_ssl,
        reminder_to: settings.reminder_to,
        daily_followup_enabled: settings.daily_followup_enabled,
      });
      setSettings((prev) => ({ ...prev, ...updated }));
      setMailMessage("邮箱配置已保存，重启应用后生效");
      // 保存成功后尝试发送绑定确认邮件（失败不阻断保存）
      try {
        await api.sendMailConfirmation();
        setMailMessage("邮箱配置已保存，确认邮件已发送，请查收");
      } catch {
        setMailMessage("邮箱配置已保存，但确认邮件发送失败，请检查 SMTP 配置");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "邮箱配置保存失败");
    }
  }

  async function testMailConfig() {
    setError(null);
    setMailTestResult(null);
    try {
      setMailTestResult(await api.testMail());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "邮箱连接测试失败");
    }
  }

  async function syncMailNow() {
    setError(null);
    setMailSyncMessage("");
    try {
      const result = await api.syncMail();
      setMailSyncMessage(`同步完成：新入库 ${result.ingested} 份简历`);
      await loadMailStatus();
    } catch (caught) {
      setMailSyncMessage(caught instanceof Error ? caught.message : "邮箱同步失败");
    }
  }

  async function loadMailStatus() {
    try {
      setMailStatus(await api.mailStatus());
    } catch {
      setMailStatus(null);
    }
  }

  async function sendFollowupTest() {
    setError(null);
    setMailMessage("");
    try {
      const result = await api.sendFollowupTest();
      setMailMessage(result.message);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "测试报告发送失败");
    }
  }

  function addMailWhitelistTag() {
    const tag = mailWhitelistInput.trim();
    if (!tag) return;
    const existing = (settings.imap_whitelist ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!existing.includes(tag)) existing.push(tag);
    setSettings({ ...settings, imap_whitelist: existing.join(",") });
    setMailWhitelistInput("");
  }

  function removeMailWhitelistTag(tag: string) {
    const existing = (settings.imap_whitelist ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    setSettings({ ...settings, imap_whitelist: existing.filter((s) => s !== tag).join(",") });
  }

  async function saveDataRoot() {
    setError(null);
    setDataRootMessage("");
    try {
      await api.setDataRoot(dataRootInput.trim());
      setDataRootMessage("数据目录已保存，重启应用后生效");
    } catch (caught) {
      setDataRootMessage(caught instanceof Error ? caught.message : "数据目录保存失败");
    }
  }

  async function loadBackups() {
    setError(null);
    setBackupBusy(true);
    try {
      setBackups(await api.listBackups());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "备份列表加载失败");
    } finally {
      setBackupBusy(false);
    }
  }

  async function createBackup() {
    setError(null);
    setBackupBusy(true);
    try {
      await api.createBackup();
      setBackups(await api.listBackups());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建备份失败");
    } finally {
      setBackupBusy(false);
    }
  }

  async function restoreBackupItem(filename: string) {
    const confirmed = window.confirm(
      `确认恢复备份 "${filename}" 到当前数据目录？\n恢复将覆盖当前数据，且需要重启应用后生效。`
    );
    if (!confirmed) return;
    setError(null);
    setBackupBusy(true);
    try {
      await api.restoreBackup(filename);
      setBackups(await api.listBackups());
      setPortableMessage("恢复已准备：请从托盘退出后重新启动。重启后会同步搜索索引，期间部分结果暂不可用。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "恢复失败");
    } finally {
      setBackupBusy(false);
    }
  }

  async function createPortableBackup() {
    if (!portableBackupPath.trim() || !portablePassphrase) return;
    setError(null);
    setPortableBusy(true);
    try {
      const result = await api.createPortableBackup(portableBackupPath.trim(), portablePassphrase);
      setPortableMessage(`便携备份已创建：${result.path}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "便携备份创建失败");
    } finally {
      setPortableBusy(false);
    }
  }

  async function restorePortableBackup() {
    if (!portableRestorePath.trim() || !portableRestoreTarget.trim() || !portablePassphrase) return;
    const confirmed = window.confirm(
      `确认从 "${portableRestorePath.trim()}" 恢复到 "${portableRestoreTarget.trim()}"？\n目标目录将被替换；如需在当前应用使用该数据，请重启并切换数据目录。`
    );
    if (!confirmed) return;
    setError(null);
    setPortableBusy(true);
    try {
      const result = await api.restorePortableBackup(
        portableRestorePath.trim(),
        portableRestoreTarget.trim(),
        portablePassphrase
      );
      setPortableMessage(result.ok
        ? `便携备份恢复并校验完成：${result.files_verified} 个文件。请重启应用并切换数据目录后使用。`
        : "便携备份恢复校验未通过");
    } catch (caught) {
      setPortableMessage(caught instanceof Error ? caught.message : "便携备份恢复失败");
    } finally {
      setPortableBusy(false);
    }
  }

  async function migrateData(event: FormEvent) {
    event.preventDefault();
    if (!migrationTarget.trim()) return;
    setError(null);
    setMigrationMessage("");
    setMigrationBusy(true);
    try {
      setMigrationReport(await api.migrateData(migrationTarget.trim()));
      setMigrationMessage("迁移校验通过，请重启应用并切换到新数据目录后生效");
    } catch (caught) {
      setMigrationReport(null);
      setMigrationMessage(caught instanceof Error ? caught.message : "数据迁移失败");
    } finally {
      setMigrationBusy(false);
    }
  }

  async function loadOnboarding() {
    setError(null);
    try {
      setOnboarding(await api.onboardingStatus());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "启动检查失败");
    }
  }

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const meta = event.metaKey || event.ctrlKey;
      if (!meta) return;

      if (event.key === "k" || event.key === "K") {
        event.preventDefault();
        setActiveNav(0);
        return;
      }
      const digit = Number(event.key);
      if (digit >= 1 && digit <= navigation.length) {
        event.preventDefault();
        setActiveNav(digit - 1);
      }
    }

    function onEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setPreviewUrl(null);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keydown", onEscape);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keydown", onEscape);
    };
  }, []);

  useEffect(() => {
    return () => {
      for (const poll of taskPolls.current.values()) poll.controller.abort();
      taskPolls.current.clear();
    };
  }, []);

  useEffect(() => {
    const release = URL.revokeObjectURL?.bind(URL);
    return () => { if (previewUrl) release?.(previewUrl); };
  }, [previewUrl]);

  useEffect(() => {
    api.getVendors().then(setVendors).catch(() => {});
  }, []);

  // 索引版本升级后后端会自动重建：首屏读一次状态，重建期间轮询进度并在完成后提示一次。
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let sawActive = false;

    async function poll() {
      try {
        const status = await api.indexStatus();
        if (cancelled) return;
        const state = status.rebuild;
        if (!state) return;
        if (!state.finished_at) {
          sawActive = true;
          setIndexRebuild({ state, pending: status.pending });
          timer = setTimeout(() => void poll(), 3000);
          return;
        }
        // 完成状态会长期留在后端，只在本次会话亲眼见过重建进行中时才提示一次，
        // 否则每次启动都会重复播报一条旧消息。
        setIndexRebuild(null);
        if (sawActive) {
          sawActive = false;
          setNotice("搜索索引已按新版本重建完成，之前搜不到的人现在可以搜到了。");
        }
      } catch {
        // 读不到索引状态不打扰用户：重建本身在后端继续进行。
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  function refreshCurrentPage() {
    if (activeNav === 0) void loadCandidates();
    if (activeNav === 1) { void loadJds(); void loadJdsPage(1, jdFilter); }
    if (activeNav === 2) { void loadJds(); void loadCases(); }
    if (activeNav === 3) { void loadJds(); void loadDashboard(); }
    if (activeNav === 4) void loadCompanies();
    if (activeNav === 5) { /* BD 助手线索为本地状态，无独立加载函数；整页刷新由 reloadApp 处理 */ }
    if (activeNav === 6) void loadSettings();
  }

  function reloadApp() {
    window.location.reload();
  }

  useEffect(() => {
    refreshCurrentPage();
  }, [activeNav]);

  const displayOrgTree = useMemo(
    () => (orgTree ? filterOrgTree(orgTree, orgSearch, orgFilterKind, orgFilterKey) : null),
    [orgTree, orgSearch, orgFilterKind, orgFilterKey]
  );

  const selectedOrgNode = useMemo(() => {
    if (!orgTree || !selectedOrgNodeId) return null;
    function find(node: OrgTreeNode): OrgTreeNode | null {
      if (node.id === selectedOrgNodeId) return node;
      for (const child of node.children) {
        const hit = find(child);
        if (hit) return hit;
      }
      return null;
    }
    return find(orgTree);
  }, [orgTree, selectedOrgNodeId]);

  const selectedOrgEmployee = selectedOrgNode?.kind === "employee"
    ? employees.find((e) => e.id === selectedOrgNode.id) ?? null
    : null;
  const selectedOrgDepartment = selectedOrgNode?.kind === "department"
    ? departments.find((d) => d.id === selectedOrgNode.id) ?? null
    : null;

  return (
    <div className={navCollapsed ? "app-shell nav-collapsed" : "app-shell"}>
      <aside className="nav" aria-label="主导航">
        <div className="nav__brand">
          <div className="nav__brand-title">工作台<small>Recruiting Console</small></div>
          <button className="nav__refresh" type="button" title="刷新" aria-label="刷新" onClick={() => reloadApp()}>↻</button>
          <button
            className="nav__collapse"
            type="button"
            aria-label={navCollapsed ? "展开导航" : "收起导航"}
            onClick={() => setNavCollapsed((v) => !v)}
          >
            {navCollapsed ? "»" : "«"}
          </button>
        </div>
        <nav className="nav__list">
          {navigation.map((item, index) => (
            <button
              className={index === activeNav ? "nav__item is-active" : "nav__item"}
              key={item}
              aria-label={item}
              onClick={() => setActiveNav(index)}
            >
              <span className="nav__dot" aria-hidden="true" />
              {item}
            </button>
          ))}
        </nav>
        <div className="nav__foot">本地数据 · 已同步</div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <h1>{navigation[activeNav]}</h1>
            {activeNav === 0 && candidateTotal > 0 && (
              <div className="topbar__sub">共 {candidateTotal} 位候选人</div>
            )}
            {activeNav === 1 && (
              <div className="topbar__sub">共 {jds.filter((jd) => jd.jd_status === "OPEN").length} 个在招岗位</div>
            )}
            {activeNav === 2 && (
              <div className="topbar__sub">招聘流程与面试跟进</div>
            )}
            {activeNav === 3 && (
              <div className="topbar__sub">推荐统计与面试漏斗</div>
            )}
            {activeNav === 4 && (
              <div className="topbar__sub">组织架构与关键人才地图</div>
            )}
            {activeNav === 5 && (
              <div className="topbar__sub">深度检索目标公司与招聘线索</div>
            )}
            {activeNav === 6 && (
              <div className="topbar__sub">服务配置、数据与系统健康</div>
            )}
          </div>
          {activeNav === 0 && (
            <div className="page-head__actions">
              <button className="btn btn-secondary" onClick={() => void loadDirectionPending()}>方向待核</button>
              <button className="btn btn-secondary" onClick={() => void runReparseFailed()}>一键重新生成不合格项</button>
              <label className="btn btn-secondary" style={{ cursor: "pointer" }}>
                文件夹导入
                <input
                  type="file"
                  multiple
                  {...{ webkitdirectory: "" } as Record<string, string>}
                  style={{ display: "none" }}
                  onChange={(e) => void uploadResume(undefined, e.target.files)}
                />
              </label>
              <label className="btn btn-primary" style={{ cursor: "pointer" }}>
                上传简历
                <input
                  aria-label="选择简历文件"
                  accept=".pdf,.doc,.docx"
                  multiple
                  type="file"
                  style={{ display: "none" }}
                  onChange={(event) => void uploadResume(undefined, event.target.files)}
                />
              </label>
            </div>
          )}
          {activeNav === 1 && (
            <div className="page-head__actions">
              <button className="btn btn-primary" onClick={() => setJdImportOpen((v) => !v)}>导入 JD</button>
            </div>
          )}
          {activeNav === 2 && (
            <div className="page-head__actions">
              <button className="btn btn-secondary" onClick={() => loadCases()}>刷新流程</button>
            </div>
          )}
        </header>

        {error && <div className="error-banner" role="alert">{error}</div>}
        {notice && <div className="notice-banner" role="status">{notice}</div>}
        {indexRebuild && (
          <div className="notice-banner" role="status">
            检测到索引版本升级，正在重建搜索索引（已完成 {Math.max(indexRebuild.state.total - indexRebuild.pending, 0)}/{indexRebuild.state.total} 条），期间搜索结果可能不完整。
          </div>
        )}

        {activeNav === 0 && (
          <TalentPoolPage
            query={query}
            onQueryChange={setQuery}
            searchMode={searchMode}
            onSearchModeChange={setSearchMode}
            keywordOperator={keywordOperator}
            onKeywordOperatorChange={setKeywordOperator}
            rewriteEnabled={rewriteEnabled}
            onRewriteEnabledChange={setRewriteEnabled}
            searchBody={searchBody}
            onSearchBodyChange={setSearchBody}
            parseEnabled={parseEnabled}
            onParseEnabledChange={setParseEnabled}
            searching={searching}
            searchFilterDraft={searchFilterDraft}
            onSearchFilterChange={setSearchFilterDraft}
            batchProgress={batchProgress}
            batchTaskIds={batchTaskIds}
            batchTimedOut={batchTimedOut}
            hasSearched={hasSearched}
            results={results}
            searchConditions={searchConditions}
            relaxedSearchFields={relaxedSearchFields}
            searchPlanEcho={searchPlanEcho}
            removedConditions={removedConditions}
            onRemoveSearchCondition={removeSearchCondition}
            onEditSearchCondition={editSearchCondition}
            onRestoreSearchCondition={restoreSearchCondition}
            onDropInferredConditions={dropInferredConditionsAndSearch}
            candidates={candidates}
            candidatePage={candidatePage}
            candidateTotal={candidateTotal}
            candidateTotalPages={candidateTotalPages}
            candidatePageSize={candidatePageSize}
            onCandidatePageSizeChange={changeCandidatePageSize}
            visibleColumns={visibleColumns}
            columnOrder={columnOrder}
            columnsMenuOpen={columnsMenuOpen}
            candidateListRef={candidateListRef}
            onSubmitSearch={submitSearch}
            onContinueBatchPolling={continueBatchPolling}
            onLoadCandidates={loadCandidates}
            onCloseSearchResults={closeSearchResults}
            onResetSearch={resetSearchAndGoHome}
            onToggleColumn={toggleCandidateColumn}
            onMoveColumn={moveCandidateColumn}
            onColumnsMenuOpenChange={setColumnsMenuOpen}
            onScrollTop={scrollToCandidateListTop}
            onUpdateField={updateCandidateFieldValue}
            onSaveCommunicationNote={saveCommunicationNote}
            onEditEducation={(candidateId, educations) => setEducationEditor({ candidateId, educations })}
            onEditProfile={(candidateId, summary, source, stale) => setProfileEditor({ candidateId, summary, source, stale })}
            onMatch={runCandidateMatch}
            onPreview={previewResumeFile}
            onDownload={downloadResumeFile}
            onCreateCase={openCreateCasePicker}
            onCreateReminder={openCandidateReminder}
            onReparse={reparseResumeSoft}
            onForceReparse={forceReparse}
            onOpenReview={openResumeReview}
            onOpenParsed={(candidateId, parsed) => setCandidateParsedEditor({ candidateId, parsed })}
            onDelete={deleteCandidateRow}
            selectedCandidateIds={selectedCandidateIds}
            bulkBusy={bulkBusy}
            onToggleCandidateSelect={toggleCandidateSelect}
            onToggleCandidateSelectAll={toggleCandidateSelectAll}
            onBulkDelete={() => void bulkDeleteSelectedCandidates()}
            onBulkReparse={(revisionIds) => void bulkReparseSelected(revisionIds)}
            onBulkForceOcr={(revisionIds) => void bulkForceOcrSelected(revisionIds)}
            onBulkMatch={() => void bulkMatchSelectedCandidates()}
            onBulkDownload={() => void bulkDownloadSelectedCandidates()}
            searchReview={searchReview}
            selectedSearchCount={selectedSearchCandidateIds.length}
            onBulkSearchReview={() => void runSearchReview(selectedSearchCandidateIds)}
            onCancelSearchReview={() => void cancelSearchReview()}
            onRetrySearchReview={() => void retrySearchReview()}
          />
        )}

        {directionPendingOpen && (
          <div className="direction-pending" role="region" aria-label="方向待核"
               style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12, marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{directionPending.length} 人方向待核</strong>
              <button className="btn btn-secondary" onClick={() => setDirectionPendingOpen(false)}>关闭</button>
            </div>
            {directionPending.length === 0 ? (
              <p className="muted">暂无待核方向</p>
            ) : (
              <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
                {directionPending.map((c) => {
                  // 控件展示值与确认提交值必须同源，否则「已存在方向」的行会出现
                  // 显示了方向但确认按钮不可点（或提交空数组）的矛盾。
                  const resolved = directionPendingDraft[c.candidate_id]
                    ?? c.parsed_data?.career_directions ?? [];
                  return (
                    <li key={c.candidate_id}
                        style={{ display: "flex", gap: 12, alignItems: "center", padding: "6px 0", borderBottom: "1px solid #eee" }}>
                      <span style={{ minWidth: 120 }}>{c.display_name}</span>
                      <span className="muted" style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {c.original_filename}
                      </span>
                      <CareerDirectionPicker
                        ariaLabel={`${c.display_name || c.candidate_id} 职业方向`}
                        showSpecializations={false}
                        maxDirections={2}
                        directions={resolved}
                        specializations={[]}
                        onChange={(next) => setDirectionPendingDraft(
                          (draft) => ({ ...draft, [c.candidate_id]: next.directions }))}
                      />
                      <button
                        type="button"
                        className="btn btn-secondary btn-xs"
                        disabled={resolved.length === 0}
                        onClick={() => void setCandidateCareerDirections(c.candidate_id, resolved)}
                      >确认</button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}

        {activeNav === 1 && (
          <JdManagementPage
            jdSource={jdSource}
            onJdSourceChange={setJdSource}
            jdResult={jdResult}
            jds={jdPageItems}
            jdPage={jdPage}
            jdTotal={jdTotal}
            jdTotalPages={jdTotalPages}
            openJdSource={openJdSource}
            jdFilter={jdFilter}
            onJdFilterChange={setJdFilter}
            jdImportOpen={jdImportOpen}
            jdMatchMode={jdMatchMode}
            onJdMatchModeChange={setJdMatchMode}
            onSubmitJd={submitJd}
            onUploadJd={uploadJd}
            onLoadJds={loadJdsPage}
            onUpdateJdStatus={updateJdStatus}
            onUpdateJdField={updateJdFieldValue}
            onSaveJdProfile={saveJdProfileInline}
            onRegenerateJdProfile={regenerateJdProfile}
            onMatchJd={runJdMatch}
            onDeleteJd={deleteJd}
            onOpenParsed={(jd) => setJdParsedEditor({ jdId: jd.jd_id, title: jd.title, parsed: jd.parsed_data })}
            onReparseFailed={runReparseFailed}
            onToggleJdSource={toggleJdSource}
            jdMatch={jdMatch}
            jdReview={jdReview}
            jdMatchingId={jdMatchingId}
            onJdAiReview={runJdAiReview}
            onJdAiReviewCancel={() => void cancelAiReview(jdReview, setJdReview)}
            onJdAiReviewRetry={() => void retryAiReview(jdMatch?.run_id ?? null, jdReview, setJdReview)}
            jdReviewReasoning={jdReviewReasoning}
            onJdReviewReasoningChange={setJdReviewReasoning}
            visibleColumns={visibleColumns}
            columnOrder={columnOrder}
            onToggleColumn={toggleCandidateColumn}
            matchCaseIds={matchCaseIds}
            creatingCase={creatingCase}
            onPreviewResume={previewResumeFile}
            onCreateCaseFromMatch={createCaseFromDrawer}
            onEditProfile={(candidateId, summary, source, stale) => setProfileEditor({ candidateId, summary, source, stale })}
            onBulkDeleteJds={(ids) => void bulkDeleteJdsHandler(ids)}
          />
        )}

        {activeNav === 3 && (
          <DashboardPage
            dashboard={dashboard}
            dashboardByJdData={dashboardByJdData}
            dashboardTrend={dashboardTrend}
            trendGranularity={trendGranularity}
            dashboardDraft={dashboardDraft}
            dashboardBusy={dashboardBusy}
            dailyFollowup={dailyFollowup}
            onToggleTodo={toggleDailyTodo}
            onCompleteReminder={completeCandidateReminder}
            jds={jds}
            onDashboardDraftChange={setDashboardDraft}
            onApplyFilters={loadDashboard}
            onReload={() => void loadDashboard()}
            onReloadTrend={reloadTrend}
            onExportExcel={() => void api.dashboardExport(dashboardFilters).catch((caught) => setError(caught instanceof Error ? caught.message : "导出失败"))}
          />
        )}

        {activeNav === 2 && (
          <RecruitmentPage
            caseJdFilter={caseJdFilter}
            cases={cases}
            casePage={casePage}
            caseTotal={caseTotal}
            caseTotalPages={caseTotalPages}
            jds={jds}
            onCaseJdFilterChange={setCaseJdFilter}
            onLoadCases={loadCases}
            onOpenCase={openCaseDrawer}
            onDeleteCase={deleteCase}
            onBulkDeleteCases={(ids) => void bulkDeleteCasesHandler(ids)}
          />
        )}

        {activeNav === 4 && (
          <MappingPage
            companyName={companyName}
            onCompanyNameChange={setCompanyName}
            companies={companies}
            selectedCompanyId={selectedCompanyId}
            selectedOrgNode={selectedOrgNode}
            selectedOrgNodeId={selectedOrgNodeId}
            selectedOrgEmployee={selectedOrgEmployee}
            selectedOrgDepartment={selectedOrgDepartment}
            employees={employees}
            departments={departments}
            displayOrgTree={displayOrgTree}
            orgSearch={orgSearch}
            orgFilterKind={orgFilterKind}
            orgFilterKey={orgFilterKey}
            leftCollapsed={leftCollapsed}
            rightCollapsed={rightCollapsed}
            pendingEditId={pendingEditId}
            orgImportText={orgImportText}
            orgImportFileName={orgImportFileName}
            orgImportDraft={orgImportDraft}
            orgImportQuestions={orgImportQuestions}
            orgImportAnswers={orgImportAnswers}
            orgImportBusy={orgImportBusy}
            orgImportMessage={orgImportMessage}
            orgReviseInstruction={orgReviseInstruction}
            orgSource={orgSource}
            onCreateCompany={createCompany}
            onLoadCompanies={loadCompanies}
            onAddOrgPerson={addOrgPerson}
            onUndo={undo}
            onRedo={redo}
            onOrgImportTextChange={setOrgImportText}
            onParseOrgImportFile={parseOrgImportFile}
            onOrgImportFileNameChange={setOrgImportFileName}
            onParseOrgImportText={parseOrgImportText}
            onCommitOrgImport={commitOrgImport}
            onAnswerOrgImportDraft={answerOrgImportDraft}
            onOrgImportAnswersChange={setOrgImportAnswers}
            onReviseOrgImportDraft={reviseOrgImportDraft}
            onOrgReviseInstructionChange={setOrgReviseInstruction}
            onUpdateOrgImportCompanyName={updateOrgImportCompanyName}
            onUpdateOrgImportDepartment={updateOrgImportDepartment}
            onUpdateOrgImportEmployee={updateOrgImportEmployee}
            onSelectCompany={selectCompany}
            onToggleOrgSource={toggleOrgSource}
            onDeleteCompany={deleteCompany}
            onOrgSourceChange={setOrgSource}
            onOrgSearchChange={setOrgSearch}
            onOrgFilterKindChange={setOrgFilterKind}
            onOrgFilterKeyChange={setOrgFilterKey}
            onLeftCollapsedChange={setLeftCollapsed}
            onRightCollapsedChange={setRightCollapsed}
            onSelectOrgNode={setSelectedOrgNodeId}
            onRenameOrgNode={renameOrgNode}
            onAddOrgChild={addOrgChild}
            onAddOrgSibling={addOrgSibling}
            onDeleteOrgNode={deleteOrgNode}
            onMoveOrgNode={moveOrgNode}
            onPendingEditConsumed={() => setPendingEditId(null)}
            onUpdateOrgEmployeeField={updateOrgEmployeeField}
            onUpdateOrgDepartmentField={updateOrgDepartmentField}
            onBindOrgEmployee={bindOrgEmployee}
            onPreviewResume={previewResumeFile}
            onExportOrgInternal={(id) => void api.exportOrgInternal(id)}
            onExportOrgClient={(id) => void api.exportOrgClient(id)}
            onExportOrgArchPdf={(id) => void api.exportOrgArchPdf(id)}
          />
        )}

        {activeNav === 5 && (
          <BdAssistantPage
            bdQuery={bdQuery}
            onBdQueryChange={setBdQuery}
            bdFollowUp={bdFollowUp}
            onBdFollowUpChange={setBdFollowUp}
            bdSessionId={bdSessionId}
            bdLeads={bdLeads}
            bdDegradedReason={bdDegradedReason}
            bdLoading={bdLoading}
            bdProgress={bdProgress}
            bdPoolByLead={bdPoolByLead}
            bdPoolBusyId={bdPoolBusyId}
            collapsedPool={collapsedPool}
            onSearchBd={searchBd}
            onFollowUpBd={followUpBd}
            onLookupPool={lookupPool}
            onTogglePoolCollapse={togglePoolCollapse}
            onOpenExternal={openExternal}
            onCopyLink={copyLink}
            onPreviewResume={previewResumeFile}
          />
        )}

        {activeNav === 6 && (
          <SettingsPage
            api={api}
            onboarding={onboarding}
            settings={settings}
            vendors={vendors}
            providerChecks={providerChecks}
            aiApiMessage={aiApiMessage}
            aiCatalog={aiCatalog}
            aiConfig={aiConfig}
            aiStatus={aiStatus}
            aiBusy={aiBusy}
            aiMessage={aiMessage}
            aiWizard={aiWizard}
            aiAdvanced={aiAdvanced}
            onAddAiService={(slot) => setAiWizard(slot)}
            onCloseAiWizard={() => setAiWizard(null)}
            onToggleAiAdvanced={() => setAiAdvanced((v) => !v)}
            onSaveAiConnection={saveAiConnection}
            onUpdateAiConnections={updateAiConnections}
            onRefreshAiCatalog={refreshAiCatalog}
            mailMessage={mailMessage}
            mailTestResult={mailTestResult}
            mailSyncMessage={mailSyncMessage}
            mailStatus={mailStatus}
            mailWhitelistInput={mailWhitelistInput}
            dataRootInput={dataRootInput}
            dataRootMessage={dataRootMessage}
            health={health}
            backups={backups}
            portableBackupPath={portableBackupPath}
            portableRestorePath={portableRestorePath}
            portableRestoreTarget={portableRestoreTarget}
            portablePassphrase={portablePassphrase}
            portableMessage={portableMessage}
            backupBusy={backupBusy}
            portableBusy={portableBusy}
            migrationTarget={migrationTarget}
            migrationReport={migrationReport}
            migrationBusy={migrationBusy}
            migrationMessage={migrationMessage}
            backfillMessage={backfillMessage}
            tasks={tasks}
            onSettingsChange={setSettings}
            onMailWhitelistInputChange={setMailWhitelistInput}
            onDataRootInputChange={setDataRootInput}
            onPortableBackupPathChange={setPortableBackupPath}
            onPortablePassphraseChange={setPortablePassphrase}
            onPortableRestorePathChange={setPortableRestorePath}
            onPortableRestoreTargetChange={setPortableRestoreTarget}
            onMigrationTargetChange={setMigrationTarget}
            onLoadSettings={loadSettings}
            onLoadOnboarding={loadOnboarding}
            onRestartApp={restartApp}
            onSaveAiApiSettings={saveAiApiSettings}
            onTestProviders={testProviders}
            onSaveMailSettings={saveMailSettings}
            onTestMailConfig={testMailConfig}
            onSyncMailNow={syncMailNow}
            onLoadMailStatus={loadMailStatus}
            onSendFollowupTest={sendFollowupTest}
            onAddMailWhitelistTag={addMailWhitelistTag}
            onRemoveMailWhitelistTag={removeMailWhitelistTag}
            onSaveDataRoot={saveDataRoot}
            onCheckHealth={checkHealth}
            onExportDiagnostics={exportDiagnostics}
            onLoadBackups={loadBackups}
            onCreateBackup={createBackup}
            onRestoreBackupItem={restoreBackupItem}
            onCreatePortableBackup={createPortableBackup}
            onRestorePortableBackup={restorePortableBackup}
            onRunBackfill={runBackfill}
            onControlTask={controlTask}
            onMigrateData={migrateData}
            onOpenCase={openCaseDrawer}
          />
        )}
      </main>

      {reminderTarget && (
        <CandidateReminderDialog
          candidateName={reminderTarget.name}
          busy={reminderBusy}
          onClose={() => setReminderTarget(null)}
          onSave={saveCandidateReminder}
        />
      )}

      {matchDrawer && (
        <div className="match-drawer-backdrop" onClick={() => setMatchDrawer(null)}>
          <aside className="match-drawer" role="dialog" aria-modal="true" aria-label="匹配结果" onClick={(e) => e.stopPropagation()}>
            <div className="match-drawer-header">
              <div>
                <h2>{matchDrawer.title}</h2>
                <small>{matchDrawer.items.length} 条匹配</small>
              </div>
              <div className="case-actions">
                {matchDrawer.mode === "jds" && drawerSingleRunId && (
                  <>
                    {(!drawerSingleReview || drawerSingleReview.status === "SUCCESS") && (
                      <>
                        <label className="muted" style={{ fontSize: 12, marginRight: 8, cursor: "pointer" }}>
                          <input type="checkbox" checked={candidateReviewReasoning} onChange={(e) => setCandidateReviewReasoning(e.target.checked)} style={{ marginRight: 4 }} />
                          深度思考
                        </label>
                        <button className="detail-button" onClick={() => void runAiReview(drawerSingleRunId)}>
                          {drawerSingleReview ? "重新复核" : "AI 深度复核"}
                        </button>
                      </>
                    )}
                    {drawerSingleReview && ["FAILED", "DEAD_LETTER", "CANCELLED"].includes(drawerSingleReview.status) && (
                      <button className="detail-button" onClick={() => void retryAiReview(drawerSingleRunId, drawerSingleReview, reviewSetterFor(drawerSingleRunId))}>重试复核</button>
                    )}
                    {drawerSingleReview && !["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"].includes(drawerSingleReview.status) && (
                      <>
                        <span className="muted" style={{ fontSize: 12, marginRight: 8 }}>AI 复核 {drawerSingleReview.progress}%</span>
                        <button className="detail-button" onClick={() => void cancelAiReview(drawerSingleReview, reviewSetterFor(drawerSingleRunId))}>取消复核</button>
                      </>
                    )}
                  </>
                )}
                <button className="detail-button" onClick={() => setMatchDrawer(null)}>关闭</button>
              </div>
            </div>
            {matchDrawer.mode === "jds" && (Object.keys(drawerVerdicts).length > 0 || drawerSingleReview?.error_message) && (
              <div className="muted" style={{ padding: "0 16px 8px" }}>
                {Object.keys(drawerVerdicts).length > 0 && (
                  <>
                    推荐 {drawerVerdictCounts.recommend} / 待核 {drawerVerdictCounts.pending} / 不推荐 {drawerVerdictCounts.reject}
                    {drawerVerdictFailed > 0 && `（复核失败 ${drawerVerdictFailed}）`}
                  </>
                )}
                {/* 任务失败或轮询超时的原因：不显示的话用户只看到「复核中」或「全是待核」。 */}
                {drawerSingleReview?.error_message && <span role="status"> {drawerSingleReview.error_message}</span>}
              </div>
            )}
            {matchDrawer.mode === "jds" && drawerSources.length > 0 && (
              <div className="muted" style={{ padding: "0 16px 8px", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <label htmlFor="match-drawer-candidate">查看候选人</label>
                <select
                  id="match-drawer-candidate"
                  aria-label="按候选人筛选匹配结果"
                  value={drawerCandidateFilter}
                  onChange={(event) => { setDrawerCandidateFilter(event.target.value); setMatchPage(1); }}
                >
                  <option value="">全部（{matchDrawer.items.length} 条）</option>
                  {drawerSources.map((source) => (
                    <option key={source.candidate_id} value={source.candidate_id}>
                      {source.name}（{matchDrawer.items.filter((item) => item.candidate_id === source.candidate_id).length} 条）
                    </option>
                  ))}
                </select>
                <label style={{ cursor: "pointer" }}>
                  <input type="checkbox" checked={candidateReviewReasoning} onChange={(e) => setCandidateReviewReasoning(e.target.checked)} style={{ marginRight: 4 }} />
                  深度思考
                </label>
                {reviewingRunIds.length > 0 && <span>AI 复核进行中：{reviewingRunIds.length} 人</span>}
              </div>
            )}
            <div className="match-drawer-body">
              {matchDrawer.items.length === 0 ? (
                <div className="empty-state">
                  <strong>暂无匹配</strong>
                  <p>没有满足条件的匹配结果。</p>
                </div>
              ) : matchDrawer.mode === "candidates" ? (
                <table className="drawer-table">
                  <thead><tr><th>姓名</th><th>学历</th><th>电话</th><th>行业</th><th>操作</th></tr></thead>
                  <tbody>
                    {matchDrawer.items.map((item) => {
                      const p = item.parsed_data;
                      const status = matchDrawer.statuses[item.result_id || ""] ?? "未处理";
                      const industry = p?.current_industry || p?.longest_industry
                        ? `${p.current_industry ? `最近：${p.current_industry}` : ""}${p.longest_industry ? ` 最长：${p.longest_industry}` : ""}`
                        : "—";
                      return (
                        <Fragment key={item.candidate_id}>
                          <tr>
                            <td><strong>{item.name}</strong></td>
                            <td>{eduLabel(p) || "—"}</td>
                            <td>{item.phone || "—"}</td>
                            <td>{industry}</td>
                            <td>
                              <div className="case-actions">
                                <button className="detail-button" onClick={() => void previewResumeFile(item.revision_id, item.name, item.original_filename ?? "")}>查看详情</button>
                                <button className="detail-button" onClick={() => void downloadResumeFile(item.revision_id, item.original_filename ?? item.name)}>下载</button>
                                <button className="detail-button" disabled={creatingCase || !item.result_id} onClick={() => void createCaseFromDrawer(item.result_id || "")}>{matchCaseIds[item.result_id || ""] ? "查看流程" : "建流程"}</button>
                                {status !== "未处理" && <span className="match-status">{status}</span>}
                              </div>
                            </td>
                          </tr>
                          {(p?.experiences?.length || p?.projects?.length) ? (
                            <tr>
                              <td colSpan={5}>
                                {p.experiences && p.experiences.length > 0 && (
                                  <div className="candidate-line">
                                    <small>工作履历</small>
                                    {p.experiences.map((e, i) => {
                                      const head = [e.title, e.company].filter(Boolean).join(" @ ");
                                      const tail = [e.industry ? `（${e.industry}）` : "", e.summary].filter(Boolean).join("，");
                                      return <div key={i}>{[head, tail].filter(Boolean).join("，")}</div>;
                                    })}
                                  </div>
                                )}
                                {p.projects && p.projects.length > 0 && (
                                  <div className="candidate-line">
                                    <small>项目经验</small>
                                    {p.projects.map((pr, i) => (
                                      <div key={i}>{pr.name ?? ""}{pr.tech_stack ? `（${pr.tech_stack}）` : ""}{pr.business_scene ? `，${pr.business_scene}` : ""}</div>
                                    ))}
                                  </div>
                                )}
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <>
                  <table className="drawer-table">
                  <thead><tr>{drawerSources.length > 0 && <th>候选人</th>}<th>岗位名称</th><th>公司</th><th>候选人画像</th><th>符合点</th><th>风险点</th><th>查看详情</th><th>操作</th></tr></thead>
                  <tbody>
                    {(() => {
                      const reviewDone = Object.keys(drawerVerdicts).length > 0;
                      const sorted = sortReviewItems(drawerItems, drawerVerdicts, reviewDone);
                      return sorted.slice((matchPage - 1) * 10, matchPage * 10).map((item) => {
                        const profile = item.parsed_data?.candidate_profile || "";
                        const verdict = drawerVerdicts[item.result_id];
                        // 符合点：三段（项目 → 工作经历 → 技术栈）按顺序拼接；风险点单列。
                        const reasonsText = reviewMatchLines(verdict).join("\n");
                        const cautionsText = (verdict?.risks ?? []).join("\n");
                        // 批量匹配时每行带自己的 run（按行复核）；单人匹配时共用抽屉级 run。
                        const rowRunId = item.run_id ?? drawerSingleRunId;
                        return (
                          <tr key={`${item.candidate_id ?? ""}:${item.jd_id}`}>
                            {drawerSources.length > 0 && (
                              <td>
                                <strong>{item.candidate_name}</strong>
                              </td>
                            )}
                            <td>
                              <strong>{item.title}</strong>
                              {item.business_match && (
                                <span className="muted" title="业务方向与本人一致，优先展示" style={{ marginLeft: 8, fontSize: 12 }}>业务一致</span>
                              )}
                              {verdict && (
                                <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>{reviewVerdictLabel(verdict.verdict)}</span>
                              )}
                              {verdict?.failed && (
                                <span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>复核失败</span>
                              )}
                              {verdict?.skipped && (
                                <span className="muted" title={(verdict.risks || []).join("\n")} style={{ marginLeft: 6, fontSize: 12 }}>未复核</span>
                              )}
                            </td>
                            <td>{item.company}</td>
                            <td>
                              <button className="cell-edit" onClick={() => setJdProfileEditor({ jdId: item.jd_id, title: item.title, profile: item.parsed_data?.candidate_profile ?? null, constraints: item.parsed_data?.exact_constraints ?? [] })}>
                                <HoverText text={profile} />
                              </button>
                            </td>
                            <td className="col-review">
                              <HoverText text={reasonsText} />
                            </td>
                            <td className="col-review">
                              <HoverText text={cautionsText} />
                            </td>
                            <td>
                              {item.resume_revision_id && (
                                <button className="detail-button" onClick={() => void previewResumeFile(item.resume_revision_id || "")}>查看详情</button>
                              )}
                            </td>
                            <td>
                              <div className="case-actions">
                                <button className="detail-button" disabled={creatingCase || (!item.case_id && item.jd_status !== "OPEN")} onClick={() => item.case_id ? void openCaseDrawer(item.case_id) : void createCaseFromDrawer(item.result_id)}>{item.case_id || matchCaseIds[item.result_id] ? "查看流程" : "建流程"}</button>
                                {drawerSources.length > 0 && renderRunReviewAction(rowRunId ?? null)}
                              </div>
                            </td>
                          </tr>
                        );
                      });
                    })()}
                  </tbody>
                  </table>
                  <div className="pagination" style={{ padding: 12 }}>
                    <button className="page-number" disabled={matchPage <= 1} onClick={() => setMatchPage((p) => p - 1)}>上一页</button>
                    <span style={{ fontSize: 13, color: "#6b7280" }}>第 {matchPage} / {Math.max(1, Math.ceil(drawerItems.length / 10))} 页</span>
                    <button className="page-number" disabled={matchPage >= Math.ceil(drawerItems.length / 10)} onClick={() => setMatchPage((p) => p + 1)}>下一页</button>
                  </div>
                </>
              )}
            </div>
          </aside>
        </div>
      )}

      {caseDrawer && <CaseDrawer key={caseDrawer.id} api={api} initialCase={caseDrawer} onClose={() => setCaseDrawer(null)} onUpdated={(detail) => { setCaseDrawer(detail); setCases((items) => items.map((item) => item.id === detail.id ? detail : item)); }} />}
      {resumeReview && <ResumeReviewDrawer key={resumeReview.revision_id} api={api} initialReview={resumeReview} onClose={() => setResumeReview(null)} onReparsed={() => void loadCandidates()} onReparse={reparseResumeSoft} onForceReparse={forceReparse} />}

      {educationEditor && (
        <CandidateEducationEditor
          educations={educationEditor.educations}
          onSave={(educations) => saveEducations(educations)}
          onClose={() => setEducationEditor(null)}
        />
      )}

      {profileEditor && (
        <CandidateProfileEditor
          candidateId={profileEditor.candidateId}
          summary={profileEditor.summary}
          source={profileEditor.source}
          onSave={(summary) => saveProfile(summary)}
          onRegenerate={(instruction, onStage) => regenerateProfile(instruction, onStage)}
          onClose={() => setProfileEditor(null)}
        />
      )}

      {jdProfileEditor && (
        <JdProfileEditor
          jdId={jdProfileEditor.jdId}
          title={jdProfileEditor.title}
          profile={jdProfileEditor.profile}
          constraints={jdProfileEditor.constraints}
          onSave={(jdId, profile, constraints, years) => saveJdProfileAndConstraints(jdId, profile, constraints, years)}
          onRegenerate={(jdId, instruction, onStage) => regenerateJdProfile(jdId, instruction, onStage)}
          onParseConstraints={(text) => api.parseJdConstraints(text).then((r) => ({
            constraints: r.constraints, minYears: r.min_years, yearsStated: r.years_stated,
          }))}
          onClose={() => setJdProfileEditor(null)}
        />
      )}

      {candidateParsedEditor && (
        <CandidateParsedEditor
          candidateId={candidateParsedEditor.candidateId}
          parsed={candidateParsedEditor.parsed}
          onSave={(parsed) => saveCandidateParsed(candidateParsedEditor.candidateId, parsed)}
          onClose={() => setCandidateParsedEditor(null)}
        />
      )}

      {jdParsedEditor && (
        <JdParsedEditor
          jdId={jdParsedEditor.jdId}
          title={jdParsedEditor.title}
          parsed={jdParsedEditor.parsed}
          onSave={(parsed) => saveJdParsed(jdParsedEditor.jdId, parsed)}
          onClose={() => setJdParsedEditor(null)}
        />
      )}

      {previewUrl && (
        <div className="preview-overlay" onClick={() => { URL.revokeObjectURL(previewUrl); setPreviewUrl(null); }}>
          <div className="preview-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="preview-header">
              <span>{previewName}</span>
              <button className="detail-button" onClick={() => { URL.revokeObjectURL(previewUrl); setPreviewUrl(null); }}>关闭</button>
            </div>
            <iframe src={previewUrl} title={previewName} className="preview-frame" />
          </div>
        </div>
      )}

      {createCasePicker && (
        <div className="preview-overlay" onClick={() => setCreateCasePicker(null)}>
          <div className="preview-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="preview-header">
              <span>为「{createCasePicker.name}」选择岗位创建流程</span>
              <button className="detail-button" onClick={() => setCreateCasePicker(null)}>关闭</button>
            </div>
            <div className="create-case-picker-body">
              {jds.filter((jd) => jd.jd_status === "OPEN").length === 0 ? (
                <p>暂无「开放」状态的岗位，请先在 JD 管理中新增岗位。</p>
              ) : (
                jds.filter((jd) => jd.jd_status === "OPEN").map((jd) => (
                  <button key={jd.jd_id} className="jd-pick-item" disabled={creatingCase} onClick={() => void confirmCreateCase(jd.jd_id)}>
                    <strong>{jd.title}</strong>
                    <span>{jd.company || "未填写公司"}{jd.location ? ` · ${jd.location}` : ""}</span>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
