import { useState, type ReactNode } from "react";
import type { JdParsedData } from "../App";
import {
  BusinessDirectionPicker,
  CareerDirectionPicker,
} from "./DirectionPicker";

const DEGREE_OPTIONS = [
  { value: "DOCTORATE", label: "博士" },
  { value: "MASTER", label: "硕士" },
  { value: "BACHELOR", label: "本科" },
  { value: "ASSOCIATE", label: "大专" },
];

const AI_CATEGORY_OPTIONS = [
  { value: "CORE_AI", label: "核心 AI" },
  { value: "AI_RELATED", label: "AI 相关" },
  { value: "NON_AI", label: "非 AI" },
];

const REQUIREMENT_KIND_OPTIONS = [
  { value: "MUST", label: "必备" },
  { value: "PLUS", label: "加分" },
  { value: "EXCLUDE", label: "排除" },
];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value ?? null));
}

function str(value: unknown): string {
  return value == null ? "" : String(value);
}

function num(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

function parseNumber(value: string): number | null {
  if (!value.trim()) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function splitLines(value: string): string[] {
  return value.split("\n").map((s) => s.trim()).filter(Boolean);
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="parsed-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="parsed-section">
      <div className="parsed-section__title">{title}</div>
      <div className="parsed-section__body">{children}</div>
    </div>
  );
}

type JdRequirement = { kind: string; label: string; value: string };

function RequirementsEditor({
  value,
  onChange,
}: {
  value: JdRequirement[];
  onChange: (v: JdRequirement[]) => void;
}) {
  const list = value ?? [];
  function patch(i: number, p: Partial<JdRequirement>) {
    onChange(list.map((it, idx) => (idx === i ? { ...it, ...p } : it)));
  }
  return (
    <div className="parsed-list">
      {list.map((req, i) => (
        <div key={i} className="parsed-list__item">
          <div className="parsed-list__head">
            <span>要求 {i + 1}</span>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange(list.filter((_, idx) => idx !== i))}>删除</button>
          </div>
          <Field label="类型">
            <select value={str(req.kind)} onChange={(e) => patch(i, { kind: e.target.value })}>
              {REQUIREMENT_KIND_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </Field>
          <Field label="标签"><input value={str(req.label)} onChange={(e) => patch(i, { label: e.target.value })} /></Field>
          <Field label="内容"><input value={str(req.value)} onChange={(e) => patch(i, { value: e.target.value })} /></Field>
        </div>
      ))}
      <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange([...list, { kind: "MUST", label: "技能", value: "" }])}>+ 添加要求</button>
    </div>
  );
}

export interface JdParsedEditorProps {
  jdId: string;
  title: string;
  parsed: JdParsedData | null;
  onSave: (parsed: JdParsedData) => Promise<void>;
  onClose: () => void;
}

export function JdParsedEditor({
  jdId,
  title,
  parsed,
  onSave,
  onClose,
}: JdParsedEditorProps) {
  const [draft, setDraft] = useState<JdParsedData>(() => clone(parsed ?? {}));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function update(patch: Partial<JdParsedData>) {
    setDraft((d) => ({ ...d, ...patch }));
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(draft);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "解析数据保存失败");
      setSaving(false);
    }
  }

  return (
    <div className="parsed-editor-overlay" role="dialog" aria-label="JD 解析表">
      <div className="parsed-editor">
        <div className="parsed-editor__head">
          <h3>解析表 · {title || jdId}</h3>
          <button type="button" className="btn btn-ghost btn-xs" onClick={onClose}>关闭</button>
        </div>
        <div className="parsed-editor__body">
          <Section title="基本信息">
            <Field label="岗位名称"><input value={str(draft.title)} onChange={(e) => update({ title: e.target.value })} /></Field>
            <Field label="公司"><input value={str(draft.company)} onChange={(e) => update({ company: e.target.value })} /></Field>
            <Field label="部门"><input value={str(draft.department)} onChange={(e) => update({ department: e.target.value || null })} /></Field>
            <Field label="地点"><input value={str(draft.location)} onChange={(e) => update({ location: e.target.value || null })} /></Field>
            <Field label="薪资"><input value={str(draft.salary)} onChange={(e) => update({ salary: e.target.value || null })} /></Field>
            <Field label="行业"><input value={str(draft.industry)} onChange={(e) => update({ industry: e.target.value || null })} /></Field>
            <Field label="职业方向（大类 → 细分，最多 3 个大类 / 每个大类 2 个细分）">
              <CareerDirectionPicker
                ariaLabel="职业方向"
                maxDirections={3}
                directions={draft.career_directions ?? []}
                specializations={draft.career_specializations ?? []}
                onChange={(next) => update({
                  career_directions: next.directions,
                  career_specializations: next.specializations,
                })}
              />
            </Field>
            <Field label="业务方向（最多 2 个）">
              <BusinessDirectionPicker
                ariaLabel="业务方向"
                maxValues={2}
                values={draft.business_directions ?? []}
                onChange={(business_directions) => update({ business_directions })}
              />
            </Field>
            <Field label="AI 分类">
              <select value={str(draft.ai_category)} onChange={(e) => update({ ai_category: e.target.value || null })}>
                <option value="">—</option>
                {AI_CATEGORY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </Field>
          </Section>

          <Section title="年限与学历">
            <Field label="最低工作年限"><input value={num(draft.min_years)} onChange={(e) => update({ min_years: parseNumber(e.target.value) })} /></Field>
            <Field label="最低学历">
              <select value={str(draft.highest_degree)} onChange={(e) => update({ highest_degree: e.target.value || null })}>
                <option value="">—</option>
                {DEGREE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </Field>
            <Field label="QS等级"><input value={str(draft.qs_level)} onChange={(e) => update({ qs_level: e.target.value || null })} /></Field>
          </Section>

          <Section title="技能与职责（每行一个）">
            <Field label="必备技能">
              <textarea rows={4} value={(draft.required_skills ?? []).join("\n")} onChange={(e) => update({ required_skills: splitLines(e.target.value) })} />
            </Field>
            <Field label="加分技能">
              <textarea rows={3} value={(draft.plus_skills ?? []).join("\n")} onChange={(e) => update({ plus_skills: splitLines(e.target.value) })} />
            </Field>
            <Field label="核心职责">
              <textarea rows={4} value={(draft.core_duties ?? []).join("\n")} onChange={(e) => update({ core_duties: splitLines(e.target.value) })} />
            </Field>
            <Field label="加分行业">
              <textarea rows={2} value={(draft.plus_industry ?? []).join("\n")} onChange={(e) => update({ plus_industry: splitLines(e.target.value) })} />
            </Field>
            <Field label="加分项目类型">
              <textarea rows={2} value={(draft.plus_project_types ?? []).join("\n")} onChange={(e) => update({ plus_project_types: splitLines(e.target.value) })} />
            </Field>
          </Section>

          <Section title="摘要与画像">
            <Field label="岗位摘要">
              <textarea rows={3} value={str(draft.summary)} onChange={(e) => update({ summary: e.target.value })} />
            </Field>
            <Field label="候选人画像要求">
              <textarea rows={4} value={str(draft.candidate_profile)} onChange={(e) => update({ candidate_profile: e.target.value || null })} />
            </Field>
          </Section>

          <Section title="岗位要求">
            <RequirementsEditor value={draft.requirements ?? []} onChange={(v) => update({ requirements: v })} />
          </Section>
        </div>
        {error && <p role="alert" className="parsed-editor__error">{error}</p>}
        <div className="parsed-editor__actions">
          <button type="button" className="btn btn-primary btn-xs" disabled={saving} onClick={() => void save()}>
            {saving ? "保存中…" : "保存"}
          </button>
          <button type="button" className="btn btn-ghost btn-xs" disabled={saving} onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  );
}
