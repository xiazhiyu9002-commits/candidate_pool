import { useRef, useState } from "react";
import type { CaseActionInput, CaseDetail, CaseEventItem, RecruitmentApi } from "../App";
import { RemindersPanel } from "./RemindersPanel";

const labels: Record<string, string> = {
  RECOMMENDED: "已推荐", INTERVIEW_ENTERED: "进入面试", INTERVIEW_RESULT: "面试结果",
  OFFER: "Offer", ONBOARDED: "入职", EXIT: "退出",
};

function ordered(events: CaseEventItem[]) {
  return [...events].sort((a, b) => Date.parse(a.occurred_at) - Date.parse(b.occurred_at)
    || Date.parse(a.recorded_at) - Date.parse(b.recorded_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

type Attempt = { request: () => Promise<unknown>; committed: boolean };

export function CaseDrawer({ api, initialCase, onClose, onUpdated }: {
  api: RecruitmentApi; initialCase: CaseDetail; onClose: () => void; onUpdated: (detail: CaseDetail) => void;
}) {
  const [detail, setDetail] = useState(initialCase);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [occurredAt, setOccurredAt] = useState("");
  const [note, setNote] = useState("");
  const [roundName, setRoundName] = useState("");
  const valid = ordered(detail.events.filter((event) => event.status === "active"));
  const entered = new Set(valid.filter((event) => event.event_type === "INTERVIEW_ENTERED").map((event) => event.case_round_id));
  const effectiveRounds = detail.rounds.filter((round) => entered.has(round.id));
  const currentRound = [...effectiveRounds].sort((a, b) => b.round_no - a.round_no)[0];
  const latestResult = (roundId: string) => valid.filter((event) => event.event_type === "INTERVIEW_RESULT" && event.case_round_id === roundId).at(-1);
  const currentResult = currentRound && latestResult(currentRound.id);
  const terminal = valid.some((event) => event.event_type === "EXIT" || event.event_type === "ONBOARDED")
    || (currentResult && ["未通过", "候选人退出", "取消"].includes(currentResult.result || ""));
  const canAdvance = detail.can_advance !== false && !terminal;
  const hasRecommendation = valid.some((event) => event.event_type === "RECOMMENDED");
  const offer = valid.filter((event) => event.event_type === "OFFER").at(-1);
  const hasIssued = valid.some((event) => event.event_type === "OFFER" && event.result === "已发放");
  const unresolved = currentRound && (!currentResult || currentResult.result === "待反馈");
  const template = detail.process_rounds || [];
  const hasNextRound = template.length === 0 || !currentRound || template.some((round) => round.round_no > currentRound.round_no);
  const canAddRound = hasNextRound || roundName.trim().length > 0;
  const actionDisabled = busy || !!attempt || !canAdvance;
  const timeSelected = occurredAt.trim().length > 0;

  async function execute(next: Attempt) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    setAttempt(next);
    try {
      if (!next.committed) {
        await next.request();
        next.committed = true;
      }
      const refreshed = await api.getCase(detail.id);
      setDetail(refreshed);
      onUpdated(refreshed);
      setAttempt(null);
      setNote("");
      setOccurredAt("");
      setRoundName("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请重试");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  function run(action: (payload: CaseActionInput) => Promise<unknown>) {
    if (lock.current || attempt) return;
    const payload: CaseActionInput = {
      idempotency_key: crypto.randomUUID(),
      occurred_at: occurredAt ? occurredAt + (occurredAt.length === 16 ? ":00+08:00" : "+08:00") : undefined,
      note: note.trim() || undefined,
    };
    void execute({ request: () => action(payload), committed: false });
  }

  function close() {
    if (!busy && !attempt) onClose();
  }

  return <div className="match-drawer-backdrop" onClick={close}>
    <aside className="match-drawer workflow-drawer" role="dialog" aria-modal="true" aria-label="流程中" onClick={(event) => event.stopPropagation()}>
      <div className="match-drawer-header">
        <div><h2>流程中</h2><small>{detail.candidate_name || detail.candidate_id} · {detail.company} {detail.jd_title || detail.jd_id} · {detail.stage}</small></div>
        <button className="detail-button" disabled={busy || !!attempt} onClick={close}>关闭</button>
      </div>
      <div className="match-drawer-body">
        {detail.note && <p>{detail.note}</p>}
        {detail.blocked_reason && <p role="status">{detail.blocked_reason}</p>}
        {error && <div role="alert">{error}</div>}
        {attempt && !busy && <div className="case-actions">
          <button className="import-button" onClick={() => void execute(attempt)}>重试上次操作</button>
          <button className="detail-button" onClick={async () => {
            setBusy(true);
            try { const refreshed = await api.getCase(detail.id); setDetail(refreshed); onUpdated(refreshed); setError(null); }
            catch (caught) { setError(caught instanceof Error ? caught.message : "刷新失败"); }
            finally { setBusy(false); }
          }}>核对最新状态</button>
          <button className="detail-button" onClick={() => { setAttempt(null); setError(null); }}>结束本次重试</button>
          <small>响应中断时操作可能已保存；重试会沿用同一请求编号。结束重试前请先核对最新状态。</small>
        </div>}
        <fieldset disabled={busy || !!attempt} style={{ border: 0, padding: 0 }}>
          <label>发生时间（上海）<input type="datetime-local" aria-label="发生时间（上海）" value={occurredAt} onChange={(event) => setOccurredAt(event.target.value)} /></label>
          <small>请先选择发生时间，再执行操作；补录按上海时间保存。</small>
          <label>操作备注<input aria-label="操作备注" value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <label>临时轮次名称<input aria-label="临时轮次名称" placeholder="留空沿用流程模板" value={roundName} onChange={(event) => setRoundName(event.target.value)} /></label>
          {!hasNextRound && !hasIssued && <small>已到模板最后一轮。如需加面，请明确填写临时轮次名称。</small>}
        </fieldset>
        <div className="case-actions">
          <button className="import-button" disabled={actionDisabled || !timeSelected || hasRecommendation} onClick={() => run((payload) => api.recommendCase(detail.id, payload))}>已推荐</button>
          <button className="import-button" disabled={actionDisabled || !timeSelected || !hasRecommendation || !!unresolved || hasIssued || !canAddRound} onClick={() => run((payload) => api.enterInterview(detail.id, { ...payload, round_name: roundName.trim() || undefined }))}>进入面试</button>
          <button className="detail-button" disabled={actionDisabled || !timeSelected || hasIssued || !!unresolved} onClick={() => run((payload) => api.offerCase(detail.id, payload))}>发 Offer</button>
          <button className="detail-button" disabled={actionDisabled || !timeSelected || !offer || !["已发放", "已接受"].includes(offer.result || "")} onClick={() => run((payload) => api.onboardCase(detail.id, payload))}>确认入职</button>
          <button className="detail-button" disabled={actionDisabled || !timeSelected} onClick={() => run((payload) => api.exitCase(detail.id, undefined, payload))}>退出</button>
        </div>
        {detail.rounds.length > 0 && <div className="section-heading"><h3>面试轮次</h3></div>}
        {detail.rounds.map((round) => {
          const result = entered.has(round.id) ? latestResult(round.id) : undefined;
          const active = round.id === currentRound?.id && (!result || result.result === "待反馈") && !hasIssued;
          return <div key={round.id} className="case-row">
            <div><strong>第{round.round_no}轮 · {round.round_name}</strong><small aria-label={`第${round.round_no}轮当前结果`}>{result?.result || "待反馈"}</small></div>
            {active && <div className="case-actions">
              <button className="detail-button" disabled={actionDisabled || !timeSelected} onClick={() => run((payload) => api.recordResult(detail.id, round.id, "通过", payload))}>通过（结束面试）</button>
              {canAddRound && <button className="detail-button" disabled={actionDisabled || !timeSelected} onClick={() => run((payload) => api.passAndAdvance(detail.id, round.id, { ...payload, next_round_name: roundName.trim() || undefined }))}>通过进下一轮</button>}
              <button className="detail-button" disabled={actionDisabled || !timeSelected} onClick={() => run((payload) => api.recordResult(detail.id, round.id, "未通过", payload))}>未通过</button>
              <button className="detail-button" disabled={actionDisabled || !timeSelected || result?.result === "待反馈"} onClick={() => run((payload) => api.recordResult(detail.id, round.id, "待反馈", payload))}>待反馈</button>
            </div>}
          </div>;
        })}
        <RemindersPanel api={api} caseId={detail.id} defaultTitle={`跟进 ${detail.candidate_name || detail.candidate_id} · ${detail.jd_title || detail.jd_id}`} refreshKey={detail} />
        <div className="section-heading"><h3>时间线</h3></div>
        {valid.map((event) => <div key={event.id} className="case-row">
          <div>
            <strong>{labels[event.event_type] || event.event_type}{event.round_name ? " · " + event.round_name : ""}</strong>
            {event.result && <small>{event.result}</small>}
            {event.note && <small>{event.note}</small>}
            <small>{new Date(event.occurred_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}（上海）</small>
          </div>
          <button className="detail-button" disabled={busy || !!attempt} aria-label={`作废${labels[event.event_type] || event.event_type} ${event.id}`} onClick={() => run((payload) => api.voidEvent(event.id, payload))}>作废</button>
        </div>)}
      </div>
    </aside>
  </div>;
}
