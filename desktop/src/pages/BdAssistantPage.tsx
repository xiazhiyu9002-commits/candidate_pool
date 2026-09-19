import type { FormEvent } from "react";
import type { BdAgentLead, BdPoolCandidate, BdProgress } from "../App";
import { LoadingButton, LongTaskProgress } from "../components/Loading";
import { Button } from "../components/ui";

export interface BdAssistantPageProps {
  bdQuery: string;
  onBdQueryChange: (value: string) => void;
  bdFollowUp: string;
  onBdFollowUpChange: (value: string) => void;
  bdSessionId: string | null;
  bdLeads: BdAgentLead[];
  bdLoading: boolean;
  bdProgress: BdProgress | null;
  bdPoolByLead: Record<string, BdPoolCandidate[]>;
  bdPoolBusyId: string | null;
  collapsedPool: Record<string, boolean>;
  onSearchBd: (event: FormEvent) => void;
  onFollowUpBd: (event: FormEvent) => void;
  onLookupPool: (leadId: string) => void;
  onTogglePoolCollapse: (leadId: string) => void;
  onOpenExternal: (url: string) => void;
  onCopyLink: (url: string) => void;
  onPreviewResume: (revisionId: string, name?: string) => void;
}

export function BdAssistantPage({
  bdQuery, onBdQueryChange, bdFollowUp, onBdFollowUpChange, bdSessionId, bdLeads,
  bdLoading, bdProgress, bdPoolByLead, bdPoolBusyId, collapsedPool,
  onSearchBd, onFollowUpBd, onLookupPool, onTogglePoolCollapse, onOpenExternal, onCopyLink, onPreviewResume,
}: BdAssistantPageProps) {
  return (
    <section>
      <form className="search-bar" onSubmit={(event) => void onSearchBd(event)}>
        <input value={bdQuery} onChange={(e) => onBdQueryChange(e.target.value)} placeholder="输入自然语言需求，如「找上海做大模型算法的公司，最好在招人」" aria-label="BD 深度检索" />
        <LoadingButton type="submit" loading={bdLoading} className="btn btn-primary">深度检索</LoadingButton>
      </form>

      <LongTaskProgress message={bdProgress?.message ?? null} />

      {bdSessionId && (
        <form className="followup" onSubmit={(event) => void onFollowUpBd(event)}>
          <input value={bdFollowUp} onChange={(e) => onBdFollowUpChange(e.target.value)} placeholder="追问：补充或修正检索方向…" aria-label="BD 追问" />
          <LoadingButton type="submit" loading={bdLoading} className="btn btn-secondary">追问</LoadingButton>
        </form>
      )}

      {bdLeads.length === 0 ? (
        <div className="empty-state"><strong>暂无线索</strong><p>输入需求开始深度检索，系统会规划搜索、抓取页面并综合出带证据的线索。</p></div>
      ) : (
        <div className="card">
          <div className="card__head">
            <div className="card__title">线索 <span className="card__count">{bdLeads.length} 条</span></div>
          </div>
          <div style={{ padding: 16 }}>
            <div className="leads">
              {bdLeads.map((lead) => (
                <div key={lead.id} className="lead">
                  <div className="lead-head">
                    <button type="button" className="company" onClick={() => onLookupPool(lead.id)}>{lead.company_name}</button>
                    <span className="role">{lead.job_title ?? "岗位未知"}</span>
                    {lead.is_hiring === true && <span className="tag tag-ok">在招</span>}
                    {lead.is_hiring === false && <span className="tag tag-bad">未在招</span>}
                    {lead.confidence != null && <span className="tag tag-muted">置信度 {Math.round(lead.confidence * 100)}%</span>}
                  </div>
                  {(lead.posted_time || lead.salary_range || lead.level) && (
                    <div className="meta">
                      {lead.posted_time && <span>开放时间：{lead.posted_time}</span>}
                      {lead.salary_range && <span>薪资：{lead.salary_range}</span>}
                      {lead.level && <span>职级：{lead.level}</span>}
                    </div>
                  )}
                  {lead.requirements.length > 0 && (
                    <ul className="requirements">
                      {lead.requirements.map((req, index) => <li key={index}>{req}</li>)}
                    </ul>
                  )}
                  {lead.summary && <p className="summary">{lead.summary}</p>}
                  {lead.url && (
                    <div className="bd-link-row">
                      <Button variant="ghost" size="sm" onClick={() => onOpenExternal(lead.url as string)}>打开链接</Button>
                      <Button variant="ghost" size="sm" onClick={() => onCopyLink(lead.url as string)}>复制链接</Button>
                    </div>
                  )}
                  {lead.evidence.length > 0 && (
                    <ul className="evidence">
                      {lead.evidence.map((item, index) => (
                        <li key={index}>
                          {item.claim ? <strong>{item.claim}</strong> : null}
                          {item.quote ? <span>：{item.quote}</span> : null}
                          {item.source_url ? <button type="button" className="bd-source-link" onClick={() => onOpenExternal(item.source_url as string)}>（来源）</button> : null}
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="pool">
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <Button variant="ghost" size="sm" disabled={bdPoolBusyId === lead.id} onClick={() => onLookupPool(lead.id)}>
                        {bdPoolBusyId === lead.id ? "查询中…" : "查人才库"}
                      </Button>
                      {(bdPoolByLead[lead.id] ?? []).length > 0 && (
                        <Button variant="ghost" size="sm" onClick={() => onTogglePoolCollapse(lead.id)}>
                          {collapsedPool[lead.id] ? "展开" : "收起"}
                        </Button>
                      )}
                    </div>
                    {!collapsedPool[lead.id] && (bdPoolByLead[lead.id] ?? []).length > 0 && (
                      <ul className="pool-results">
                        {bdPoolByLead[lead.id].map((c) => (
                          <li key={c.candidate_id}>
                            <span>{c.name}{c.phone ? ` · ${c.phone}` : ""}</span>
                            <Button variant="ghost" size="sm" onClick={() => onPreviewResume(c.revision_id, c.name)}>预览简历</Button>
                          </li>
                        ))}
                      </ul>
                    )}
                    {bdPoolByLead[lead.id] && bdPoolByLead[lead.id].length === 0 && (
                      <p className="muted">人才库中未找到相关候选人</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
