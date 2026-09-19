import { useEffect, useRef, useState } from "react";
import type { RecruitmentApi, ReminderItem } from "../App";

export function RemindersPanel({ api, caseId, defaultTitle = "", onOpenCase, refreshKey }: {
  api: RecruitmentApi; caseId?: string; defaultTitle?: string;
  onOpenCase?: (caseId: string) => void; refreshKey?: unknown;
}) {
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [title, setTitle] = useState(defaultTitle);
  const [remindAt, setRemindAt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  async function run(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try { await action(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "提醒操作失败"); }
    finally { lock.current = false; setBusy(false); }
  }
  const refresh = () => run(async () => setReminders(await api.listReminders()));
  useEffect(() => { void refresh(); }, [api, caseId, refreshKey]);
  const visible = reminders.filter((item) => !item.dismissed && (!caseId || item.case_id === caseId));
  const heading = caseId ? "本流程跟进提醒" : "提醒管理";
  return <section className="section workflow-reminders" aria-label={heading}>
    <div className="section-head">
      <h2>{heading}</h2>
      <div className="actions">
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void refresh()}>刷新提醒</button>
      </div>
    </div>
    <form onSubmit={(event) => {
      event.preventDefault();
      if (!title.trim() || !remindAt) return;
      void run(async () => {
        const created = await api.createReminder({
          title: title.trim(), case_id: caseId,
          remind_at: remindAt + (remindAt.length === 16 ? ":00+08:00" : "+08:00"),
        });
        setReminders((items) => [created, ...items.filter((item) => item.id !== created.id)]);
        setTitle(defaultTitle);
        setRemindAt("");
      });
    }}>
      <div className="form-grid" style={{ marginTop: 10 }}>
        <label style={{ display: "block" }}>提醒内容<input className="ui-input" required aria-label="提醒内容" value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} /></label>
        <label style={{ display: "block" }}>提醒时间（上海）<input className="ui-input" required type="datetime-local" aria-label="提醒时间（上海）" value={remindAt} disabled={busy} onChange={(event) => setRemindAt(event.target.value)} /></label>
      </div>
      <button type="submit" className="btn btn-primary" style={{ marginTop: 12 }} disabled={busy || !title.trim() || !remindAt}>添加提醒</button>
      {!caseId && <small className="muted">此处创建独立提醒；关联流程的提醒可在招聘流程中添加。</small>}
    </form>
    {error && <p role="alert">{error}</p>}
    {visible.length === 0 ? <p className="muted">暂无待处理提醒</p> : <div className="row-list">{visible.map((item) => <div className="row-item" key={item.id}>
      <div><strong>{item.title}</strong>
        <small>{new Date(item.remind_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })}（上海）</small>
        {item.note && <small>{item.note}</small>}
        {item.paused_by_workflow && <small>已暂停：候选人、岗位或流程状态暂不允许跟进；状态恢复后自动继续。</small>}
        {item.case_id && !onOpenCase && <small>关联本流程</small>}
      </div>
      <div className="actions">
        {item.case_id && onOpenCase && <button type="button" className="btn btn-secondary btn-xs" disabled={busy} onClick={() => onOpenCase(item.case_id!)}>查看关联流程</button>}
        <button type="button" className="btn btn-secondary btn-xs" disabled={busy} onClick={() => void run(async () => {
          await api.dismissReminder(item.id);
          setReminders((items) => items.filter((entry) => entry.id !== item.id));
        })}>完成</button>
      </div>
    </div>)}</div>}
  </section>;
}

