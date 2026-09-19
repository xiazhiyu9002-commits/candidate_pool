import { useState, type ReactNode } from "react";
import type {
  ParsedEducationData,
  ParsedExperienceData,
  ParsedProjectData,
  ParsedResumeData,
} from "../App";
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

function ExperiencesEditor({
  value,
  onChange,
}: {
  value: ParsedExperienceData[];
  onChange: (v: ParsedExperienceData[]) => void;
}) {
  const list = value ?? [];
  function patch(i: number, p: Partial<ParsedExperienceData>) {
    onChange(list.map((it, idx) => (idx === i ? { ...it, ...p } : it)));
  }
  return (
    <div className="parsed-list">
      {list.map((exp, i) => (
        <div key={i} className="parsed-list__item">
          <div className="parsed-list__head">
            <span>工作经历 {i + 1}</span>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange(list.filter((_, idx) => idx !== i))}>删除</button>
          </div>
          <Field label="公司"><input value={str(exp.company)} onChange={(e) => patch(i, { company: e.target.value || null })} /></Field>
          <Field label="职位"><input value={str(exp.title)} onChange={(e) => patch(i, { title: e.target.value || null })} /></Field>
          <Field label="开始时间"><input value={str(exp.start_date)} onChange={(e) => patch(i, { start_date: e.target.value || null })} /></Field>
          <Field label="结束时间"><input value={str(exp.end_date)} onChange={(e) => patch(i, { end_date: e.target.value || null })} /></Field>
          <Field label="行业"><input value={str(exp.industry)} onChange={(e) => patch(i, { industry: e.target.value || null })} /></Field>
          <Field label="职责"><textarea rows={2} value={str(exp.summary)} onChange={(e) => patch(i, { summary: e.target.value })} /></Field>
        </div>
      ))}
      <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange([...list, { company: null, title: null, summary: "" }])}>+ 添加工作经历</button>
    </div>
  );
}

function ProjectsEditor({
  value,
  onChange,
}: {
  value: ParsedProjectData[];
  onChange: (v: ParsedProjectData[]) => void;
}) {
  const list = value ?? [];
  function patch(i: number, p: Partial<ParsedProjectData>) {
    onChange(list.map((it, idx) => (idx === i ? { ...it, ...p } : it)));
  }
  return (
    <div className="parsed-list">
      {list.map((proj, i) => (
        <div key={i} className="parsed-list__item">
          <div className="parsed-list__head">
            <span>项目经历 {i + 1}</span>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange(list.filter((_, idx) => idx !== i))}>删除</button>
          </div>
          <Field label="项目名"><input value={str(proj.name)} onChange={(e) => patch(i, { name: e.target.value || null })} /></Field>
          <Field label="技术栈"><input value={str(proj.tech_stack)} onChange={(e) => patch(i, { tech_stack: e.target.value || null })} /></Field>
          <Field label="业务场景"><input value={str(proj.business_scene)} onChange={(e) => patch(i, { business_scene: e.target.value || null })} /></Field>
          <Field label="描述"><textarea rows={2} value={str(proj.summary)} onChange={(e) => patch(i, { summary: e.target.value })} /></Field>
        </div>
      ))}
      <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange([...list, { name: null, summary: "" }])}>+ 添加项目经历</button>
    </div>
  );
}

function EducationsEditor({
  value,
  onChange,
}: {
  value: ParsedEducationData[];
  onChange: (v: ParsedEducationData[]) => void;
}) {
  const list = value ?? [];
  function patch(i: number, p: Partial<ParsedEducationData>) {
    onChange(list.map((it, idx) => (idx === i ? { ...it, ...p } : it)));
  }
  return (
    <div className="parsed-list">
      {list.map((edu, i) => (
        <div key={i} className="parsed-list__item">
          <div className="parsed-list__head">
            <span>教育经历 {i + 1}</span>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange(list.filter((_, idx) => idx !== i))}>删除</button>
          </div>
          <Field label="学校"><input value={str(edu.school)} onChange={(e) => patch(i, { school: e.target.value || null })} /></Field>
          <Field label="学历">
            <select value={str(edu.degree)} onChange={(e) => patch(i, { degree: e.target.value || null })}>
              <option value="">—</option>
              {DEGREE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </Field>
          <Field label="专业"><input value={str(edu.major)} onChange={(e) => patch(i, { major: e.target.value || null })} /></Field>
          <Field label="毕业年份"><input value={num(edu.graduation_year)} onChange={(e) => patch(i, { graduation_year: parseNumber(e.target.value) })} /></Field>
          <Field label="国家/地区"><input value={str(edu.country_region)} onChange={(e) => patch(i, { country_region: e.target.value || null })} /></Field>
          <Field label="学校标签"><input value={(edu.school_tags ?? []).join("、")} onChange={(e) => patch(i, { school_tags: splitLines(e.target.value.replace(/、/g, "\n")) })} /></Field>
          <Field label="QS排名"><input value={num(edu.qs_rank)} onChange={(e) => patch(i, { qs_rank: parseNumber(e.target.value) })} /></Field>
        </div>
      ))}
      <button type="button" className="btn btn-ghost btn-xs" onClick={() => onChange([...list, {}])}>+ 添加教育经历</button>
    </div>
  );
}

export interface CandidateParsedEditorProps {
  candidateId: string;
  parsed: ParsedResumeData | null;
  onSave: (parsed: ParsedResumeData) => Promise<void>;
  onClose: () => void;
}

export function CandidateParsedEditor({
  candidateId,
  parsed,
  onSave,
  onClose,
}: CandidateParsedEditorProps) {
  const [draft, setDraft] = useState<ParsedResumeData>(() => clone(parsed ?? {}));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function update(patch: Partial<ParsedResumeData>) {
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
    <div className="parsed-editor-overlay" role="dialog" aria-label="候选人解析表">
      <div className="parsed-editor">
        <div className="parsed-editor__head">
          <h3>解析表 · {draft.name || candidateId}</h3>
          <button type="button" className="btn btn-ghost btn-xs" onClick={onClose}>关闭</button>
        </div>
        <div className="parsed-editor__body">
          <Section title="基本信息">
            <Field label="姓名"><input value={str(draft.name)} onChange={(e) => update({ name: e.target.value || null })} /></Field>
            <Field label="性别">
              <select value={str(draft.gender)} onChange={(e) => update({ gender: e.target.value || null })}>
                <option value="">—</option>
                <option value="男">男</option>
                <option value="女">女</option>
              </select>
            </Field>
            <Field label="年龄"><input value={num(draft.age)} onChange={(e) => update({ age: parseNumber(e.target.value) })} /></Field>
            <Field label="出生年份"><input value={num(draft.birth_year)} onChange={(e) => update({ birth_year: parseNumber(e.target.value) })} /></Field>
            <Field label="工作年限"><input value={num(draft.total_years)} onChange={(e) => update({ total_years: parseNumber(e.target.value) })} /></Field>
            <Field label="职级"><input value={str(draft.job_level)} onChange={(e) => update({ job_level: e.target.value || null })} /></Field>
            <Field label="薪资"><input value={str(draft.salary)} onChange={(e) => update({ salary: e.target.value || null })} /></Field>
            <Field label="职业方向（大类 → 细分，最多 2 个大类 / 每个大类 2 个细分）">
              <CareerDirectionPicker
                ariaLabel="职业方向"
                maxDirections={2}
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
            <Field label="所在城市"><input value={str(draft.location)} onChange={(e) => update({ location: e.target.value || null })} /></Field>
            <Field label="意向城市"><input value={str(draft.preferred_location)} onChange={(e) => update({ preferred_location: e.target.value || null })} /></Field>
            <Field label="当前公司"><input value={str(draft.current_company)} onChange={(e) => update({ current_company: e.target.value || null })} /></Field>
            <Field label="当前职位"><input value={str(draft.current_title)} onChange={(e) => update({ current_title: e.target.value || null })} /></Field>
          </Section>

          <Section title="学历">
            <Field label="最高学历">
              <select value={str(draft.highest_degree)} onChange={(e) => update({ highest_degree: e.target.value || null })}>
                <option value="">—</option>
                {DEGREE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </Field>
            <Field label="学校"><input value={str(draft.school)} onChange={(e) => update({ school: e.target.value || null })} /></Field>
            <Field label="学校等级"><input value={str(draft.school_level)} onChange={(e) => update({ school_level: e.target.value || null })} /></Field>
            <Field label="学校档位"><input value={str(draft.school_tier)} onChange={(e) => update({ school_tier: e.target.value || null })} /></Field>
            <Field label="QS排名"><input value={num(draft.qs_rank)} onChange={(e) => update({ qs_rank: parseNumber(e.target.value) })} /></Field>
            <Field label="毕业年份"><input value={num(draft.graduation_year)} onChange={(e) => update({ graduation_year: parseNumber(e.target.value) })} /></Field>
          </Section>

          <Section title="行业">
            <Field label="行业"><input value={str(draft.industry)} onChange={(e) => update({ industry: e.target.value || null })} /></Field>
            <Field label="当前行业"><input value={str(draft.current_industry)} onChange={(e) => update({ current_industry: e.target.value || null })} /></Field>
            <Field label="最长行业"><input value={str(draft.longest_industry)} onChange={(e) => update({ longest_industry: e.target.value || null })} /></Field>
          </Section>

          <Section title="技能">
            <Field label="技能（每行一个）">
              <textarea rows={5} value={(draft.skills ?? []).join("\n")} onChange={(e) => update({ skills: splitLines(e.target.value) })} />
            </Field>
            <Field label="意向城市列表（每行一个）">
              <textarea rows={2} value={(draft.preferred_locations ?? []).join("\n")} onChange={(e) => update({ preferred_locations: splitLines(e.target.value) })} />
            </Field>
          </Section>

          <Section title="摘要">
            <Field label="个人摘要">
              <textarea rows={4} value={str(draft.summary)} onChange={(e) => update({ summary: e.target.value })} />
            </Field>
          </Section>

          <Section title="工作经历">
            <ExperiencesEditor value={draft.experiences ?? []} onChange={(v) => update({ experiences: v })} />
          </Section>

          <Section title="项目经历">
            <ProjectsEditor value={draft.projects ?? []} onChange={(v) => update({ projects: v })} />
          </Section>

          <Section title="教育经历">
            <EducationsEditor value={draft.educations ?? []} onChange={(v) => update({ educations: v })} />
          </Section>

          {draft.ai_profile_summary && (
            <Section title="AI 画像（只读，请用画像编辑器修改）">
              {draft.ai_profile_summary.split(/\n+/).map((s) => s.trim()).filter(Boolean).map((point, i) => (
                <p key={i} className="parsed-readonly">{point}</p>
              ))}
            </Section>
          )}
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
