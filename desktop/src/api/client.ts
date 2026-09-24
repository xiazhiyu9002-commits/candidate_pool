import { invoke } from "@tauri-apps/api/core";

import type {
  AiCatalog,
  AiConfig,
  AiConfigUpdate,
  AiStatus,
  ConnectionProbeReport,
} from "../ai/types";
import type { JdExactConstraint } from "../components/JdProfileEditor";
import type {
  AppSettings,
  BackupSnapshot,
  BdAgentLead,
  BdAgentQueryResult,
  BdLead,
  BdPoolCandidate,
  BdProgress,
  CandidateContact,
  CandidateListItem,
  CandidatePage,
  CandidateSearchFilters,
  CandidateSearchResult,
  CaseItem,
  CasePage,
  CaseActionInput,
  CaseDetail,
  CaseEventItem,
  CaseRoundItem,
  CandidateReminderItem,
  CorrectionRecord,
  DashboardByJd,
  DashboardOverview,
  DashboardTrendItem,
  DailyFollowupToday,
  DiagnosticsData,
  ImportedResume,
  CreateReminderInput,
  IndexSyncStatus,
  JdListItem,
  JdPage,
  JdParsedData,
  MappingProject,
  MappingSnapshot,
  MappingTreeNode,
  MatchCandidateItem,
  MatchMarkStatus,
  MatchResultGroup,
  MigrationReport,
  OnboardingStatus,
  CreateOrgDepartmentInput,
  CreateOrgEmployeeInput,
  UpdateOrgDepartmentInput,
  UpdateOrgEmployeeInput,
  OrgCompany,
  OrgDepartment,
  OrgEmployee,
  OrgImportDraft,
  OrgParseResult,
  BindEmployeeResult,
  BulkCandidateMatchResult,
  OrgTreeNode,
  ParsedResumeData,
  ProfilePointData,
  ProviderCheck,
  RecruitmentApi,
  ReminderItem,
  ReverseMatchItem,
  ResumeRevision,
  ResumeReview,
  SearchReviewStatus,
  TaskAction,
  TaskStatus,
  VendorPreset
} from "../App";


export interface RuntimeConfig {
  apiBaseUrl: string;
  sessionToken: string;
}

export type CandidateKeywordOperator = "smart" | "and" | "or";

export interface CandidateSearchOptions {
  mode: "keyword" | "vector" | "hybrid";
  operator: CandidateKeywordOperator;
  rewriteEnabled: boolean;
  searchBody?: boolean;
  /** AI 智能解析：把输入框自然语言拆成硬条件 + 词条 + 语义查询（默认关闭，三种模式均可用）。 */
  parseEnabled?: boolean;
  limit?: number;
  offset?: number;
}

interface ApiError {
  message?: string;
  detail?: string;
}

class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiRequestError";
  }
}

/** 画像生成的阶段性反馈（服务端推送，文案由服务端给）。 */
export interface ProfileGenerationProgress {
  /** 阶段标识：`loading` / `draft` / `repair`。 */
  stage: string;
  /** 直接展示给使用者的中文说明。 */
  message: string;
}

export interface DashboardQuery {
  company?: string;
  jd_id?: string;
  date_from?: string;
  date_to?: string;
}

function _dashQuery(filters?: DashboardQuery): string {
  if (!filters) return "";
  const parts: string[] = [];
  if (filters.company) parts.push(`company=${encodeURIComponent(filters.company)}`);
  if (filters.jd_id) parts.push(`jd_id=${encodeURIComponent(filters.jd_id)}`);
  if (filters.date_from) parts.push(`date_from=${encodeURIComponent(filters.date_from)}`);
  if (filters.date_to) parts.push(`date_to=${encodeURIComponent(filters.date_to)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}


export class ApiClient implements RecruitmentApi {
  private readonly baseUrl: string;

  constructor(
    baseUrl: string,
    private readonly sessionToken: string,
    private readonly fetcher: typeof fetch = fetch.bind(globalThis)
  ) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  importResume(file: File): Promise<ImportedResume> {
    const form = new FormData();
    form.append("file", file);
    return this.request<ImportedResume>("/api/resumes/import", {
      body: form,
      method: "POST"
    });
  }

  importFolder(directory: string) {
    return this.request<{ imported: ImportedResume[]; skipped: string[]; errors: string[] }>(
      "/api/resumes/import-folder",
      {
        body: JSON.stringify({ directory }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  onboardingStatus() {
    return this.request<OnboardingStatus>("/api/onboarding/status");
  }

  testProviders() {
    return this.request<ProviderCheck[]>("/api/onboarding/test-providers", {
      method: "POST"
    });
  }

  getTask(taskId: string): Promise<TaskStatus> {
    return this.request<TaskStatus>(`/api/tasks/${encodeURIComponent(taskId)}`);
  }

  listTasks(): Promise<TaskStatus[]> {
    return this.request<TaskStatus[]>("/api/tasks");
  }

  getTaskStatusBatch(taskIds: string[]): Promise<{ found: TaskStatus[]; missing_ids: string[] }> {
    return this.request<{ found: TaskStatus[]; missing_ids: string[] }>(
      "/api/tasks/status-batch",
      {
        body: JSON.stringify({ task_ids: taskIds }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  controlTask(taskId: string, action: TaskAction): Promise<TaskStatus> {
    return this.request<TaskStatus>(
      `/api/tasks/${encodeURIComponent(taskId)}/${action}`,
      { method: "POST" }
    );
  }

  triggerBackfill(kind: "school-mappings" | "candidate-profiles" | "jd-profiles" | "reparse-failed"): Promise<{ task_id: string; task_type: string }> {
    return this.request<{ task_id: string; task_type: string }>(
      `/api/backfill/${encodeURIComponent(kind)}`,
      { method: "POST" }
    );
  }

  listResumeRevisions(candidateId: string): Promise<ResumeRevision[]> {
    return this.request<ResumeRevision[]>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/revisions`
    );
  }

  switchResumeRevision(revisionId: string): Promise<ResumeRevision> {
    return this.request<ResumeRevision>(
      `/api/resumes/revisions/${encodeURIComponent(revisionId)}/switch`,
      { method: "POST" }
    );
  }

  reparseResume(revisionId: string, forceOcr: boolean, useVision: boolean): Promise<{ revision_id: string; task_id: string }> {
    return this.request<{ revision_id: string; task_id: string }>(
      `/api/resumes/revisions/${encodeURIComponent(revisionId)}/reparse`,
      {
        body: JSON.stringify({ force_ocr: forceOcr, use_vision: useVision }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  async downloadResume(revisionId: string, filename: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/resumes/revisions/${encodeURIComponent(revisionId)}/download`,
      { headers }
    );
    if (!response.ok) throw new Error("下载失败");
    const blob = await response.blob();
    await this.triggerDownload(blob, filename);
  }

  async previewResume(revisionId: string): Promise<string> {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/resumes/revisions/${encodeURIComponent(revisionId)}/preview`,
      { headers }
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({})) as ApiError;
      throw new Error(error.message || error.detail || "预览失败");
    }
    const blob = await response.blob();
    return URL.createObjectURL(blob);
  }

  async viewResume(revisionId: string): Promise<{ kind: "opened" | "preview"; filename: string; url?: string }> {
    const target = await this.request<{ kind: "word" | "preview"; filename: string; path?: string }>(
      `/api/resumes/revisions/${encodeURIComponent(revisionId)}/view-target`,
    );
    if (target.kind === "word") {
      if (!target.path) throw new Error("Word 文件路径不可用，请重新打开。");
      try {
        await invoke("open_document", { path: target.path });
      } catch (error) {
        throw new Error(`无法使用系统默认应用打开 Word：${error instanceof Error ? error.message : String(error)}`);
      }
      return { kind: "opened", filename: target.filename };
    }
    return { kind: "preview", filename: target.filename, url: await this.previewResume(revisionId) };
  }

  searchCandidates(query: string, filters?: CandidateSearchFilters, options?: CandidateSearchOptions): Promise<CandidateSearchResult> {
    const { mode = "hybrid", operator = "smart", rewriteEnabled = false, searchBody = false, parseEnabled = false, limit, offset = 0 } = options ?? {};
    // 关键词/精确筛选：返回所有满足条件的；混合/向量：相关性排序后返回（默认 50）。
    // 默认值取 50 的依据（见方案 §4.5 实测）：limit=20/50/100 三档的头部指标
    // （R@20 / NDCG@10 / P@5）完全相同，且 limit=50 的 R@50 已与 limit=100 持平——
    // 多返回的那 50 条没有质量增量，却要多传一倍数据。
    const effectiveLimit = limit ?? ((mode === "keyword" || !query.trim()) ? 2000 : 50);
    // 关键词模式不支持 AI 语义改写：即使开关开着也不发送，避免后端 422。
    const effectiveRewrite = mode === "keyword" ? false : rewriteEnabled;
    // 向量/混合模式仅支持智能排序：强制使用 smart，避免后端 422。
    const effectiveOperator = mode === "keyword" ? operator : "smart";
    // 正文检索（检索经历/项目正文）在关键词与混合两种模式下含义一致：都只影响关键词通道，
    // 不影响向量与重排。向量模式没有 FTS 通道，开关无意义，仍不发送。
    const effectiveSearchBody = mode === "vector" ? false : searchBody;
    return this.request<CandidateSearchResult>("/api/search/candidates", {
      body: JSON.stringify({
        query, mode, operator: effectiveOperator, rewrite_enabled: effectiveRewrite,
        search_body: effectiveSearchBody, parse_enabled: parseEnabled, limit: effectiveLimit, offset,
        ...(filters && Object.keys(filters).length ? { filters } : {})
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  listCandidates(): Promise<CandidateListItem[]> {
    return this.request<CandidateListItem[]>("/api/resumes/candidates");
  }

  listDirectionPending(): Promise<CandidateListItem[]> {
    return this.request<CandidateListItem[]>("/api/resumes/candidates/direction-pending");
  }

  async listCandidatesPage(page: number, pageSize: number): Promise<CandidatePage> {
    try {
      return await this.request<CandidatePage>(
        `/api/resumes/candidates/page?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`
      );
    } catch (error) {
      if (!(error instanceof ApiRequestError) || error.status !== 404) throw error;
      // Transitional compatibility for a desktop frontend that reloads before
      // its already-running sidecar has restarted with the paging endpoint.
      const items = await this.listCandidates();
      const start = Math.max(0, (page - 1) * pageSize);
      return {
        items: items.slice(start, start + pageSize),
        total: items.length,
        page,
        page_size: pageSize,
        has_more: start + pageSize < items.length,
      };
    }
  }

  importJd(input: { company: string; title: string; sourceText: string }) {
    return this.request<{ jd_id: string; revision_id: string }>("/api/jd/import", {
      body: JSON.stringify({
        company: input.company,
        title: input.title,
        source_text: input.sourceText
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  importJdFile(file: File, company: string, title: string) {
    const form = new FormData();
    form.append("file", file);
    form.append("company", company);
    form.append("title", title);
    return this.request<{ jd_id: string; revision_id: string }>("/api/jd/import-file", {
      body: form,
      method: "POST"
    });
  }

  importJdBatch(sourceText: string) {
    return this.request<{ imported: { jd_id: string; revision_id: string }[] }>(
      "/api/jd/import-batch",
      {
        body: JSON.stringify({ source_text: sourceText }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  importJdBatchFile(file: File) {
    const form = new FormData();
    form.append("file", file);
    return this.request<{ imported: { jd_id: string; revision_id: string }[] }>(
      "/api/jd/import-batch-file",
      { body: form, method: "POST" }
    );
  }

  listJds(): Promise<JdListItem[]> {
    return this.request<JdListItem[]>("/api/jd");
  }

  listJdsPage(page: number, pageSize: number, filter?: { title?: string; company?: string; status?: string }): Promise<JdPage> {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (filter?.title) query.set("title", filter.title);
    if (filter?.company) query.set("company", filter.company);
    if (filter?.status) query.set("status", filter.status);
    return this.request<JdPage>(`/api/jd/page?${query}`);
  }

  updateJdStatus(jdId: string, status: string) {
    return this.request<{ jd_id: string; status: string }>(
      `/api/jd/${encodeURIComponent(jdId)}/status`,
      {
        body: JSON.stringify({ status }),
        headers: { "Content-Type": "application/json" },
        method: "PATCH"
      }
    );
  }

  // points/compact 由「重新生成」的双形态结果原样透传；手工编辑时为空，后端按句读确定性拆点。
  updateJdField(
    jdId: string,
    field: string,
    value: unknown,
    extra?: { points?: ProfilePointData[]; compact?: string | null }
  ) {
    return this.request<{ jd_id: string; revision_id: string; field: string; value: unknown }>(
      `/api/jd/${encodeURIComponent(jdId)}/field`,
      {
        body: JSON.stringify({ field, value, ...(extra ?? {}) }),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  updateJdParsed(jdId: string, parsedData: JdParsedData) {
    return this.request<{ jd_id: string; revision_id: string; field: string; value: unknown }>(
      `/api/jd/${encodeURIComponent(jdId)}/parsed`,
      {
        body: JSON.stringify({ parsed_data: parsedData }),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  regenerateJdProfile(
    jdId: string,
    instruction = "",
    onStage?: (progress: ProfileGenerationProgress) => void,
  ) {
    return this.streamProfileGeneration<{ generated: boolean; summary?: string; points?: ProfilePointData[]; compact?: string | null; input_hash?: string; constraints?: JdExactConstraint[] }>(
      `/api/jd/${encodeURIComponent(jdId)}/regen-profile`,
      { instruction },
      onStage,
    );
  }

  parseJdConstraints(sourceText: string) {
    // 年限不在 exact_constraints 里（它是 min_years 驱动的硬窗口），故单独回传：
    // min_years 为 null 且 years_stated 为 true 表示画像明确「经验不限」，要清空年限。
    return this.request<{ constraints: JdExactConstraint[]; min_years: number | null; years_stated: boolean }>(
      "/api/jd/parse-constraints",
      { body: JSON.stringify({ source_text: sourceText }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  matchJd(revisionId: string, limit = 50, mode: "keyword" | "vector" | "hybrid" = "hybrid") {
    return this.request<{
      run_id: string | null;
      items: CandidateSearchResult["items"];
      empty_reason?: string | null;
      // 实际下推到检索层的 JD 硬条件（含原文依据），用于在匹配结果页明示排除依据。
      hard_filters?: { kind: string; alternatives: string[]; source_text: string }[];
      // 安全阀触发时回退掉的硬条件；非空表示结果不再受这些条件约束。
      relaxed?: string[];
    }>(
      "/api/match/jd",
      {
        body: JSON.stringify({ revision_id: revisionId, limit, mode }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  startAiReview(runId: string, reasoning = false) {
    return this.request<{ review_id: string; status: string }>(
      `/api/match/run/${encodeURIComponent(runId)}/ai-review`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reasoning }) }
    );
  }

  getAiReview(runId: string) {
    return this.request<{ status: string; progress: number; result_ref: string | null; error_message: string | null }>(
      `/api/match/run/${encodeURIComponent(runId)}/ai-review`
    );
  }

  /** 搜索侧 AI 复核：按搜索条件复核勾选的候选人，产出亮点/风险点。 */
  startSearchReview(payload: {
    query: string;
    filters?: CandidateSearchFilters;
    candidate_ids: string[];
    reasoning?: boolean;
  }) {
    return this.request<{ review_id: string; status: string; query_key: string }>(
      "/api/search/review",
      {
        body: JSON.stringify({ reasoning: false, ...payload }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  getSearchReview(reviewId: string) {
    return this.request<SearchReviewStatus>(
      `/api/search/review/${encodeURIComponent(reviewId)}`
    );
  }

  matchBatch(revisionIds: string[], limit = 20, mode: "keyword" | "vector" | "hybrid" = "hybrid") {
    return this.request<{ results: { revision_id: string; run_id: string; items: CandidateSearchResult["items"] }[] }>(
      "/api/match/batch",
      {
        body: JSON.stringify({ revision_ids: revisionIds, limit, mode }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  markMatchResult(resultId: string, status: MatchMarkStatus) {
    return this.request<{ result_id: string; status: MatchMarkStatus }>(
      `/api/match/result/${encodeURIComponent(resultId)}/mark`,
      {
        body: JSON.stringify({ status }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  listMatchResults() {
    return this.request<{ groups: MatchResultGroup[] }>("/api/match/results");
  }

  listMatchResultsForCandidate(candidateId: string) {
    return this.request<MatchCandidateItem[]>(
      `/api/match/candidate/${encodeURIComponent(candidateId)}`
    );
  }

  matchCandidate(candidateId: string, mode: "keyword" | "vector" | "hybrid" = "hybrid") {
    return this.request<{ run_id: string | null; items: MatchCandidateItem[] }>(
      `/api/match/candidate/${encodeURIComponent(candidateId)}?mode=${encodeURIComponent(mode)}`,
      { method: "POST" }
    );
  }

  createCaseFromMatchResult(resultId: string) {
    return this.request<{ case_id: string; result_id: string; status: string }>(
      `/api/match/result/${encodeURIComponent(resultId)}/create-case`,
      { method: "POST" }
    );
  }

  async exportMatchJd(revisionId: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/match/jd/${encodeURIComponent(revisionId)}/export`,
      { headers }
    );
    if (!response.ok) throw new Error("导出失败");
    const blob = await response.blob();
    await this.triggerDownload(blob, `match_${revisionId}.xlsx`);
  }

  health() {
    return this.request<Record<string, { status: string; message?: string }>>(
      "/health/checks"
    );
  }

  async waitForReady(timeoutMs = 20000): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const response = await this.fetcher(`${this.baseUrl}/health/ready`, {
          cache: "no-store"
        });
        if (response.ok) return true;
      } catch {
        // sidecar 尚未监听端口，继续轮询
      }
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return false;
  }

  diagnostics() {
    return this.request<DiagnosticsData>("/api/diagnostics");
  }

  async exportDiagnostics() {
    await this.download("/api/diagnostics/export", "diagnostics.json");
  }

  listMappingProjects() {
    return this.request<MappingProject[]>("/api/mapping/projects");
  }

  createMappingProject(name: string, description?: string) {
    return this.request<MappingProject>("/api/mapping/projects", {
      body: JSON.stringify({ name, description }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  buildMappingTree(projectId: string, text: string, label = "") {
    return this.request<MappingSnapshot>(
      `/api/mapping/projects/${encodeURIComponent(projectId)}/build-from-text`,
      {
        body: JSON.stringify({ text, label }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  listMappingSnapshots(projectId: string) {
    return this.request<MappingSnapshot[]>(
      `/api/mapping/projects/${encodeURIComponent(projectId)}/snapshots`
    );
  }

  getMappingTree(snapshotId: string) {
    return this.request<MappingTreeNode[]>(
      `/api/mapping/snapshots/${encodeURIComponent(snapshotId)}/tree`
    );
  }

  listCompanies() {
    return this.request<OrgCompany[]>("/api/org/companies");
  }

  createCompany(name: string) {
    return this.request<OrgCompany>("/api/org/companies", {
      body: JSON.stringify({ name }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  updateCompany(companyId: string, name: string) {
    return this.request<OrgCompany>(
      `/api/org/companies/${encodeURIComponent(companyId)}`,
      {
        body: JSON.stringify({ name }),
        headers: { "Content-Type": "application/json" },
        method: "PATCH"
      }
    );
  }

  listDepartments(companyId: string) {
    return this.request<OrgDepartment[]>(
      `/api/org/companies/${encodeURIComponent(companyId)}/departments`
    );
  }

  createDepartment(input: CreateOrgDepartmentInput) {
    return this.request<OrgDepartment>("/api/org/departments", {
      body: JSON.stringify(input),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  listEmployees(companyId: string) {
    return this.request<OrgEmployee[]>(
      `/api/org/companies/${encodeURIComponent(companyId)}/employees`
    );
  }

  getOrgTree(companyId: string) {
    return this.request<OrgTreeNode>(
      `/api/org/companies/${encodeURIComponent(companyId)}/tree`
    );
  }

  createEmployee(input: CreateOrgEmployeeInput) {
    return this.request<OrgEmployee>("/api/org/employees", {
      body: JSON.stringify(input),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  async exportOrgInternal(companyId: string) {
    await this.download(
      `/api/org/companies/${encodeURIComponent(companyId)}/export`,
      `org_internal_${companyId}.xlsx`
    );
  }

  async exportOrgClient(companyId: string) {
    await this.download(
      `/api/org/companies/${encodeURIComponent(companyId)}/export-client`,
      `org_client_${companyId}.xlsx`
    );
  }

  async exportOrgArchPdf(companyId: string) {
    await this.download(
      `/api/org/companies/${encodeURIComponent(companyId)}/export-pdf`,
      `org_arch_${companyId}.pdf`
    );
  }

  updateDepartment(departmentId: string, changes: UpdateOrgDepartmentInput) {
    return this.request<OrgDepartment>(
      `/api/org/departments/${encodeURIComponent(departmentId)}`,
      {
        body: JSON.stringify(changes),
        headers: { "Content-Type": "application/json" },
        method: "PATCH"
      }
    );
  }

  async deleteDepartment(departmentId: string) {
    await this.request<{ deleted: boolean }>(
      `/api/org/departments/${encodeURIComponent(departmentId)}`,
      { method: "DELETE" }
    );
  }

  updateEmployee(employeeId: string, changes: UpdateOrgEmployeeInput) {
    return this.request<OrgEmployee>(
      `/api/org/employees/${encodeURIComponent(employeeId)}`,
      {
        body: JSON.stringify(changes),
        headers: { "Content-Type": "application/json" },
        method: "PATCH"
      }
    );
  }

  async deleteEmployee(employeeId: string) {
    await this.request<{ deleted: boolean }>(
      `/api/org/employees/${encodeURIComponent(employeeId)}`,
      { method: "DELETE" }
    );
  }

  async deleteCompany(companyId: string) {
    await this.request<{ deleted: boolean }>(
      `/api/org/companies/${encodeURIComponent(companyId)}`,
      { method: "DELETE" }
    );
  }

  parseOrgImport(text: string) {
    return this.request<OrgParseResult>("/api/org/import/parse", {
      body: JSON.stringify({ text }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  parseOrgWord(file: File) {
    const form = new FormData();
    form.append("file", file);
    return this.request<{ result: OrgParseResult; source_text: string }>("/api/org/import/word", {
      body: form,
      method: "POST"
    });
  }

  answerOrgImport(text: string, answers: string[]) {
    return this.request<OrgParseResult>("/api/org/import/answer", {
      body: JSON.stringify({ text, answers }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  commitOrgImport(companyId: string, draft: OrgImportDraft, sourceText?: string | null) {
    return this.request<{ departments: number; employees: number }>(
      "/api/org/import/commit",
      {
        body: JSON.stringify({ company_id: companyId, draft, source_text: sourceText ?? null }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  reviseOrgImport(draft: OrgImportDraft, instruction: string) {
    return this.request<OrgImportDraft>(
      "/api/org/import/revise",
      {
        body: JSON.stringify({ draft, instruction }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  getCompanySource(companyId: string) {
    return this.request<{ company_id: string; source_text: string | null }>(
      `/api/org/companies/${encodeURIComponent(companyId)}/source`
    );
  }

  bindEmployee(employeeId: string, phone: string, name?: string | null) {
    return this.request<BindEmployeeResult>(
      `/api/org/employees/${encodeURIComponent(employeeId)}/bind`,
      {
        body: JSON.stringify({ phone, name }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  searchBdLeads(query: string, limit = 10) {
    return this.request<BdLead[]>("/api/bd/search", {
      body: JSON.stringify({ query, limit }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  searchLeadsForCandidate(candidateId: string, limit = 10) {
    return this.request<BdLead[]>("/api/bd/search-for-candidate", {
      body: JSON.stringify({ candidate_id: candidateId, limit }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  updateLeadStatus(leadId: string, status: string, note?: string) {
    return this.request<BdLead>(`/api/bd/${encodeURIComponent(leadId)}/status`, {
      body: JSON.stringify({ status, note }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  runBdAgent(query: string, kind = "text", limit = 10) {
    return this.request<BdAgentQueryResult>("/api/bd/agent/query", {
      body: JSON.stringify({ query, kind, limit }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  async runBdAgentStream(
    query: string,
    kind = "text",
    limit = 10,
    onProgress?: (progress: BdProgress) => void,
    onLeads?: (leads: BdAgentLead[]) => void
  ) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    headers.set("Content-Type", "application/json");
    const response = await this.fetcher(`${this.baseUrl}/api/bd/agent/query-stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({ query, kind, limit })
    });
    if (!response.ok) throw new Error("检索失败");
    if (!response.body) throw new Error("浏览器不支持流式响应");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result: BdAgentQueryResult | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        const data = JSON.parse(raw) as
          | (BdProgress & { type: "progress" })
          | (BdAgentQueryResult & { type: "result" })
          | { type: "leads"; leads: BdAgentLead[] };
        if (data.type === "progress" && onProgress) {
          onProgress({ stage: data.stage, message: data.message });
        } else if (data.type === "leads" && onLeads) {
          onLeads(data.leads);
        } else if (data.type === "result") {
          result = data as BdAgentQueryResult;
        }
      }
    }

    if (!result) throw new Error("未收到检索结果");
    return result;
  }

  followUpBdAgent(sessionId: string, query: string, limit = 10) {
    return this.request<BdAgentQueryResult>(
      `/api/bd/agent/session/${encodeURIComponent(sessionId)}/follow-up`,
      {
        body: JSON.stringify({ query, limit }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  lookupPool(leadId: string) {
    return this.request<BdPoolCandidate[]>(
      `/api/bd/leads/${encodeURIComponent(leadId)}/lookup-pool`,
      { method: "POST" }
    );
  }

  indexStatus() {
    return this.request<IndexSyncStatus>("/api/search/index-status");
  }

  retryIndexSync() {
    return this.request<IndexSyncStatus>("/api/search/index-retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  }

  getResumeReview(revisionId: string) {
    return this.request<ResumeReview>(`/api/resumes/revisions/${encodeURIComponent(revisionId)}/review`);
  }

  createCase(candidateId: string, jdId: string) {
    return this.request<CaseItem>("/api/case", {
      body: JSON.stringify({ candidate_id: candidateId, jd_id: jdId }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  listCasesPage(page: number, pageSize: number, jdId?: string) {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (jdId) query.set("jd_id", jdId);
    return this.request<CasePage>(`/api/case?${query}`);
  }

  getCase(caseId: string) {
    return this.request<CaseDetail>(`/api/case/${encodeURIComponent(caseId)}`);
  }

  deleteCase(caseId: string) {
    return this.request<{ deleted: string }>(
      `/api/case/${encodeURIComponent(caseId)}`,
      { method: "DELETE" }
    );
  }

  recommendCase(caseId: string, payload: CaseActionInput = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/recommend`, {
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  enterInterview(caseId: string, payload: CaseActionInput & { round_name?: string; round_type?: string } = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/enter-interview`, {
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  recordResult(caseId: string, caseRoundId: string, result: string, payload: CaseActionInput = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/result`, {
      body: JSON.stringify({ ...payload, case_round_id: caseRoundId, result }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  passAndAdvance(caseId: string, caseRoundId: string, payload: CaseActionInput & { next_round_name?: string } = {}) {
    return this.request<CaseEventItem[]>(`/api/case/${encodeURIComponent(caseId)}/pass-and-advance`, {
      body: JSON.stringify({ ...payload, case_round_id: caseRoundId }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  offerCase(caseId: string, payload: CaseActionInput = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/offer`, {
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  onboardCase(caseId: string, payload: CaseActionInput = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/onboard`, {
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  exitCase(caseId: string, result?: string, payload: CaseActionInput = {}) {
    return this.request<CaseEventItem>(`/api/case/${encodeURIComponent(caseId)}/exit`, {
      body: JSON.stringify({ ...payload, result: result ?? null }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  voidEvent(eventId: string, payload: CaseActionInput = {}) {
    return this.request<{ deleted: string }>(`/api/case/event/${encodeURIComponent(eventId)}/void`, {
      body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  dashboardOverview(filters?: DashboardQuery) {
    return this.request<DashboardOverview>(`/api/dashboard/overview${_dashQuery(filters)}`);
  }

  dashboardByJd(filters?: DashboardQuery) {
    return this.request<DashboardByJd[]>(`/api/dashboard/by-jd${_dashQuery(filters)}`);
  }

  dashboardTrend(granularity: string, filters?: DashboardQuery) {
    const base = `/api/dashboard/trend?granularity=${encodeURIComponent(granularity)}`;
    const extra = _dashQuery(filters);
    return this.request<DashboardTrendItem[]>(
      extra ? `${base}&${extra.slice(1)}` : base
    );
  }

  async dashboardExport(filters?: DashboardQuery) {
    await this.download(`/api/dashboard/export${_dashQuery(filters)}`, "dashboard.xlsx");
  }

  dailyFollowupToday() {
    return this.request<DailyFollowupToday>("/api/daily-followup/today");
  }

  checkDailyTodo(input: { item_key: string; done: boolean }) {
    return this.request<{ date: string; item_key: string; done: boolean }>("/api/daily-followup/check", {
      method: "POST",
      // 必须显式声明 JSON：否则浏览器按 text/plain 发送，FastAPI 解析不出 body 会回 422。
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  }

  createCandidateReminder(input: { candidate_id: string; content: string }) {
    return this.request<CandidateReminderItem>("/api/candidate-reminders", {
      method: "POST",
      // 必须显式声明 JSON：否则浏览器按 text/plain 发送，FastAPI 解析不出 body 会回 422。
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  }

  completeCandidateReminder(reminderId: string) {
    return this.request<CandidateReminderItem>(`/api/candidate-reminders/${reminderId}/done`, {
      method: "POST",
    });
  }

  reverseMatch(candidateId: string, mode: "keyword" | "vector" | "hybrid" = "hybrid") {
    return this.request<ReverseMatchItem[]>(
      `/api/match/reverse/${encodeURIComponent(candidateId)}?mode=${encodeURIComponent(mode)}`
    );
  }

  getCandidateContact(candidateId: string) {
    return this.request<CandidateContact>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/contact`
    );
  }

  updateCandidateContact(candidateId: string, input: { email: string | null; phone: string | null }) {
    return this.request<CandidateContact>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/contact`,
      {
        body: JSON.stringify(input),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  // points/compact 由「重新生成」的双形态结果原样透传；手工编辑时为空，后端按句读确定性拆点。
  /**
   * 保存候选人的沟通记录（单条自由文本，整条覆盖）。
   *
   * 与 `updateCandidateField` 分开是刻意的：后者写 parsed_data，会触发画像过期判定
   * 与索引重建；沟通记录是候选人级备注，既不进画像也不进索引。
   */
  updateCommunicationNote(candidateId: string, note: string) {
    return this.request<{ candidate_id: string; communication_note: string | null }>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/communication-note`,
      {
        body: JSON.stringify({ note }),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  updateCandidateField(
    candidateId: string,
    field: string,
    value: unknown,
    extra?: { points?: ProfilePointData[]; compact?: string | null }
  ) {
    return this.request<{ candidate_id: string; revision_id: string; field: string; value: unknown }>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/field`,
      {
        body: JSON.stringify({ field, value, ...(extra ?? {}) }),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  updateCandidateParsed(candidateId: string, parsedData: ParsedResumeData) {
    return this.request<{ candidate_id: string; revision_id: string; updated_fields: string[] }>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/parsed`,
      {
        body: JSON.stringify({ parsed_data: parsedData }),
        headers: { "Content-Type": "application/json" },
        method: "PUT"
      }
    );
  }

  regenerateCandidateProfile(
    candidateId: string,
    instruction = "",
    onStage?: (progress: ProfileGenerationProgress) => void,
  ) {
    return this.streamProfileGeneration<{ generated: boolean; summary?: string; points?: ProfilePointData[]; compact?: string | null; input_hash?: string }>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}/regen-profile`,
      { instruction },
      onStage,
    );
  }

  deleteCandidate(candidateId: string) {
    return this.request<{ candidate_id: string; deleted: boolean }>(
      `/api/resumes/candidate/${encodeURIComponent(candidateId)}`,
      { method: "DELETE" }
    );
  }

  bulkDeleteCandidates(candidateIds: string[]) {
    return this.request<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>(
      "/api/resumes/bulk/delete",
      { body: JSON.stringify({ ids: candidateIds }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  bulkForceOcr(revisionIds: string[]) {
    return this.request<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>(
      "/api/resumes/bulk/force-ocr",
      { body: JSON.stringify({ ids: revisionIds }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  bulkReparse(revisionIds: string[]) {
    return this.request<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>(
      "/api/resumes/bulk/reparse",
      { body: JSON.stringify({ ids: revisionIds }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  async bulkDownloadCandidates(candidateIds: string[]) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    headers.set("Content-Type", "application/json");
    const response = await this.fetcher(`${this.baseUrl}/api/resumes/bulk/download`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ids: candidateIds }),
    });
    if (!response.ok) throw new Error("批量下载失败");
    const blob = await response.blob();
    await this.triggerDownload(blob, "resumes_batch.zip");
  }

  bulkMatchCandidates(candidateIds: string[], mode: "keyword" | "vector" | "hybrid" = "hybrid") {
    return this.request<BulkCandidateMatchResult>(
      "/api/match/candidates/bulk",
      { body: JSON.stringify({ candidate_ids: candidateIds, mode }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  deleteJd(jdId: string) {
    return this.request<{ jd_id: string; deleted: boolean }>(
      `/api/jd/${encodeURIComponent(jdId)}`,
      { method: "DELETE" }
    );
  }

  bulkDeleteJds(jdIds: string[]) {
    return this.request<{ results: { entity_id: string; ok: boolean; error: string | null; extra: Record<string, unknown> }[]; succeeded: number; failed: number }>(
      "/api/jd/bulk/delete",
      { body: JSON.stringify({ ids: jdIds }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  bulkDeleteCases(caseIds: string[]) {
    return this.request<{ results: { entity_id: string; ok: boolean; error: string | null }[]; succeeded: number; failed: number }>(
      "/api/case/bulk/delete",
      { body: JSON.stringify({ ids: caseIds }), headers: { "Content-Type": "application/json" }, method: "POST" }
    );
  }

  applyCorrection(input: { entityType: string; entityId: string; fieldName: string; newValue: string | null; reason?: string }) {
    return this.request<CorrectionRecord>("/api/correction/apply", {
      body: JSON.stringify({
        entity_type: input.entityType,
        entity_id: input.entityId,
        field_name: input.fieldName,
        new_value: input.newValue,
        reason: input.reason
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  undoCorrection(correctionId: string) {
    return this.request<CorrectionRecord>(
      `/api/correction/${encodeURIComponent(correctionId)}/undo`,
      { method: "POST" }
    );
  }

  async exportMappingTree(snapshotId: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/mapping/snapshots/${encodeURIComponent(snapshotId)}/export`,
      { headers }
    );
    if (!response.ok) {
      throw new Error("导出失败");
    }
    const blob = await response.blob();
    await this.triggerDownload(blob, `mapping_${snapshotId}.xlsx`);
  }

  async exportMappingTreePdf(snapshotId: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/mapping/snapshots/${encodeURIComponent(snapshotId)}/export-pdf`,
      { headers }
    );
    if (!response.ok) {
      throw new Error("导出失败");
    }
    const blob = await response.blob();
    await this.triggerDownload(blob, `mapping_${snapshotId}.pdf`);
  }

  getSettings() {
    return this.request<AppSettings>("/api/settings");
  }

  getVendors() {
    return this.request<VendorPreset[]>("/api/settings/vendors");
  }

  getAiCatalog() {
    return this.request<AiCatalog>("/api/ai/catalog");
  }

  refreshAiCatalog() {
    return this.request<{ status: string; active_version: number; message: string }>(
      "/api/ai/catalog/refresh",
      { method: "POST" }
    );
  }

  getAiConfig() {
    return this.request<AiConfig>("/api/ai/config");
  }

  updateAiConfig(update: AiConfigUpdate) {
    return this.request<AiConfig>("/api/ai/config", {
      body: JSON.stringify(update),
      headers: { "Content-Type": "application/json" },
      method: "PUT"
    });
  }

  probeAiConnection(input: { provider_id: string; api_key?: string; connection_id?: string | null; base_url_override?: string | null; parameter_style?: string | null; models?: Record<string, string> }) {
    return this.request<ConnectionProbeReport>("/api/ai/probe", {
      body: JSON.stringify(input),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  getAiStatus() {
    return this.request<AiStatus>("/api/ai/status");
  }

  updateSettings(values: Partial<AppSettings>) {
    return this.request<AppSettings>("/api/settings", {
      body: JSON.stringify(values),
      headers: { "Content-Type": "application/json" },
      method: "PUT"
    });
  }

  testMail() {
    return this.request<{ imap: { ok: boolean; message: string }; smtp: { ok: boolean; message: string } }>(
      "/api/settings/mail/test",
      { method: "POST" }
    );
  }

  sendMailConfirmation() {
    return this.request<{ sent: boolean; to: string; message: string }>(
      "/api/settings/mail/send-confirmation",
      { method: "POST" }
    );
  }

  sendFollowupTest() {
    return this.request<{ sent: boolean; to: string; message: string }>(
      "/api/settings/mail/send-followup-test",
      { method: "POST" }
    );
  }

  syncMail() {
    return this.request<{ ingested: number; revision_ids: string[] }>(
      "/api/settings/mail/sync",
      { method: "POST" }
    );
  }

  mailStatus() {
    return this.request<{ configured: boolean; last_uid: number }>("/api/settings/mail/status");
  }

  async exportMatchRun(runId: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(
      `${this.baseUrl}/api/match/run/${encodeURIComponent(runId)}/export`,
      { headers }
    );
    if (!response.ok) {
      throw new Error("导出失败");
    }
    const blob = await response.blob();
    await this.triggerDownload(blob, `match_${runId}.xlsx`);
  }

  listBackups() {
    return this.request<BackupSnapshot[]>("/api/backup/snapshots");
  }

  createBackup(label = "") {
    return this.request<{ filename: string; path: string }>("/api/backup/snapshots", {
      body: JSON.stringify({ label }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  restoreBackup(filename: string) {
    return this.request<{ restored_from: string; safety_backup: string; restart_required?: boolean; status?: string }>(
      `/api/backup/restore/${encodeURIComponent(filename)}`,
      { method: "POST" }
    );
  }

  createPortableBackup(targetPath: string, passphrase: string) {
    return this.request<{ path: string; same_volume: boolean }>("/api/backup/portable", {
      body: JSON.stringify({ target_path: targetPath, passphrase }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  restorePortableBackup(backupPath: string, targetRoot: string, passphrase: string) {
    return this.request<{ target_root: string; files_restored: number; files_verified: number; ok: boolean }>(
      "/api/backup/portable/restore",
      {
        body: JSON.stringify({ backup_path: backupPath, target_root: targetRoot, passphrase }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      }
    );
  }

  listReminders() {
    return this.request<ReminderItem[]>("/api/reminders");
  }

  createReminder(input: CreateReminderInput) {
    return this.request<ReminderItem>("/api/reminders", {
      body: JSON.stringify(input),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  dismissReminder(id: string) {
    return this.request<ReminderItem>(
      `/api/reminders/${encodeURIComponent(id)}/dismiss`,
      { method: "POST" }
    );
  }

  migrateData(targetRoot: string) {
    return this.request<MigrationReport>("/api/migration", {
      body: JSON.stringify({ target_root: targetRoot }),
      headers: { "Content-Type": "application/json" },
      method: "POST"
    });
  }

  async setDataRoot(path: string) {
    return invoke<string>("set_data_root", { path });
  }

  private async download(path: string, filename: string) {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    const response = await this.fetcher(`${this.baseUrl}${path}`, { headers });
    if (!response.ok) throw new Error("导出失败");
    const blob = await response.blob();
    await this.triggerDownload(blob, filename);
  }

  private async triggerDownload(blob: Blob, filename: string) {
    try {
      const buffer = await blob.arrayBuffer();
      const content = Array.from(new Uint8Array(buffer));
      await invoke("save_file", { filename, content });
    } catch {
      // 浏览器/无 Tauri 环境：回退到 <a download> 原生下载。
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  /**
   * 画像生成：走 SSE 拿阶段性反馈（`loading` / `draft` / `repair`），最后收结果或错误。
   *
   * 为什么不用普通 POST：生成最坏要等 150 秒，界面上只有一个转圈时，使用者分不清
   * 「快好了」和「刚开始第二次模型调用」——而这两者预期等待差一倍。服务端按
   * `Accept: text/event-stream` 在**同一个路径**上切成流式（见后端 `api/profile_stream.py`，
   * 那里写了为什么不新开一条 `-stream` 路由）。
   *
   * 错误在流里以 `error` 事件回传（响应头一旦发出就改不了状态码），这里还原成
   * `ApiRequestError`，让调用方的错误处理与普通请求完全一致。
   */
  private async streamProfileGeneration<T>(
    path: string,
    body: Record<string, unknown>,
    onStage?: (progress: ProfileGenerationProgress) => void,
  ): Promise<T> {
    const headers = new Headers();
    headers.set("X-Kerui-Session", this.sessionToken);
    headers.set("Content-Type", "application/json");
    headers.set("Accept", "text/event-stream");
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      // 会话失效、路由不存在这类「流还没开始」的失败仍是普通 HTTP 错误。
      const payload = (await response.json().catch(() => ({}))) as ApiError;
      throw new ApiRequestError(
        payload.message ?? payload.detail ?? "本机服务请求失败，请稍后重试",
        response.status,
      );
    }
    if (!response.body) throw new ApiRequestError("当前环境不支持流式响应", response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result: T | null = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (!raw) continue;
        const data = JSON.parse(raw) as { type?: string; [key: string]: unknown };
        if (data.type === "progress") {
          onStage?.({
            stage: String(data.stage ?? ""),
            message: String(data.message ?? ""),
          });
        } else if (data.type === "error") {
          throw new ApiRequestError(
            String(data.message ?? "画像生成失败"),
            Number(data.status ?? 500),
          );
        } else {
          result = data as unknown as T;
        }
      }
    }
    if (result === null) throw new ApiRequestError("未收到生成结果，请稍后重试", response.status);
    return result;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("X-Kerui-Session", this.sessionToken);
    // 禁用浏览器缓存，确保目录/配置等 GET 数据总是拿到最新值。
    const response = await this.fetcher(`${this.baseUrl}${path}`, { ...init, headers, cache: "no-store" });
    const payload = await response.json() as T | ApiError;
    if (!response.ok) {
      const apiError = payload as ApiError;
      throw new ApiRequestError(
        apiError.message ?? apiError.detail ?? "本机服务请求失败，请稍后重试",
        response.status,
      );
    }
    return payload as T;
  }
}


export async function createRuntimeApi(): Promise<RecruitmentApi> {
  let config: RuntimeConfig;
  try {
    config = await invoke<RuntimeConfig>("runtime_config");
  } catch {
    // Browser/Playwright fallback: reach a locally-run sidecar via env vars.
    config = {
      apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:43127",
      sessionToken: import.meta.env.VITE_SESSION_TOKEN ?? "0".repeat(64)
    };
  }
  const client = new ApiClient(config.apiBaseUrl, config.sessionToken);
  // 桌面版 sidecar 后台就绪：等待其 ready 后再返回，避免首次请求连接失败。
  await client.waitForReady();
  return client;
}
