import { useState } from "react";

export interface JdExactConstraint {
  kind: string;
  operator: string;
  alternatives: string[];
  strength: string;
  source: string;
  source_text: string;
}

interface JdProfileEditorProps {
  jdId: string;
  title: string;
  profile: string | null;
  constraints: JdExactConstraint[];
  onSave: (jdId: string, profile: string, constraints: JdExactConstraint[]) => Promise<void>;
  onRegenerate: (jdId: string, instruction: string) => Promise<string | null>;
  onParseConstraints: (sourceText: string) => Promise<JdExactConstraint[]>;
  onClose: () => void;
}

export function JdProfileEditor({
  jdId,
  title,
  profile,
  constraints: initialConstraints,
  onSave,
  onRegenerate,
  onParseConstraints,
  onClose,
}: JdProfileEditorProps) {
  const [draft, setDraft] = useState(profile ?? "");
  const [instruction, setInstruction] = useState("");
  const [constraints, setConstraints] = useState<JdExactConstraint[]>(initialConstraints);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 画像正文被改过：保存时会按新文字重解析硬条件，因此旧草稿不再展示、也不参与保存。 */
  const draftChanged = draft.trim() !== (profile ?? "").trim();

  async function save() {
    if (saving || regenerating) return;
    setError(null);
    // 画像一变就按最新文字重解析硬条件并覆盖（完全交给 AI）；文本没变则沿用现有硬条件，
    // 不做二次确认——保存与重解析在同一次点击里完成。
    setSaving(true);
    try {
      const next = draftChanged ? await onParseConstraints(draft) : constraints;
      await onSave(jdId, draft.trim() || "", next);
      onClose();
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
      const profile = await onRegenerate(jdId, instruction);
      if (profile != null) setDraft(profile);
      setInstruction("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像重新生成失败");
    } finally {
      setRegenerating(false);
    }
  }

  function setStrength(index: number, strength: string) {
    setConstraints((current) => current.map((c, i) => (i === index ? { ...c, strength } : c)));
  }

  function removeConstraint(index: number) {
    setConstraints((current) => current.filter((_, i) => i !== index));
  }

  return (
    <div className="profile-editor-overlay" role="dialog" aria-label="编辑候选人画像">
      <div className="profile-editor">
        <div className="profile-editor__head">
          <h3>候选人画像 · {title}</h3>
        </div>
        <textarea
          aria-label="画像正文"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={8}
          placeholder="候选人画像要求…"
        />
        <div className="sub-label">AI 重新生成（可选：输入新的描述或要求）</div>
        <textarea
          aria-label="重生成指令"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          rows={3}
          placeholder="例如：更看重 AI/LLM 落地经验…"
        />
        {constraints.length > 0 && !draftChanged && (
          <div className="constraint-preview" role="list" aria-label="硬条件草稿">
            <div className="sub-label">硬条件（保存后生效，可修改强度或删除）</div>
            {constraints.map((c, i) => (
              <div key={i} className="constraint-item">
                <select
                  aria-label="硬条件强度"
                  value={c.strength}
                  onChange={(e) => setStrength(i, e.target.value)}
                >
                  <option value="MUST">必须</option>
                  <option value="PLUS">优先</option>
                  <option value="EXCLUDE">排除</option>
                </select>
                <span>{c.alternatives.join(` ${c.operator === "OR" ? "或" : "且"} `)}</span>
                {c.source_text && <em className="muted">（{c.source_text}）</em>}
                <button type="button" className="btn btn-ghost btn-xs" aria-label="删除硬条件" onClick={() => removeConstraint(i)}>
                  删除
                </button>
              </div>
            ))}
          </div>
        )}
        {error && <p role="alert" className="profile-editor__error">{error}</p>}
        {draftChanged && !error && (
          <p className="profile-editor__hint">画像已修改：保存时会按新文字重新解析硬条件并覆盖</p>
        )}
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
