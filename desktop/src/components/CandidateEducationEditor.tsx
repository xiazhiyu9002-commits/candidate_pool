import { useState } from "react";
import type { ParsedEducationData } from "../App";

interface CandidateEducationEditorProps {
  educations: ParsedEducationData[];
  onSave: (educations: ParsedEducationData[]) => Promise<void>;
  onClose: () => void;
}

function blank(): ParsedEducationData {
  return { school: null, degree: null, major: null, graduation_year: null };
}

// 后端将学历归一化为英文规范值（ASSOCIATE/BACHELOR/MASTER/DOCTORATE），
// 编辑器的下拉选项使用中文，需先映射回中文再展示。
const DEGREE_LABEL: Record<string, string> = {
  ASSOCIATE: "大专", 大专: "大专",
  BACHELOR: "本科", 本科: "本科",
  MASTER: "硕士", 硕士: "硕士",
  DOCTORATE: "博士", 博士: "博士",
};

export function CandidateEducationEditor({
  educations,
  onSave,
  onClose,
}: CandidateEducationEditorProps) {
  const [draft, setDraft] = useState<ParsedEducationData[]>(() => {
    const items = educations.length
      ? [...educations].sort((a, b) => (b.graduation_year ?? -1) - (a.graduation_year ?? -1))
      : [blank()];
    return items.map((e) => ({ ...e }));
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function patch(index: number, field: keyof ParsedEducationData, value: unknown) {
    setDraft((prev) =>
      prev.map((edu, i) => {
        if (i !== index) return edu;
        const next: ParsedEducationData = { ...edu, [field]: value };
        // 学校名变化后，旧的 school_tags / qs_rank / qs_year 不再可信，交给后端重算。
        if (field === "school") {
          delete next.school_tags;
          delete next.qs_rank;
          delete next.qs_year;
        }
        return next;
      }),
    );
  }

  function remove(index: number) {
    setDraft((prev) => prev.filter((_, i) => i !== index));
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(
        draft.filter((e) => e.school || e.degree || e.major || e.graduation_year),
      );
      // 成功时由父组件关闭弹窗，这里不再重复关闭。
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "教育经历保存失败");
      setSaving(false);
    }
  }

  return (
    <div className="edu-editor-overlay" role="dialog" aria-label="编辑教育经历">
      <div className="edu-editor">
        <h3>教育经历</h3>
        {draft.map((edu, index) => (
          <div className="edu-row" key={index}>
            <input
              aria-label={`学校-${index}`}
              placeholder="学校"
              value={edu.school ?? ""}
              onChange={(e) => patch(index, "school", e.target.value.trim() || null)}
            />
            <select
              aria-label={`学历-${index}`}
              value={edu.degree ? (DEGREE_LABEL[edu.degree] ?? edu.degree) : ""}
              onChange={(e) => patch(index, "degree", e.target.value || null)}
            >
              <option value="">学历</option>
              <option value="本科">本科</option>
              <option value="硕士">硕士</option>
              <option value="博士">博士</option>
              <option value="大专">大专</option>
            </select>
            <input
              aria-label={`专业-${index}`}
              placeholder="专业"
              value={edu.major ?? ""}
              onChange={(e) => patch(index, "major", e.target.value.trim() || null)}
            />
            <input
              aria-label={`毕业年份-${index}`}
              placeholder="毕业年份"
              type="number"
              min={1950}
              max={2100}
              value={edu.graduation_year ?? ""}
              onChange={(e) =>
                patch(index, "graduation_year", e.target.value ? Number(e.target.value) : null)
              }
            />
            <button type="button" className="detail-button danger" disabled={saving} onClick={() => remove(index)}>
              删除
            </button>
          </div>
        ))}
        {error && <p role="alert" className="edu-error">{error}</p>}
        <div className="edu-actions">
          <button type="button" className="detail-button" disabled={saving} onClick={() => setDraft((p) => [...p, blank()])}>
            新增一段
          </button>
          <button
            type="button"
            className="btn btn-primary btn-xs"
            disabled={saving}
            onClick={() => void save()}
          >
            {saving ? "保存中…" : "保存"}
          </button>
          <button type="button" className="btn btn-ghost btn-xs" disabled={saving} onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
