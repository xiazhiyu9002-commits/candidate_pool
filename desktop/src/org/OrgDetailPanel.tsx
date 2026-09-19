import { useEffect, useRef, useState } from "react";
import type {
  BindEmployeeResult,
  OrgDepartment,
  OrgEmployee,
  OrgTreeNode,
  UpdateOrgDepartmentInput,
  UpdateOrgEmployeeInput,
} from "../App";


function LockIcon() {
  return (
    <svg className="org-lock" viewBox="0 0 24 24" width="12" height="12" aria-label="敏感字段">
      <rect x="4" y="10" width="16" height="10" rx="2" fill="currentColor" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}


function Field({
  label,
  value,
  sensitive,
  onChange,
}: {
  label: string;
  value: string | number | null;
  sensitive?: boolean;
  onChange: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => setDraft(value ?? ""), [value]);

  function handle(next: string) {
    setDraft(next);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => onChange(next), 2000);
  }

  return (
    <label className="org-field">
      <span className="org-field-label">
        {label}
        {sensitive && <LockIcon />}
      </span>
      <input value={draft} onChange={(e) => handle(e.target.value)} />
    </label>
  );
}


interface OrgDetailPanelProps {
  node: OrgTreeNode | null;
  employee: OrgEmployee | null;
  department: OrgDepartment | null;
  employees: OrgEmployee[];
  departments: OrgDepartment[];
  onUpdateEmployee: (id: string, changes: UpdateOrgEmployeeInput) => void;
  onUpdateDepartment: (id: string, changes: UpdateOrgDepartmentInput) => void;
  onDelete: (node: OrgTreeNode) => void;
  /** 在公司根节点下新增顶层部门（从公司开始加部门，不必先去脑图里选中公司）。 */
  onAddChild?: (node: OrgTreeNode) => void;
  onBindEmployee: (employeeId: string, phone: string, name: string) => Promise<BindEmployeeResult>;
  onPreviewResume?: (revisionId: string, name: string) => void;
  onCollapse?: () => void;
}


export function OrgDetailPanel({
  node,
  employee,
  department,
  employees,
  departments,
  onUpdateEmployee,
  onUpdateDepartment,
  onDelete,
  onAddChild,
  onBindEmployee,
  onPreviewResume,
  onCollapse,
}: OrgDetailPanelProps) {
  const [bindPhone, setBindPhone] = useState("");
  const [bindBusy, setBindBusy] = useState(false);
  const [bindResult, setBindResult] = useState<BindEmployeeResult | null>(null);

  useEffect(() => {
    setBindPhone("");
    setBindResult(null);
  }, [employee?.id]);

  if (!node) {
    return (
      <div className="org-detail org-detail--empty">
        {onCollapse && (
          <button className="org-collapse" onClick={onCollapse} title="收起详情">»</button>
        )}
        <span>点击节点查看并编辑详情</span>
      </div>
    );
  }

  return (
    <div className="org-detail">
      <div className="org-detail-head">
        <h3>{node.kind === "employee" ? "人员" : node.kind === "department" ? "部门" : "公司"}</h3>
        <div className="org-detail-actions">
          {onCollapse && (
            <button className="org-collapse" onClick={onCollapse} title="收起详情">»</button>
          )}
          {node.kind !== "company" && (
            <button className="detail-button danger" onClick={() => onDelete(node)}>删除</button>
          )}
        </div>
      </div>

      {node.kind === "company" && (
        <div className="org-field-group">
          <p className="muted">当前公司：{node.name}</p>
          {onAddChild && (
            <button type="button" className="btn btn-secondary btn-xs" onClick={() => onAddChild(node)}>
              ＋部门
            </button>
          )}
        </div>
      )}

      {node.kind === "employee" && employee && (
        <div className="org-field-group">
          <h4>基本信息</h4>
          <Field label="姓名" value={employee.name} onChange={(v) => onUpdateEmployee(employee.id, { name: v.trim() || undefined })} />
          <Field label="岗位 Title" value={employee.title} onChange={(v) => onUpdateEmployee(employee.id, { title: v.trim() || null })} />
          <Field label="内部职级" value={employee.job_level} onChange={(v) => onUpdateEmployee(employee.id, { job_level: v.trim() || null })} />
          <label className="org-field">
            <span className="org-field-label">所属部门</span>
            <select
              value={employee.department_id ?? ""}
              onChange={(e) => onUpdateEmployee(employee.id, { department_id: e.target.value || null })}
            >
              <option value="">未分配部门</option>
              {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </label>
          <label className="org-field">
            <span className="org-field-label">直接汇报人</span>
            <select
              value={employee.report_to ?? ""}
              onChange={(e) => onUpdateEmployee(employee.id, { report_to: e.target.value || null })}
            >
              <option value="">无汇报人</option>
              {employees.filter((e) => e.id !== employee.id).map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </label>
          <label className="org-field">
            <span className="org-field-label">人员状态</span>
            <select value={employee.status ?? ""} onChange={(e) => onUpdateEmployee(employee.id, { status: e.target.value || null })}>
              <option value="">未设置</option>
              <option value="在职">在职</option>
              <option value="离职">离职</option>
              <option value="待入职">待入职</option>
              <option value="离职流程中">离职流程中</option>
            </select>
          </label>
          <label className="org-field">
            <span className="org-field-label">核心/关键岗位</span>
            <input
              type="checkbox"
              checked={employee.is_key}
              onChange={(e) => onUpdateEmployee(employee.id, { is_key: e.target.checked })}
            />
          </label>

          <h4>敏感信息</h4>
          <Field label="跳槽意向" value={employee.intention} sensitive onChange={(v) => onUpdateEmployee(employee.id, { intention: v.trim() || null })} />
          <Field label="联系方式" value={employee.contact} sensitive onChange={(v) => onUpdateEmployee(employee.id, { contact: v.trim() || null })} />
          <Field label="备注" value={employee.remark} sensitive onChange={(v) => onUpdateEmployee(employee.id, { remark: v.trim() || null })} />

          <h4>绑定人才库</h4>
          {employee.candidate_id && employee.candidate_name ? (
            <div role="status">
              <p className="muted">已绑定候选人：{employee.candidate_name}</p>
              <div className="case-actions">
                {employee.current_revision_id && onPreviewResume && (
                  <button
                    type="button"
                    className="detail-button"
                    onClick={() => onPreviewResume(employee.current_revision_id as string, employee.candidate_name ?? "")}
                  >
                    预览简历
                  </button>
                )}
              </div>
            </div>
          ) : (
            <p className="muted">未绑定候选人</p>
          )}
          <div className="jd-row">
            <input
              value={bindPhone}
              onChange={(e) => setBindPhone(e.target.value)}
              placeholder="电话（必填）"
              aria-label="绑定电话"
            />
            <button
              type="button"
              className="detail-button"
              disabled={bindBusy || !bindPhone.trim()}
              onClick={() => {
                setBindBusy(true);
                void onBindEmployee(employee.id, bindPhone.trim(), employee.name)
                  .then((result) => setBindResult(result))
                  .finally(() => setBindBusy(false));
              }}
            >
              {bindBusy ? "绑定中…" : "绑定"}
            </button>
          </div>
          {bindResult && (
            <div role="status">
              <p className="muted">
                {bindResult.matched
                  ? `已绑定：${bindResult.candidate_name ?? ""}${bindResult.name_mismatch ? "（姓名不一致，以电话为准）" : ""}`
                  : "未匹配到人才库候选人，请先录入人才库"}
              </p>
              {bindResult.matched && (
                <div className="case-actions">
                  {bindResult.current_revision_id && onPreviewResume && (
                    <button
                      type="button"
                      className="detail-button"
                      onClick={() => onPreviewResume(bindResult.current_revision_id as string, bindResult.candidate_name ?? "")}
                    >
                      预览简历
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {node.kind === "department" && department && (
        <div className="org-field-group">
          <h4>部门信息</h4>
          <Field label="名称" value={department.name} onChange={(v) => onUpdateDepartment(department.id, { name: v.trim() || undefined })} />
          <Field label="办公地" value={department.office_location} onChange={(v) => onUpdateDepartment(department.id, { office_location: v.trim() || null })} />
          <label className="org-field">
            <span className="org-field-label">小组负责人</span>
            <select
              value={department.leader_id ?? ""}
              onChange={(e) => onUpdateDepartment(department.id, { leader_id: e.target.value || null })}
            >
              <option value="">未设置</option>
              {employees.map((emp) => <option key={emp.id} value={emp.id}>{emp.name}</option>)}
            </select>
          </label>
          <label className="org-field">
            <span className="org-field-label">HC 状态</span>
            <select value={department.hc_status ?? ""} onChange={(e) => onUpdateDepartment(department.id, { hc_status: e.target.value || null })}>
              <option value="">未设置</option>
              <option value="开放">开放</option>
              <option value="关闭">关闭</option>
              <option value="审批中">审批中</option>
              <option value="冻结">冻结</option>
            </select>
          </label>
          <Field label="团队人数" value={department.team_size} onChange={(v) => onUpdateDepartment(department.id, { team_size: v.trim() ? Number(v) : null })} />
          <Field label="业务方向" value={department.business_direction} onChange={(v) => onUpdateDepartment(department.id, { business_direction: v.trim() || null })} />
          <Field label="技术栈" value={department.tech_stack} onChange={(v) => onUpdateDepartment(department.id, { tech_stack: v.trim() || null })} />

          <h4>敏感信息</h4>
          <Field label="HC 内部判断" value={department.hc_internal_note} sensitive onChange={(v) => onUpdateDepartment(department.id, { hc_internal_note: v.trim() || null })} />
        </div>
      )}
    </div>
  );
}
