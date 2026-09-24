import { useState } from "react";
import { Button } from "../components/ui";

export interface CandidateReminderDialogProps {
  candidateName: string;
  busy?: boolean;
  onClose: () => void;
  /** 返回错误信息表示保存失败；返回 null 表示成功（由上层关闭弹窗）。 */
  onSave: (content: string) => Promise<string | null>;
}

/**
 * 建提醒：只让使用者写内容，人名自动带入。
 *
 * 提醒建好后出现在首页「今日待办」的「我的提醒」列，直到被勾选完成。
 */
export function CandidateReminderDialog(props: CandidateReminderDialogProps) {
  const { candidateName, busy, onClose, onSave } = props;
  const [content, setContent] = useState("");
  const [error, setError] = useState("");

  async function save() {
    if (!content.trim()) {
      setError("请填写提醒内容");
      return;
    }
    const message = await onSave(content.trim());
    if (message !== null) setError(message);
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" role="dialog" aria-label="建提醒" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <h2>给 {candidateName} 建提醒</h2>
          <div className="actions">
            <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭">×</button>
          </div>
        </div>

        <div className="form-grid" style={{ marginTop: 12 }}>
          <input
            className="full"
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder="提醒内容（如：下周一电话回访）"
            aria-label="提醒内容"
          />
        </div>
        {/* 放在 .actions 里而不是 form-grid 的第二列：否则按钮会被网格拉伸成半屏宽。 */}
        <div className="actions" style={{ marginTop: 12 }}>
          <Button variant="primary" disabled={busy} onClick={() => void save()}>
            {busy ? "保存中…" : "保存提醒"}
          </Button>
        </div>
        <p className="muted">建好的提醒会出现在首页「今日待办」的「我的提醒」列，直到你勾选完成。</p>
        {error && <p role="status" className="muted">{error}</p>}
      </div>
    </div>
  );
}
