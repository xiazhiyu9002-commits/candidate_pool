import { useState } from "react";
import type { CaseItem, JdListItem } from "../App";
import { Button, StatusBadge } from "../components/ui";

export interface RecruitmentPageProps {
  caseJdFilter: string;
  cases: CaseItem[];
  casePage: number;
  caseTotal: number;
  caseTotalPages: number;
  jds: JdListItem[];
  onCaseJdFilterChange: (value: string) => void;
  onLoadCases: (page: number, jdId?: string) => void;
  onOpenCase: (caseId: string) => void;
  onDeleteCase: (caseId: string) => void;
  onBulkDeleteCases: (caseIds: string[]) => void;
}

function stageBadge(stage: string) {
  if (stage === "Offer" || stage === "入职") return <StatusBadge tone="success">{stage}</StatusBadge>;
  if (stage === "客户拒绝" || stage === "候选人拒绝") return <StatusBadge tone="danger">{stage}</StatusBadge>;
  return <StatusBadge tone="primary">{stage}</StatusBadge>;
}

function formatShortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

export function RecruitmentPage({
  caseJdFilter,
  cases,
  casePage,
  caseTotal,
  caseTotalPages,
  jds,
  onCaseJdFilterChange,
  onLoadCases,
  onOpenCase,
  onDeleteCase,
  onBulkDeleteCases,
}: RecruitmentPageProps) {
  const [selectedCaseIds, setSelectedCaseIds] = useState<Set<string>>(new Set());
  return (
    <section>
      <div className="filter-bar">
        <select
          aria-label="流程岗位"
          value={caseJdFilter}
          onChange={(event) => {
            onCaseJdFilterChange(event.target.value);
            onLoadCases(1, event.target.value || undefined);
          }}
        >
          <option value="">全部岗位</option>
          {jds.map((jd) => <option key={jd.jd_id} value={jd.jd_id}>{jd.company} · {jd.title}</option>)}
        </select>
        <span className="filter-count">共 {caseTotal} 条流程</span>
      </div>

      <div className="card">
        {selectedCaseIds.size > 0 && (
          <div className="bulk-toolbar" role="toolbar" aria-label="批量删除流程">
            <span className="bulk-toolbar__count">已选 {selectedCaseIds.size} 条流程</span>
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                if (window.confirm(`确定删除选中的 ${selectedCaseIds.size} 条招聘流程吗？`)) {
                  onBulkDeleteCases(Array.from(selectedCaseIds));
                  setSelectedCaseIds(new Set());
                }
              }}
            >
              删除
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelectedCaseIds(new Set())}>取消选择</Button>
          </div>
        )}
        {cases.length === 0 ? (
          <p className="muted" style={{ padding: 16 }}>暂无招聘流程，可从匹配结果建立流程。</p>
        ) : (
          <>
            <div className="table-scroll">
              <table>
                <thead><tr><th style={{ width: 36 }}><input type="checkbox" aria-label="全选流程" checked={cases.length > 0 && cases.every((c) => selectedCaseIds.has(c.id))} onChange={(e) => setSelectedCaseIds(e.target.checked ? new Set(cases.map((c) => c.id)) : new Set())} /></th><th>候选人</th><th>岗位</th><th>阶段</th><th>最近事件</th><th>操作</th></tr></thead>
                <tbody>
                  {cases.map((item) => {
                    const dateText = formatShortDate(item.last_event_at);
                    return (
                      <tr key={item.id}>
                        <td><input type="checkbox" aria-label={`选择 ${item.candidate_name || item.candidate_id}`} checked={selectedCaseIds.has(item.id)} onChange={() => setSelectedCaseIds((current) => { const next = new Set(current); if (next.has(item.id)) next.delete(item.id); else next.add(item.id); return next; })} /></td>
                        <td><strong>{item.candidate_name || item.candidate_id}</strong></td>
                        <td>{item.company || ""} · {item.jd_title || item.jd_id}</td>
                        <td>{stageBadge(item.stage)}</td>
                        <td>{item.last_event ? `${item.last_event}${dateText ? ` · ${dateText}` : ""}` : "—"}</td>
                        <td>
                          <div className="case-actions">
                            <Button variant="secondary" size="sm" onClick={() => onOpenCase(item.id)}>查看流程</Button>
                            <Button variant="danger" size="sm" onClick={() => onDeleteCase(item.id)}>删除流程</Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="pagination" style={{ padding: 12 }}>
              <button className="page" aria-label="上一页" disabled={casePage <= 1} onClick={() => onLoadCases(casePage - 1)}>‹</button>
              <span className="hint">第 {casePage} / {caseTotalPages} 页</span>
              <button className="page" aria-label="下一页" disabled={casePage >= caseTotalPages} onClick={() => onLoadCases(casePage + 1)}>›</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
