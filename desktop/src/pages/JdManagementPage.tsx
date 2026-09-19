import { Fragment, useEffect, useMemo, useState, type FormEvent } from "react";
import type { AiReviewState, CandidateSearchItem, ImportedJd, JdListItem, JdStatus } from "../App";
import { EditableCell } from "../components/EditableCell";
import { CANDIDATE_COLUMNS, COLUMN_LABELS, SEARCH_REVIEW_COLUMN_KEYS, candidateColumnText, type CandidateColumnKey } from "../components/CandidateTable";
import { Button, HoverText, OverflowMenu, StatusBadge, stripEvidenceRefs } from "../components/ui";

export const JD_STATUS_OPTIONS: { value: JdStatus; label: string }[] = [
  { value: "OPEN", label: "开放" },
  { value: "FILLED", label: "招满" },
  { value: "CANCELLED", label: "已取消" },
];

export interface JdFilter {
  title: string;
  company: string;
  status: string;
}

export interface JdManagementPageProps {
  jdSource: string;
  onJdSourceChange: (value: string) => void;
  jdResult: ImportedJd[];
  jds: JdListItem[];
  jdPage: number;
  jdTotal: number;
  jdTotalPages: number;
  openJdSource: Record<string, boolean>;
  jdFilter: JdFilter;
  onJdFilterChange: (filter: JdFilter) => void;
  jdImportOpen: boolean;
  jdMatchMode: "keyword" | "vector" | "hybrid";
  onJdMatchModeChange: (mode: "keyword" | "vector" | "hybrid") => void;
  onSubmitJd: (event: FormEvent) => void;
  onUploadJd: (file: File | undefined) => void;
  onLoadJds: (page?: number) => void;
  onUpdateJdStatus: (jdId: string, status: string) => void;
  onUpdateJdField: (jdId: string, field: string, value: unknown) => Promise<void>;
  /** 保存画像：直接落库（画像文本变化时后端按 AI 重解析硬条件并覆盖）。 */
  onSaveJdProfile: (jdId: string, profile: string) => Promise<void>;
  onRegenerateJdProfile: (jdId: string, instruction: string) => Promise<string | null>;
  onMatchJd: (jd: JdListItem) => void;
  onDeleteJd: (jd: JdListItem) => void;
  onOpenParsed: (jd: JdListItem) => void;
  onReparseFailed: () => void;
  onToggleJdSource: (revisionId: string) => void;
  jdMatch: { title: string; company: string; location: string; run_id: string | null; items: CandidateSearchItem[] } | null;
  jdReview: AiReviewState | null;
  jdMatchingId: string | null;
  onJdAiReview: () => void;
  onJdAiReviewCancel: () => void;
  onJdAiReviewRetry: () => void;
  jdReviewReasoning: boolean;
  onJdReviewReasoningChange: (v: boolean) => void;
  visibleColumns: Record<CandidateColumnKey, boolean>;
  columnOrder: CandidateColumnKey[];
  onToggleColumn: (key: CandidateColumnKey) => void;
  matchCaseIds: Record<string, string>;
  creatingCase: boolean;
  onPreviewResume: (revisionId: string, name?: string, filename?: string) => void;
  onCreateCaseFromMatch: (resultId: string) => void;
  onEditProfile: (candidateId: string, summary: string | null, source: string | null, stale: boolean) => void;
  onBulkDeleteJds: (jdIds: string[]) => void;
}

function resumeProfilePoints(p: { ai_profile_points?: { text: string }[]; ai_profile_summary?: string | null } | null | undefined): string[] {
  const points = (p?.ai_profile_points ?? []).map((x) => x.text).filter(Boolean);
  if (points.length) return points;
  return (p?.ai_profile_summary ?? "").split(/\n+/).map((s) => s.trim()).filter(Boolean);
}

function jdProfilePoints(p: { candidate_profile_points?: { text: string }[]; candidate_profile?: string | null } | null | undefined): string[] {
  const points = (p?.candidate_profile_points ?? []).map((x) => x.text).filter(Boolean);
  if (points.length) return points;
  return (p?.candidate_profile ?? "").split(/\n+/).map((s) => s.trim()).filter(Boolean);
}

export type ReviewVerdictItem = {
  match_result_id?: string;
  candidate_id?: string;
  jd_revision_id?: string;
  verdict?: string;
  reasons?: string[];
  cautions?: string[];
  /** 该条复核失败（调用出错/超时）：仍按待核展示，但要让用户看出「没复核成功」。 */
  failed?: boolean;
  error?: string;
};

export function reviewVerdictLabel(verdict: string | undefined): string {
  if (verdict === "recommend") return "推荐";
  if (verdict === "reject") return "不推荐";
  return "待核";
}

// 复核结果排序优先级：推荐 < 待核 < 不推荐（值越小越靠前）。
function reviewVerdictRank(verdict: string | undefined): number {
  if (verdict === "recommend") return 0;
  if (verdict === "reject") return 2;
  return 1;
}

// 人找岗位复核结果排序：复核完成按「推荐 → 待核 → 不推荐」，同类按基础分降序、
// 再按稳定 ID 排序；复核未完成/失败时沿用基础分排序。
export interface ReviewSortableItem {
  result_id: string;
  score: number;
  jd_id?: string;
  candidate_id?: string;
}

export function sortReviewItems<T extends ReviewSortableItem>(
  items: T[],
  verdicts: Record<string, ReviewVerdictItem>,
  reviewDone: boolean,
): T[] {
  return [...items].sort((a, b) => {
    if (reviewDone) {
      const rankA = reviewVerdictRank(verdicts[a.result_id]?.verdict);
      const rankB = reviewVerdictRank(verdicts[b.result_id]?.verdict);
      if (rankA !== rankB) return rankA - rankB;
    }
    if (b.score !== a.score) return b.score - a.score;
    const keyA = a.jd_id ?? a.candidate_id ?? a.result_id;
    const keyB = b.jd_id ?? b.candidate_id ?? b.result_id;
    return keyA < keyB ? -1 : keyA > keyB ? 1 : 0;
  });
}

export function reviewVerdictsByResultId(resultRef: string | null): Record<string, ReviewVerdictItem> {
  if (!resultRef) return {};
  try {
    const list = JSON.parse(resultRef) as ReviewVerdictItem[];
    const map: Record<string, ReviewVerdictItem> = {};
    for (const item of list) {
      if (!item.match_result_id) continue;
      // 符合点/注意点在此统一清洗掉证据编号（如「（projects[0]、experiences[1]）」，含全角括号），
      // 保证「人匹配岗位」与「岗位匹配人」两处展示一致。
      map[item.match_result_id] = {
        ...item,
        reasons: (item.reasons ?? []).map(stripEvidenceRefs).filter(Boolean),
        cautions: (item.cautions ?? []).map(stripEvidenceRefs).filter(Boolean),
      };
    }
    return map;
  } catch {
    return {};
  }
}


/** 复核结论计数；失败条数单独统计，避免「全是待核、看不出有没有跑成功」。 */
export function reviewVerdictCounts(verdicts: Record<string, ReviewVerdictItem>): {
  recommend: number;
  pending: number;
  reject: number;
  failed: number;
} {
  const counts = { recommend: 0, pending: 0, reject: 0, failed: 0 };
  for (const item of Object.values(verdicts)) {
    if (item.verdict === "recommend") counts.recommend += 1;
    else if (item.verdict === "reject") counts.reject += 1;
    else counts.pending += 1;
    if (item.failed) counts.failed += 1;
  }
  return counts;
}


export function JdManagementPage(props: JdManagementPageProps) {
  const {
    jdSource, onJdSourceChange, jdResult, jds, jdPage, jdTotal, jdTotalPages,
    openJdSource, jdFilter, onJdFilterChange,
    jdImportOpen, jdMatchMode, onJdMatchModeChange, onSubmitJd, onUploadJd, onLoadJds,
    onUpdateJdStatus, onUpdateJdField, onSaveJdProfile, onRegenerateJdProfile, onMatchJd, onDeleteJd, onOpenParsed, onReparseFailed, onToggleJdSource,
    jdMatch, jdReview, jdMatchingId, onJdAiReview, onJdAiReviewCancel, onJdAiReviewRetry, jdReviewReasoning, onJdReviewReasoningChange, visibleColumns, columnOrder, onToggleColumn, matchCaseIds, creatingCase,
    onPreviewResume, onCreateCaseFromMatch, onEditProfile,
    onBulkDeleteJds,
  } = props;

  const [expandedJdId, setExpandedJdId] = useState<string | null>(null);
  const [profileDrafts, setProfileDrafts] = useState<Record<string, string>>({});
  const [regenInstructions, setRegenInstructions] = useState<Record<string, string>>({});
  const [columnsMenuOpen, setColumnsMenuOpen] = useState(false);
  const [jdMatchPage, setJdMatchPage] = useState(1);
  const [regeneratingJdIds, setRegeneratingJdIds] = useState<Record<string, boolean>>({});
  const [savingJdIds, setSavingJdIds] = useState<Record<string, boolean>>({});
  const [selectedJdIds, setSelectedJdIds] = useState<Set<string>>(new Set());

  const jdReviewVerdicts = reviewVerdictsByResultId(jdReview?.result_ref ?? null);

  // 「亮点/风险点」只由搜索侧复核产出：岗位侧已有「符合点/注意点」两列，不重复展示。
  const orderedColumns = columnOrder.filter(
    (key) => visibleColumns[key] && !SEARCH_REVIEW_COLUMN_KEYS.includes(key),
  );

  // 基础匹配分层（推荐在前）+ AI 复核结论（推荐 > 待核 > 不推荐）二级排序。
  const reviewCompleted = jdReview?.status === "SUCCESS" && jdReview?.result_ref != null;
  const sortedMatchItems = useMemo(() => {
    if (!jdMatch) return [];
    return [...jdMatch.items].sort((a, b) => {
      const tierA = a.match_tier === "recommend" ? 0 : 1;
      const tierB = b.match_tier === "recommend" ? 0 : 1;
      if (tierA !== tierB) return tierA - tierB;
      if (reviewCompleted) {
        const verdictA = a.result_id ? jdReviewVerdicts[a.result_id]?.verdict : undefined;
        const verdictB = b.result_id ? jdReviewVerdicts[b.result_id]?.verdict : undefined;
        const rankA = reviewVerdictRank(verdictA);
        const rankB = reviewVerdictRank(verdictB);
        if (rankA !== rankB) return rankA - rankB;
      }
      return b.score - a.score;
    });
  }, [jdMatch, reviewCompleted, jdReviewVerdicts]);

  useEffect(() => {
    setJdMatchPage(1);
  }, [jdMatch]);

  function toggleExpand(jdId: string) {
    setExpandedJdId((current) => (current === jdId ? null : jdId));
  }

  async function saveProfile(jd: JdListItem) {
    const value = profileDrafts[jd.jd_id] ?? jd.parsed_data?.candidate_profile ?? "";
    setSavingJdIds((current) => ({ ...current, [jd.jd_id]: true }));
    try {
      // 直接落库：画像文本有变化时由 App 层按 AI 重解析硬条件并覆盖，不再弹窗二次确认。
      await onSaveJdProfile(jd.jd_id, value);
    } catch {
      // 错误已由 App 层通过 setError 展示。
    } finally {
      setSavingJdIds((current) => ({ ...current, [jd.jd_id]: false }));
    }
  }

  async function regenerateProfile(jd: JdListItem) {
    const instruction = regenInstructions[jd.jd_id] ?? "";
    setRegeneratingJdIds((current) => ({ ...current, [jd.jd_id]: true }));
    try {
      const newProfile = await onRegenerateJdProfile(jd.jd_id, instruction);
      // 后端只生成不落库，把新画像写入本地草稿，供「保存画像」提交。
      if (newProfile != null) {
        setProfileDrafts((current) => ({ ...current, [jd.jd_id]: newProfile }));
      }
    } catch {
      // 错误已由 App 层通过 setError 展示。
    } finally {
      setRegeneratingJdIds((current) => ({ ...current, [jd.jd_id]: false }));
    }
    setRegenInstructions((current) => ({ ...current, [jd.jd_id]: "" }));
  }

  return (
    <section className="jd-panel">
      {jdImportOpen && (
        <div className="jd-import-panel">
          <form className="jd-form" onSubmit={(event) => void onSubmitJd(event)}>
            <textarea value={jdSource} onChange={(e) => onJdSourceChange(e.target.value)} placeholder="粘贴 JD 原文…" aria-label="JD 原文" />
            <div className="jd-form__actions">
              <Button type="submit" variant="primary">导入并解析</Button>
              <label className="btn btn-secondary" style={{ cursor: "pointer" }}>
                导入 Word / Excel
                <input
                  aria-label="选择 JD 文件"
                  accept=".doc,.docx,.xls,.xlsx"
                  type="file"
                  style={{ display: "none" }}
                  onChange={(event) => void onUploadJd(event.target.files?.[0])}
                />
              </label>
            </div>
          </form>
        </div>
      )}
      {jdResult.length > 0 && (
        <div className="jd-result" role="status">
          <strong>导入成功 {jdResult.length} 个岗位</strong>
          {jdResult.map((item) => (
            <code key={item.revision_id}>{item.revision_id}</code>
          ))}
        </div>
      )}

      <div className="card">
        <div className="card__head">
          <div className="card__title">岗位列表 <span className="card__count">{jdTotal} 个</span></div>
          <div className="jd-filters">
            <input
              aria-label="筛选岗位名称"
              placeholder="筛选岗位"
              value={jdFilter.title}
              onChange={(event) => onJdFilterChange({ ...jdFilter, title: event.target.value })}
            />
            <input
              aria-label="筛选公司名称"
              placeholder="筛选公司"
              value={jdFilter.company}
              onChange={(event) => onJdFilterChange({ ...jdFilter, company: event.target.value })}
            />
            <select
              aria-label="筛选岗位状态"
              value={jdFilter.status}
              onChange={(event) => onJdFilterChange({ ...jdFilter, status: event.target.value })}
            >
              <option value="">全部状态</option>
              {JD_STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
            <Button variant="ghost" size="sm" onClick={() => onLoadJds(jdPage)}>刷新列表</Button>
            <Button variant="ghost" size="sm" onClick={onReparseFailed}>一键重新生成不合格项</Button>
          </div>
        </div>

        {selectedJdIds.size > 0 && (
          <div className="bulk-toolbar" role="toolbar" aria-label="批量删除岗位">
            <span className="bulk-toolbar__count">已选 {selectedJdIds.size} 个岗位</span>
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                if (window.confirm(`确定永久删除选中的 ${selectedJdIds.size} 个岗位吗？关联的招聘流程会级联删除，此操作不可撤销。`)) {
                  onBulkDeleteJds(Array.from(selectedJdIds));
                  setSelectedJdIds(new Set());
                }
              }}
            >
              删除
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelectedJdIds(new Set())}>取消选择</Button>
          </div>
        )}

        {jds.length === 0 ? (
          jdTotal === 0 ? (
            <p className="muted" style={{ padding: 16 }}>暂无 JD，先在上方导入。</p>
          ) : (
            <p className="muted" style={{ padding: 16 }}>没有符合筛选条件的 JD。</p>
          )
        ) : (
          <div className="table-scroll">
            <table>
              <thead><tr><th style={{ width: 36 }}><input type="checkbox" aria-label="全选岗位" checked={jds.length > 0 && jds.every((jd) => selectedJdIds.has(jd.jd_id))} onChange={(e) => setSelectedJdIds(e.target.checked ? new Set(jds.map((jd) => jd.jd_id)) : new Set())} /></th><th>岗位</th><th>公司</th><th>状态</th><th>候选人画像要求</th><th>操作</th></tr></thead>
              <tbody>
                {jds.map((jd) => {
                  const expanded = expandedJdId === jd.jd_id;
                  const profileDraft = profileDrafts[jd.jd_id] ?? jd.parsed_data?.candidate_profile ?? "";
                  const regenInstruction = regenInstructions[jd.jd_id] ?? "";
                  const jdProfile = jdProfilePoints(jd.parsed_data);
                  const jdProfileFullText = jdProfile.length > 0 ? jdProfile.join("\n") : (jd.parsed_data?.candidate_profile ?? "");
                  return (
                    <Fragment key={jd.revision_id}>
                      <tr>
                        <td><input type="checkbox" aria-label={`选择 ${jd.title}`} checked={selectedJdIds.has(jd.jd_id)} onChange={() => setSelectedJdIds((current) => { const next = new Set(current); if (next.has(jd.jd_id)) next.delete(jd.jd_id); else next.add(jd.jd_id); return next; })} /></td>
                        <td><strong><EditableCell value={jd.title || ""} onSave={(next) => onUpdateJdField(jd.jd_id, "title", next.trim())} /></strong></td>
                        <td><EditableCell value={jd.company || ""} onSave={(next) => onUpdateJdField(jd.jd_id, "company", next.trim())} /></td>
                        <td>
                          <select
                            aria-label="岗位状态"
                            className={`jd-status-select ${jd.jd_status.toLowerCase()}`}
                            value={jd.jd_status}
                            onChange={(event) => onUpdateJdStatus(jd.jd_id, event.target.value)}
                          >
                            {JD_STATUS_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                        </td>
                        <td>
                          <button className="cell-edit" onClick={() => toggleExpand(jd.jd_id)}>
                            <HoverText text={jdProfileFullText} />
                          </button>{" "}
                          <StatusBadge tone="primary">AI 生成</StatusBadge>
                        </td>
                        <td>
                          <div className="case-actions">
                            <Button variant="secondary" size="sm" aria-label="匹配" loading={jdMatchingId === jd.jd_id} onClick={() => onMatchJd(jd)}>匹配候选人</Button>
                            <OverflowMenu
                              items={[
                                { key: "parsed", label: "解析表", onSelect: () => onOpenParsed(jd) },
                                { key: "delete", label: "删除", danger: true, onSelect: () => onDeleteJd(jd) },
                              ]}
                            />
                          </div>
                        </td>
                      </tr>
                      {expanded && (
                        <tr>
                          <td colSpan={6} className="jd-inline-edit-cell">
                            <div className="jd-inline-edit">
                              <div className="jd-inline-edit__col">
                                <div className="section-head"><h3>JD 原文</h3></div>
                                <pre className="jd-source-text">{jd.source_text || "—"}</pre>
                              </div>
                              <div className="jd-inline-edit__col">
                                <div className="section-head">
                                  <h3>候选人画像要求</h3>
                                  <StatusBadge tone="primary">AI 生成</StatusBadge>
                                </div>
                                <textarea
                                  aria-label="候选人画像要求"
                                  value={profileDraft}
                                  onChange={(e) => setProfileDrafts((current) => ({ ...current, [jd.jd_id]: e.target.value }))}
                                  placeholder="填写候选人画像要求…"
                                />
                                <div className="case-actions" style={{ marginTop: 8 }}>
                                  <Button variant="primary" size="sm" disabled={!!savingJdIds[jd.jd_id]} onClick={() => void saveProfile(jd)}>
                                    {savingJdIds[jd.jd_id] ? "保存中…" : "保存画像"}
                                  </Button>
                                </div>
                                <div className="sub-label">AI 重新生成（可选：输入新的描述或要求，再结合 JD 生成）</div>
                                <textarea
                                  aria-label="重生成指令"
                                  value={regenInstruction}
                                  onChange={(e) => setRegenInstructions((current) => ({ ...current, [jd.jd_id]: e.target.value }))}
                                  placeholder="例如：更看重 AI/LLM 落地经验，优先蚂蚁/阿里系…"
                                />
                                <div className="case-actions" style={{ marginTop: 8 }}>
                                  <Button variant="secondary" size="sm" disabled={!!regeneratingJdIds[jd.jd_id]} onClick={() => regenerateProfile(jd)}>
                                    {regeneratingJdIds[jd.jd_id] ? "处理中…" : "AI 重新生成"}
                                  </Button>
                                </div>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {jdTotal > 0 && (
          <div className="pagination" style={{ padding: 12 }}>
            <button className="page-number" disabled={jdPage <= 1} onClick={() => onLoadJds(jdPage - 1)}>上一页</button>
            <span style={{ fontSize: 13, color: "#6b7280" }}>第 {jdPage} / {jdTotalPages} 页</span>
            <button className="page-number" disabled={jdPage >= jdTotalPages} onClick={() => onLoadJds(jdPage + 1)}>下一页</button>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card__head">
          <div className="card__title">
            匹配候选人
            {jdMatch && <span className="card__count"> 为「{jdMatch.title}」匹配到 {jdMatch.items.length} 人</span>}
            {jdMatch && jdMatch.items.some((item) => item.eligibility === "pending") && (
              <span className="muted">（{jdMatch.items.filter((item) => item.eligibility === "pending").length} 人方向待核）</span>
            )}
          </div>
          <div className="card__tools">
            {jdMatch?.run_id && (
              <>
                {(!jdReview || jdReview.status === "SUCCESS") && (
                  <>
                    <label className="muted" style={{ fontSize: 12, marginRight: 8, cursor: "pointer" }}>
                      <input type="checkbox" checked={jdReviewReasoning} onChange={(e) => onJdReviewReasoningChange(e.target.checked)} style={{ marginRight: 4 }} />
                      深度思考
                    </label>
                    <button type="button" className="btn btn-ghost btn-xs" onClick={onJdAiReview}>
                      {jdReview ? "重新复核" : "AI 深度复核"}
                    </button>
                  </>
                )}
                {jdReview && ["FAILED", "DEAD_LETTER", "CANCELLED"].includes(jdReview.status) && (
                  <button type="button" className="btn btn-ghost btn-xs" onClick={onJdAiReviewRetry}>重试复核</button>
                )}
                {jdReview && !["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED"].includes(jdReview.status) && (
                  <>
                    <span className="muted">AI 复核 {jdReview.progress}%</span>
                    <button type="button" className="btn btn-ghost btn-xs" onClick={onJdAiReviewCancel}>取消复核</button>
                  </>
                )}
              </>
            )}
            {jdReview?.error_message && (
              <span className="muted" style={{ fontSize: 12 }} role="status">{jdReview.error_message}</span>
            )}
            {jdReview && jdReview.status === "SUCCESS" && jdReview.result_ref && (
              <span className="muted">
                {(() => {
                  const counts = reviewVerdictCounts(jdReviewVerdicts);
                  return [
                    `推荐 ${counts.recommend} / 待核 ${counts.pending} / 不推荐 ${counts.reject}`,
                    counts.failed > 0 ? `（复核失败 ${counts.failed}）` : "",
                  ].filter(Boolean).join(" ");
                })()}
              </span>
            )}
            <div className="column-menu">
              <button className="btn btn-ghost btn-xs" onClick={() => setColumnsMenuOpen((v) => !v)}>列设置</button>
              {columnsMenuOpen && (
                <div className="column-menu__panel">
                  {CANDIDATE_COLUMNS.filter((col) => !SEARCH_REVIEW_COLUMN_KEYS.includes(col.key)).map((col) => (
                    <label key={col.key}>
                      <input type="checkbox" checked={visibleColumns[col.key]} onChange={() => onToggleColumn(col.key)} />
                      {col.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        {jdMatch == null ? (
          <div className="hint" style={{ padding: 16 }}>点击岗位列表中的「匹配候选人」开始检索。</div>
        ) : jdMatch.items.length === 0 ? (
          <div className="empty-state"><strong>暂无匹配</strong><p>没有满足条件的匹配结果。</p></div>
        ) : (
          <>
            <div className="match-context">
              <span>匹配岗位：<strong>{jdMatch.title}</strong>{jdMatch.company ? ` · ${jdMatch.company}` : ""}{jdMatch.location ? ` · ${jdMatch.location}` : ""}</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th className="col-name">姓名</th>
                    <th className="col-review">符合点</th>
                    <th className="col-review">注意点</th>
                    {orderedColumns.map((key) => <th key={key}>{COLUMN_LABELS[key]}</th>)}
                    <th className="col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedMatchItems.slice((jdMatchPage - 1) * 20, jdMatchPage * 20).map((item) => {
                    const v = item.result_id ? jdReviewVerdicts[item.result_id] : undefined;
                    return (
                      <tr key={item.candidate_id}>
                        <td className="col-name">
                          <strong>{item.name}</strong>
                          {item.match_tier === "needs_review" && !v && (
                            <span className="muted" title={item.direction_reason || ""} style={{ marginLeft: 8, fontSize: 12 }}>待核</span>
                          )}
                          {v && (
                            <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                              {reviewVerdictLabel(v.verdict)}
                            </span>
                          )}
                          {v?.failed && (
                            <span className="muted" style={{ marginLeft: 6, fontSize: 12 }}>复核失败</span>
                          )}
                        </td>
                        <td className="col-review">
                          <HoverText text={(v?.reasons || []).join("\n")} />
                        </td>
                        <td className="col-review">
                          <HoverText text={(v?.cautions || []).join("\n")} />
                        </td>
                        {orderedColumns.map((key) => {
                          if (key === "profile") {
                            const points = resumeProfilePoints(item.parsed_data);
                            const profileText = points.length > 0 ? points.join("\n") : (item.parsed_data?.ai_profile_summary ?? "");
                            return (
                              <td key={key}>
                                <button
                                  className="cell-edit"
                                  onClick={() => onEditProfile(
                                    item.candidate_id,
                                    item.parsed_data?.ai_profile_summary ?? null,
                                    item.parsed_data?.ai_profile_source ?? null,
                                    item.parsed_data?.ai_profile_stale ?? false,
                                  )}
                                >
                                  <HoverText text={profileText} />
                                </button>
                              </td>
                            );
                          }
                          return <td key={key}>{candidateColumnText(key, item.parsed_data, item.phone) || "—"}</td>;
                        })}
                        <td className="col-actions">
                          <div className="case-actions">
                            <button className="detail-button" onClick={() => onPreviewResume(item.revision_id, item.name, item.original_filename ?? "")}>查看详情</button>
                            <button className="detail-button" disabled={creatingCase || !item.result_id} onClick={() => onCreateCaseFromMatch(item.result_id || "")}>
                              {matchCaseIds[item.result_id || ""] ? "查看流程" : "建流程"}
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="pagination" style={{ padding: 12 }}>
              <button className="page-number" disabled={jdMatchPage <= 1} onClick={() => setJdMatchPage((p) => p - 1)}>上一页</button>
              <span style={{ fontSize: 13, color: "#6b7280" }}>第 {jdMatchPage} / {Math.max(1, Math.ceil(sortedMatchItems.length / 20))} 页</span>
              <button className="page-number" disabled={jdMatchPage >= Math.ceil(sortedMatchItems.length / 20)} onClick={() => setJdMatchPage((p) => p + 1)}>下一页</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
