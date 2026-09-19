import { useEffect, useRef, useState } from "react";
import type { IndexSyncStatus, RecruitmentApi } from "../App";

export function IndexSyncPanel({ api }: { api: RecruitmentApi }) {
  const [status, setStatus] = useState<IndexSyncStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function run(retry = false) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setStatus(retry ? await api.retryIndexSync() : await api.indexStatus());
      if (retry) setNotice("已请求重试，等待后台同步完成。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "读取索引同步状态失败");
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  useEffect(() => { void run(); }, [api]);

  return <section className="section" aria-label="索引同步">
    <div className="section-head">
      <h2>索引同步</h2>
      <div className="actions">
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void run()}>刷新同步状态</button>
        <button type="button" className="btn btn-primary" disabled={busy || !status?.failed} onClick={() => void run(true)}>重试失败同步</button>
      </div>
    </div>
    <p className="muted">资料变更会逐项同步到搜索索引。重试仅重新调度现有未同步任务，不执行全库重建。</p>
    {status && <p role="status" className="muted">等待同步 {status.pending} 项，失败 {status.failed} 项</p>}
    {status?.rebuild && !status.rebuild.finished_at && <p role="status" className="muted">
      正在重建索引（已完成 {Math.max(status.rebuild.total - status.pending, 0)}/{status.rebuild.total} 条）：
      {status.rebuild.mode === "reset" ? "向量口径已更换，旧索引已归档" : "已就地补齐索引结构"}（{status.rebuild.reason}）
    </p>}
    {status?.rebuild?.finished_at && <p role="status" className="muted">
      上次重建已完成：{status.rebuild.reason}
    </p>}
    {status?.indexes?.map((index) => <div key={index.entity_type}>
      <p className="muted">{index.entity_type === "candidate" ? "候选人" : index.entity_type === "jd" ? "岗位" : index.entity_type}索引：{index.compatible ? "版本兼容" : "版本不兼容"}</p>
      {index.error && <p className="muted">{index.error}</p>}
    </div>)}
    {status?.indexes?.some((index) => !index.compatible) && <p role="alert" className="muted">索引与当前版本不兼容：退出并重新启动应用，启动时会自动重建。</p>}
    {notice && <p role="status" className="muted">{notice}</p>}
    {error && <p role="alert" className="muted">{error}</p>}
    {status && status.items.length > 0 && <table><thead><tr><th>对象</th><th>同步状态</th><th>尝试次数</th><th>错误</th></tr></thead><tbody>
      {status.items.map((item) => <tr key={item.entity_type + ":" + item.entity_id}>
        <td>{item.entity_type === "candidate" ? "候选人" : item.entity_type === "jd" ? "岗位" : item.entity_type} · {item.entity_id}</td>
        <td>{item.status === "RETRY_WAIT" ? "等待重试" : item.status === "PENDING" ? "等待同步" : item.status}</td>
        <td>{item.attempts}</td><td>{item.error || "—"}</td>
      </tr>)}
    </tbody></table>}
  </section>;
}
