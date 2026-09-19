import { useState } from "react";

interface CandidateProfileEditorProps {
  candidateId: string;
  summary: string | null;
  source: string | null;
  onSave: (summary: string) => Promise<void>;
  onRegenerate: (instruction: string) => Promise<string | null>;
  onClose: () => void;
}

function sourceLabel(source: string | null): string {
  if (source === "manual") return "人工编辑";
  if (source === "ai") return "AI 生成";
  return "未生成";
}

function sourceTone(source: string | null): string {
  if (source === "manual") return "manual";
  return "ai";
}

export function CandidateProfileEditor({
  candidateId,
  summary,
  source,
  onSave,
  onRegenerate,
  onClose,
}: CandidateProfileEditorProps) {
  const [draft, setDraft] = useState(summary ?? "");
  const [instruction, setInstruction] = useState("");
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (saving || regenerating) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft.trim());
      // 成功由父组件关闭弹窗。
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像保存失败");
      setSaving(false);
    }
  }

  async function regenerate() {
    if (saving || regenerating) return;
    setRegenerating(true);
    setError(null);
    try {
      const summary = await onRegenerate(instruction);
      if (summary != null) setDraft(summary);
      setInstruction("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像重新生成失败");
    } finally {
      setRegenerating(false);
    }
  }

  return (
    <div className="profile-editor-overlay" role="dialog" aria-label="编辑 AI 画像">
      <div className="profile-editor">
        <div className="profile-editor__head">
          <h3>AI 画像</h3>
          <span className={`profile-badge profile-badge--${sourceTone(source)}`}>
            {sourceLabel(source)}
          </span>
        </div>
        <textarea
          aria-label="画像正文"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={8}
          placeholder="候选人画像摘要…"
        />
        <div className="sub-label">AI 重新生成（可选：输入新的描述或要求）</div>
        <textarea
          aria-label="重生成指令"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          rows={3}
          placeholder="例如：突出 AI/LLM 落地经验，适合金融科技方向…"
        />
        {error && <p role="alert" className="profile-editor__error">{error}</p>}
        <div className="profile-editor__actions">
          <button type="button" className="btn btn-ghost btn-xs" disabled={saving || regenerating} onClick={() => void regenerate()}>
            {regenerating ? "处理中…" : "重新生成"}
          </button>
          <button type="button" className="btn btn-primary btn-xs" disabled={saving || regenerating} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </button>
          <button type="button" className="btn btn-ghost btn-xs" disabled={saving || regenerating} onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
