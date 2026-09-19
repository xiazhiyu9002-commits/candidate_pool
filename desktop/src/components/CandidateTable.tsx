import { useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import type { ParsedEducationData, ParsedResumeData } from "../App";
import { EditableCell } from "./EditableCell";
import { Button, HoverText, OverflowMenu, StatusBadge, stripEvidenceRefs } from "./ui";

export const CANDIDATE_COLUMNS = [
  { key: "phone", label: "电话" },
  { key: "school", label: "学校学历" },
  { key: "school_level", label: "学校等级" },
  { key: "age", label: "年龄" },
  { key: "gender", label: "性别" },
  { key: "city", label: "所在城市" },
  { key: "years", label: "工作年限" },
  { key: "company", label: "公司" },
  { key: "title", label: "职位" },
  { key: "job_level", label: "职级" },
  { key: "salary", label: "薪资" },
  { key: "profile", label: "AI 画像" },
  { key: "highlights", label: "亮点" },
  { key: "risks", label: "风险点" },
] as const;

export type CandidateColumnKey = (typeof CANDIDATE_COLUMNS)[number]["key"];

/** 只有搜索侧 AI 复核才产出的列：岗位匹配页不复用（避免出现两列永远空值）。 */
export const SEARCH_REVIEW_COLUMN_KEYS: CandidateColumnKey[] = ["highlights", "risks"];

export const CANDIDATE_COLUMNS_DEFAULT: Record<CandidateColumnKey, boolean> = {
  phone: true,
  school: true,
  school_level: true,
  age: true,
  gender: true,
  city: true,
  years: true,
  company: true,
  title: true,
  job_level: true,
  salary: true,
  profile: true,
  // 复核列默认关闭：触发搜索侧 AI 复核时才自动展开。
  highlights: false,
  risks: false,
};

export const CANDIDATE_COLUMNS_ORDER_DEFAULT: CandidateColumnKey[] = [
  "phone", "school", "school_level", "age", "gender", "city", "years", "company", "title", "job_level", "salary", "profile",
  "highlights", "risks",
];

export const COLUMN_LABELS: Record<CandidateColumnKey, string> = {
  phone: "电话",
  school: "学校学历",
  school_level: "学校等级",
  age: "年龄",
  gender: "性别",
  city: "所在城市",
  years: "工作年限",
  company: "公司",
  title: "职位",
  job_level: "职级",
  salary: "薪资",
  profile: "AI 画像",
  highlights: "亮点",
  risks: "风险点",
};

// 每列默认宽度（像素），用于表格横向滚动与拖拽调整列宽。
const DEFAULT_COL_WIDTHS: Record<string, number> = {
  name: 120,
  phone: 120,
  school: 180,
  school_level: 90,
  age: 60,
  gender: 60,
  city: 90,
  years: 80,
  company: 140,
  title: 140,
  job_level: 80,
  salary: 90,
  profile: 180,
  highlights: 220,
  risks: 220,
  actions: 280,
};

function degreeLabel(degree: string | null | undefined): string {
  const map: Record<string, string> = {
    "博士": "博士", "硕士": "硕士", "本科": "本科", "大专": "大专",
    "DOCTORATE": "博士", "MASTER": "硕士", "BACHELOR": "本科", "ASSOCIATE": "大专",
  };
  return degree ? (map[degree] ?? degree) : "";
}

function schoolDegreeLabel(p: ParsedResumeData | null | undefined): string {
  if (!p) return "";
  // 只展示最高学历（点击后进入教育经历编辑器查看完整学历）
  if (p.school) return `${p.school}${degreeLabel(p.highest_degree) || ""}`;
  const edus = (p.educations || []).filter((e) => e.school);
  if (edus.length > 0) {
    const highest = edus[0];
    return `${highest.school}${degreeLabel(highest.degree) || ""}`;
  }
  return degreeLabel(p.highest_degree) || "";
}

const DEGREE_RANK: Record<string, number> = {
  "博士": 4, "DOCTORATE": 4,
  "硕士": 3, "MASTER": 3,
  "本科": 2, "BACHELOR": 2,
  "大专": 1, "ASSOCIATE": 1,
};

function qsBand(rank: number | null | undefined): string {
  if (rank == null) return "";
  if (rank <= 50) return "前50";
  if (rank <= 100) return "前100";
  if (rank <= 150) return "前150";
  if (rank <= 200) return "前200";
  if (rank <= 300) return "前300";
  return "300后";
}

const CN_REGIONS = new Set(["中国", "国内", "大陆", "中国大陆"]);

export const SCHOOL_TIER_OPTIONS = ["985", "211", "双一流", "普本", "大专", "前50", "前100", "前150", "前200", "前300", "300后"];

function schoolTierLabel(p: ParsedResumeData | null | undefined): string {
  if (!p) return "";
  // 取最高学历对应的教育经历（博士 > 硕士 > 本科 > 大专）
  let highest: ParsedEducationData | null = null;
  let highestRank = -1;
  for (const edu of p.educations || []) {
    const rank = DEGREE_RANK[edu.degree ?? ""] ?? 0;
    if (rank > highestRank) {
      highest = edu;
      highestRank = rank;
    }
  }
  // 无教育经历：回退到顶层兼容字段
  if (highest == null) {
    if (p.qs_rank != null) return qsBand(p.qs_rank);
    return p.school_level ?? "";
  }
  const tags = highest.school_tags || [];
  const region = (highest.country_region || "").trim();
  // 判定海外：标签含「海外」，或 country_region 非空且不是中国
  const isOverseas = tags.includes("海外") || (!!region && !CN_REGIONS.has(region));
  if (isOverseas) {
    if (highest.qs_rank != null) return qsBand(highest.qs_rank);
    return "普通";
  }
  // 国内院校：985 > 211 > 双一流 > 普本 > 大专 > 普通
  if (tags.includes("985")) return "985";
  if (tags.includes("211")) return "211";
  if (tags.includes("双一流")) return "双一流";
  const degree = highest.degree ?? p.highest_degree ?? "";
  if (DEGREE_RANK[degree] === 1) return "大专";
  if (degree) return "普本";
  return "普通";
}

function profileSummaryText(summary: string | null | undefined): string {
  const first = (summary ?? "").split("\n")[0] ?? "";
  return first.length > 40 ? first.slice(0, 40) + "…" : first;
}

// 分点画像：优先结构化分点，否则按整体段落换行拆点（与后端 _profile_point_items 一致）。
function profilePoints(p: ParsedResumeData | null | undefined): string[] {
  const points = (p?.ai_profile_points ?? []).map((x) => x.text).filter(Boolean);
  if (points.length) return points;
  return (p?.ai_profile_summary ?? "").split(/\n+/).map((s) => s.trim()).filter(Boolean);
}

// 供匹配结果等只读表格复用：按列 key 返回展示文本（与人才库表格一致）。
export function candidateColumnText(key: CandidateColumnKey, parsed: ParsedResumeData | null, phone: string | null): string {
  switch (key) {
    case "phone":
      return phone ?? "";
    case "school":
      return schoolDegreeLabel(parsed) || "—";
    case "school_level":
      return (parsed?.school_tier || schoolTierLabel(parsed)) || "—";
    case "age": {
      const ageText = parsed?.birth_year != null
        ? String(new Date().getFullYear() - parsed.birth_year)
        : (parsed?.age != null ? String(parsed.age) : "");
      return ageText || "—";
    }
    case "gender":
      return parsed?.gender ?? "";
    case "city":
      return parsed?.location ?? "";
    case "years":
      return parsed?.total_years != null ? String(Math.floor(parsed.total_years)) : "";
    case "company":
      return parsed?.current_company ?? "";
    case "title":
      return parsed?.current_title ?? "";
    case "job_level":
      return parsed?.job_level ?? "";
    case "salary":
      return parsed?.salary ?? "";
    case "profile":
      return profileSummaryText(parsed?.ai_profile_summary) || "—";
    default:
      return "";
  }
}

function reviewActionLabel(revisionStatus: string | null | undefined, reviewError: string | null | undefined): string {
  if (revisionStatus === "FAILED" || reviewError) return "解析失败·重新解析";
  return "";
}

function parseYears(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) ? Math.floor(number) : null;
}

// 复核结论按行展示，并在前端统一去掉证据编号（与匹配侧的「符合点/注意点」口径一致）。
function reviewLines(items: string[] | undefined): string {
  const lines = (items || []).map(stripEvidenceRefs).filter(Boolean);
  return lines.length > 0 ? lines.join("\n") : "—";
}

function profileBadge(p: ParsedResumeData | null | undefined) {
  const source = p?.ai_profile_source;
  if (source === "manual") return <StatusBadge tone="warning">人工编辑</StatusBadge>;
  if (source === "ai") return <StatusBadge tone="primary">AI 生成</StatusBadge>;
  return null;
}

export interface CandidateRow {
  key: string;
  candidateId: string;
  name: string;
  phone: string | null;
  revisionId: string;
  filename: string;
  parsed: ParsedResumeData | null;
  revisionStatus?: string | null;
  reviewError?: string | null;
  /** 搜索侧 AI 复核结论：亮点 / 风险点（未复核时为空）。 */
  highlights?: string[];
  risks?: string[];
}

export interface CandidateTableProps {
  rows: CandidateRow[];
  visibleColumns: Record<CandidateColumnKey, boolean>;
  columnOrder: CandidateColumnKey[];
  onMoveColumn: (fromKey: CandidateColumnKey, toKey: CandidateColumnKey) => void;
  onUpdateField: (candidateId: string, field: string, value: unknown) => Promise<void>;
  onEditEducation: (candidateId: string, educations: ParsedEducationData[]) => void;
  onEditProfile: (candidateId: string, summary: string | null, source: string | null, stale: boolean) => void;
  onMatch: (candidateId: string, name: string) => void;
  onPreview: (revisionId: string, name?: string, filename?: string) => void;
  onDownload: (revisionId: string, filename: string) => void;
  onCreateCase: (candidateId: string, name: string) => void;
  /** 常规重新解析（异常页才 OCR）；强制 OCR 是扫描件/乱码专用。 */
  onReparse: (revisionId: string) => Promise<void>;
  onForceReparse: (revisionId: string) => Promise<void>;
  onOpenReview: (revisionId: string) => void;
  onOpenParsed: (candidateId: string, parsed: ParsedResumeData | null) => void;
  onDelete: (candidateId: string) => void;
  selectable?: boolean;
  selectedIds?: ReadonlySet<string>;
  onToggleSelect?: (candidateId: string) => void;
  onToggleSelectAll?: (checked: boolean, candidateIds: string[]) => void;
}

export function CandidateTable({
  rows,
  visibleColumns,
  columnOrder,
  onMoveColumn,
  onUpdateField,
  onEditEducation,
  onEditProfile,
  onMatch,
  onPreview,
  onDownload,
  onCreateCase,
  onReparse,
  onForceReparse,
  onOpenReview,
  onOpenParsed,
  onDelete,
  selectable = false,
  selectedIds,
  onToggleSelect,
  onToggleSelectAll,
}: CandidateTableProps) {
  const draggingKey = useRef<CandidateColumnKey | null>(null);
  const [colWidths, setColWidths] = useState<Record<string, number>>({ ...DEFAULT_COL_WIDTHS });
  const resizeState = useRef<{ key: string; startX: number; startWidth: number } | null>(null);
  const orderedColumns = columnOrder.filter((key) => visibleColumns[key]);

  const columnWidth = (key: string) => colWidths[key] ?? DEFAULT_COL_WIDTHS[key] ?? 120;
  const totalWidth = columnWidth("name")
    + orderedColumns.reduce((sum, key) => sum + columnWidth(key), 0)
    + columnWidth("actions")
    + (selectable ? 40 : 0);

  function startResize(event: ReactMouseEvent, key: string) {
    event.preventDefault();
    event.stopPropagation();
    const width = columnWidth(key);
    resizeState.current = { key, startX: event.clientX, startWidth: width };
    window.addEventListener("mousemove", moveResize);
    window.addEventListener("mouseup", endResize);
  }

  function moveResize(event: MouseEvent) {
    if (!resizeState.current) return;
    const { key, startX, startWidth } = resizeState.current;
    const next = Math.max(0, startWidth + (event.clientX - startX));
    setColWidths((prev) => ({ ...prev, [key]: next }));
  }

  function endResize() {
    resizeState.current = null;
    window.removeEventListener("mousemove", moveResize);
    window.removeEventListener("mouseup", endResize);
  }

  function renderHeader(key: CandidateColumnKey) {
    return (
      <th
        key={key}
        style={{ width: columnWidth(key), position: "relative" }}
        draggable
        onDragStart={(e) => {
          draggingKey.current = key;
          e.dataTransfer.setData("text/plain", key);
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={(e) => e.preventDefault()}
        onDrop={() => {
          if (draggingKey.current && draggingKey.current !== key) {
            onMoveColumn(draggingKey.current, key);
          }
          draggingKey.current = null;
        }}
        onDragEnd={() => { draggingKey.current = null; }}
      >
        {COLUMN_LABELS[key]}
        <span className="col-resize-handle" onMouseDown={(e) => startResize(e, key)} />
      </th>
    );
  }

  function renderCell(key: CandidateColumnKey, r: CandidateRow, p: ParsedResumeData | null): ReactNode {
    const yearsText = p?.total_years != null ? String(Math.floor(p.total_years)) : "";
    const ageText = p?.birth_year != null ? String(new Date().getFullYear() - p.birth_year) : (p?.age != null ? String(p.age) : "");
    switch (key) {
      case "phone":
        return <td key={key}><EditableCell value={r.phone ?? ""} onSave={(next) => onUpdateField(r.candidateId, "phone", next.trim() || null)} /></td>;
      case "school":
        return <td key={key}><button className="cell-edit" onClick={() => onEditEducation(r.candidateId, p?.educations || [])}>{schoolDegreeLabel(p) || "—"}</button></td>;
      case "school_level":
        return (
          <td key={key}>
            <select
              className="cell-select"
              aria-label="学校等级"
              value={p?.school_tier || schoolTierLabel(p)}
              onChange={(e) => onUpdateField(r.candidateId, "school_tier", e.target.value || null)}
            >
              <option value="">—</option>
              {SCHOOL_TIER_OPTIONS.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          </td>
        );
      case "age":
        return <td key={key}><EditableCell value={ageText} onSave={(next) => onUpdateField(r.candidateId, "age", parseInt(next, 10) || null)} /></td>;
      case "gender":
        return <td key={key}><EditableCell value={p?.gender ?? ""} onSave={(next) => onUpdateField(r.candidateId, "gender", next.trim() || null)} /></td>;
      case "city":
        return <td key={key}><EditableCell value={p?.location ?? ""} onSave={(next) => onUpdateField(r.candidateId, "location", next.trim() || null)} /></td>;
      case "years":
        return <td key={key}><EditableCell value={yearsText} onSave={(next) => onUpdateField(r.candidateId, "total_years", parseYears(next))} /></td>;
      case "company":
        return <td key={key}><EditableCell value={p?.current_company ?? ""} onSave={(next) => onUpdateField(r.candidateId, "current_company", next.trim() || null)} /></td>;
      case "title":
        return <td key={key}><EditableCell value={p?.current_title ?? ""} onSave={(next) => onUpdateField(r.candidateId, "current_title", next.trim() || null)} /></td>;
      case "job_level":
        return <td key={key}><EditableCell value={p?.job_level ?? ""} onSave={(next) => onUpdateField(r.candidateId, "job_level", next.trim() || null)} /></td>;
      case "salary":
        return <td key={key}><EditableCell value={p?.salary ?? ""} onSave={(next) => onUpdateField(r.candidateId, "salary", next.trim() || null)} /></td>;
      case "profile": {
        const points = profilePoints(p);
        const fullText = points.length > 0 ? points.join("\n") : (p?.ai_profile_summary ?? "");
        return (
          <td key={key}>
            <button className="cell-edit" onClick={() => onEditProfile(r.candidateId, p?.ai_profile_summary ?? null, p?.ai_profile_source ?? null, p?.ai_profile_stale ?? false)}>
              <HoverText text={fullText} />
            </button>
            {profileBadge(p)}
          </td>
        );
      }
      case "highlights":
        return <td key={key}><HoverText text={reviewLines(r.highlights)} /></td>;
      case "risks":
        return <td key={key}><HoverText text={reviewLines(r.risks)} /></td>;
      default:
        return <td key={key} />;
    }
  }

  return (
    <table className="resizable-table" style={{ tableLayout: "fixed", width: totalWidth }}>
      <colgroup>
        {selectable && <col style={{ width: 40 }} />}
        <col style={{ width: columnWidth("name") }} />
        {orderedColumns.map((key) => <col key={key} style={{ width: columnWidth(key) }} />)}
        <col style={{ width: columnWidth("actions") }} />
      </colgroup>
      <thead>
        <tr>
          {selectable && (
            <th className="col-select" style={{ width: 40 }}>
              <input
                type="checkbox"
                aria-label="全选当前页"
                checked={rows.length > 0 && rows.every((r) => selectedIds?.has(r.candidateId))}
                onChange={(e) => onToggleSelectAll?.(e.target.checked, rows.map((r) => r.candidateId))}
              />
            </th>
          )}
          <th className="col-name" style={{ width: columnWidth("name") }}>
            姓名
            <span className="col-resize-handle" onMouseDown={(e) => startResize(e, "name")} />
          </th>
          {orderedColumns.map((key) => renderHeader(key))}
          <th className="col-actions" style={{ width: columnWidth("actions") }}>
            操作
            <span className="col-resize-handle" onMouseDown={(e) => startResize(e, "actions")} />
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const p = r.parsed;
          return (
            <tr key={r.key}>
              {selectable && (
                <td className="col-select">
                  <input
                    type="checkbox"
                    aria-label={`选择 ${r.name}`}
                    checked={selectedIds?.has(r.candidateId) ?? false}
                    onChange={() => onToggleSelect?.(r.candidateId)}
                  />
                </td>
              )}
              <td className="col-name"><strong><EditableCell value={r.name} onSave={(next) => onUpdateField(r.candidateId, "name", next.trim() || null)} /></strong></td>
              {orderedColumns.map((key) => renderCell(key, r, p))}
              <td className="col-actions">
                <div className="row-actions">
                  <span className="row-actions__group">
                    <Button variant="ghost" size="sm" disabled={!!r.revisionStatus && r.revisionStatus !== "READY"} onClick={() => onMatch(r.candidateId, r.name)}>匹配</Button>
                    <Button variant="ghost" size="sm" onClick={() => onPreview(r.revisionId, r.name, r.filename)}>查看详情</Button>
                    <Button variant="ghost" size="sm" onClick={() => onCreateCase(r.candidateId, r.name)}>建流程</Button>
                    {reviewActionLabel(r.revisionStatus, r.reviewError) && (
                      <Button variant="ghost" size="sm" onClick={() => onOpenReview(r.revisionId)}>{reviewActionLabel(r.revisionStatus, r.reviewError)}</Button>
                    )}
                    <OverflowMenu
                      items={[
                        { key: "parsed", label: "解析表", onSelect: () => onOpenParsed(r.candidateId, r.parsed) },
                        { key: "download", label: "下载", onSelect: () => onDownload(r.revisionId, r.filename || r.name) },
                        { key: "reparse", label: "重新解析", onSelect: () => void onReparse(r.revisionId).catch(() => {}) },
                        { key: "ocr", label: "强制 OCR", onSelect: () => void onForceReparse(r.revisionId).catch(() => {}) },
                        { key: "delete", label: "删除", danger: true, onSelect: () => onDelete(r.candidateId) },
                      ]}
                    />
                  </span>
                </div>
                {r.reviewError && <div className="virtual-row-detail"><p className="error-banner">{r.reviewError}</p></div>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
