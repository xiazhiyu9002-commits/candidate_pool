import { useRef, useState } from "react";
import type { RecruitmentApi, ResumeReview } from "../App";

export function ResumeReviewDrawer({ api, initialReview, onClose, onReparsed, onReparse, onForceReparse }: {
  api: RecruitmentApi; initialReview: ResumeReview; onClose: () => void; onReparsed?: () => void;
  /** 常规重新解析（异常页才 OCR）；强制 OCR 是扫描件/乱码专用。 */
  onReparse?: (revisionId: string) => Promise<void>;
  onForceReparse?: (revisionId: string) => Promise<void>;
}) {
  const [review, setReview] = useState(initialReview);
  const [reparsing, setReparsing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lock = useRef(false);

  async function reparse(run: (revisionId: string) => Promise<void> | undefined) {
    if (lock.current) return;
    lock.current = true;
    setReparsing(true);
    setError(null);
    try {
      await run(review.revision_id);
      const updated = await api.getResumeReview(review.revision_id);
      setReview(updated);
      onReparsed?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重新解析失败，请重试。");
    } finally { lock.current = false; setReparsing(false); }
  }

  return <div className="match-drawer-backdrop" onClick={onClose}>
    <aside className="match-drawer workflow-drawer" role="dialog" aria-modal="true" aria-label="简历解析详情" onClick={(event) => event.stopPropagation()}>
      <div className="match-drawer-header"><div><h2>简历解析详情</h2><small>{review.status === "READY" ? "已入库" : "解析失败"} · {review.revision_id}</small></div><div className="case-actions">{onReparse && <button className="detail-button" disabled={reparsing} onClick={() => void reparse(onReparse)}>{reparsing ? "解析中…" : "重新解析"}</button>}{onForceReparse && <button className="detail-button" disabled={reparsing} title="整份走 OCR，适合扫描件或文本层乱码的简历" onClick={() => void reparse(onForceReparse)}>强制 OCR</button>}<button className="detail-button" onClick={onClose}>关闭</button></div></div>
      <div className="match-drawer-body">
        {review.error_message && <p className="review-notice">{review.error_message}</p>}
        {error && <p className="review-notice review-notice--error" role="alert">{error}</p>}
        <details open><summary>原始简历正文</summary><pre style={{ whiteSpace: "pre-wrap" }}>{review.raw_text || "暂无可用正文"}</pre></details>
        {review.parsed_data && <details><summary>已解析字段</summary><pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(review.parsed_data, null, 2)}</pre></details>}
        <details><summary>高级诊断</summary>
          <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ parsed_data: review.parsed_data, review_data: review.review_data, extraction_diagnostics: review.extraction_diagnostics }, null, 2)}</pre>
        </details>
      </div>
    </aside>
  </div>;
}
