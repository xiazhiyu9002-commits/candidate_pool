import { useEffect, useRef, useState, type FormEvent } from "react";
import type {
  BindEmployeeResult,
  OrgClarificationQuestion,
  OrgCompany,
  OrgDepartment,
  OrgEmployee,
  OrgImportDraft,
  OrgTreeNode,
  UpdateOrgDepartmentInput,
  UpdateOrgEmployeeInput,
} from "../App";
import { OrgMindMap } from "../org/OrgMindMap";
import { OrgDetailPanel } from "../org/OrgDetailPanel";
import { Button } from "../components/ui";

export interface MappingPageProps {
  companyName: string;
  onCompanyNameChange: (value: string) => void;
  companies: OrgCompany[];
  selectedCompanyId: string | null;
  selectedOrgNode: OrgTreeNode | null;
  selectedOrgNodeId: string | null;
  selectedOrgEmployee: OrgEmployee | null;
  selectedOrgDepartment: OrgDepartment | null;
  employees: OrgEmployee[];
  departments: OrgDepartment[];
  displayOrgTree: OrgTreeNode | null;
  orgSearch: string;
  orgFilterKind: string;
  orgFilterKey: boolean;
  leftCollapsed: boolean;
  rightCollapsed: boolean;
  pendingEditId: string | null;
  orgImportText: string;
  orgImportFileName: string;
  orgImportDraft: OrgImportDraft | null;
  orgImportQuestions: OrgClarificationQuestion[];
  orgImportAnswers: string[];
  orgImportBusy: boolean;
  orgImportMessage: string;
  orgReviseInstruction: string;
  orgSource: { companyId: string; text: string | null } | null;
  onCreateCompany: (event: FormEvent) => void;
  onLoadCompanies: () => void;
  onAddOrgPerson: (node: OrgTreeNode) => void;
  onUndo: () => void;
  onRedo: () => void;
  onOrgImportTextChange: (value: string) => void;
  onParseOrgImportFile: (file: File) => void;
  onOrgImportFileNameChange: (value: string) => void;
  onParseOrgImportText: () => void;
  onCommitOrgImport: () => void;
  onAnswerOrgImportDraft: () => void;
  onOrgImportAnswersChange: (answers: string[]) => void;
  onReviseOrgImportDraft: () => void;
  onOrgReviseInstructionChange: (value: string) => void;
  onUpdateOrgImportCompanyName: (value: string) => void;
  onUpdateOrgImportDepartment: (index: number, field: string, value: string | number | null) => void;
  onUpdateOrgImportEmployee: (index: number, field: string, value: string | number | null) => void;
  onSelectCompany: (companyId: string) => void;
  onToggleOrgSource: (companyId: string) => void;
  onDeleteCompany: (companyId: string) => void;
  onOrgSourceChange: (source: { companyId: string; text: string | null } | null) => void;
  onOrgSearchChange: (value: string) => void;
  onOrgFilterKindChange: (value: string) => void;
  onOrgFilterKeyChange: (value: boolean) => void;
  onLeftCollapsedChange: (value: boolean) => void;
  onRightCollapsedChange: (value: boolean) => void;
  onSelectOrgNode: (nodeId: string) => void;
  onRenameOrgNode: (node: OrgTreeNode, name: string) => void;
  onAddOrgChild: (node: OrgTreeNode) => void;
  onAddOrgSibling: (node: OrgTreeNode) => void;
  onDeleteOrgNode: (node: OrgTreeNode) => void;
  onMoveOrgNode: (source: OrgTreeNode, target: OrgTreeNode) => void;
  onPendingEditConsumed: () => void;
  onUpdateOrgEmployeeField: (id: string, changes: UpdateOrgEmployeeInput) => void;
  onUpdateOrgDepartmentField: (id: string, changes: UpdateOrgDepartmentInput) => void;
  onBindOrgEmployee: (employeeId: string, phone: string, name: string) => Promise<BindEmployeeResult>;
  onPreviewResume: (revisionId: string, name?: string) => void;
  onExportOrgInternal: (companyId: string) => void;
  onExportOrgClient: (companyId: string) => void;
  onExportOrgArchPdf: (companyId: string) => void;
}

export function MappingPage(props: MappingPageProps) {
  const {
    companyName, onCompanyNameChange, companies, selectedCompanyId, selectedOrgNode,
    selectedOrgNodeId, selectedOrgEmployee, selectedOrgDepartment, employees, departments,
    displayOrgTree, orgSearch, orgFilterKind, orgFilterKey, leftCollapsed, rightCollapsed,
    pendingEditId, orgImportText, orgImportFileName, orgImportDraft, orgImportQuestions,
    orgImportAnswers, orgImportBusy, orgImportMessage, orgReviseInstruction, orgSource,
    onCreateCompany, onLoadCompanies, onAddOrgPerson, onUndo, onRedo, onOrgImportTextChange,
    onParseOrgImportFile, onOrgImportFileNameChange, onParseOrgImportText, onCommitOrgImport,
    onAnswerOrgImportDraft, onOrgImportAnswersChange, onReviseOrgImportDraft,
    onOrgReviseInstructionChange, onUpdateOrgImportCompanyName, onUpdateOrgImportDepartment,
    onUpdateOrgImportEmployee, onSelectCompany, onToggleOrgSource, onDeleteCompany,
    onOrgSourceChange, onOrgSearchChange, onOrgFilterKindChange, onOrgFilterKeyChange,
    onLeftCollapsedChange, onRightCollapsedChange, onSelectOrgNode, onRenameOrgNode,
    onAddOrgChild, onAddOrgSibling, onDeleteOrgNode, onMoveOrgNode, onPendingEditConsumed,
    onUpdateOrgEmployeeField, onUpdateOrgDepartmentField, onBindOrgEmployee, onPreviewResume,
    onExportOrgInternal, onExportOrgClient, onExportOrgArchPdf,
  } = props;

  const [orgImportOpen, setOrgImportOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);

  // 全屏作用在「脑图 + 右侧详情」整体上，保证全屏时点击部门仍能看到右侧部门信息。
  useEffect(() => {
    function onFullscreenChange() {
      setIsFullscreen(document.fullscreenElement === stageRef.current);
    }
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  async function toggleFullscreen() {
    const el = stageRef.current;
    if (!el || typeof el.requestFullscreen !== "function") return;
    if (document.fullscreenElement === el) await document.exitFullscreen();
    else await el.requestFullscreen();
  }

  function selectOrgNode(node: OrgTreeNode) {
    onSelectOrgNode(node.id);
    if (node.kind !== "department") return;
    // 点部门默认展开右侧信息；只有「重复点击已选中的同一个部门」才收起。
    // 原来的写法是纯 toggle，而面板默认已是展开的，导致首次点击反而把面板关掉。
    const collapseNow = !rightCollapsed && selectedOrgNodeId === node.id;
    onRightCollapsedChange(collapseNow);
  }

  // 全屏快捷键：F11 或 Ctrl/⌘+Shift+F。挂在 document 上，页面任意位置都生效。
  useEffect(() => {
    function onFullscreenKey(event: KeyboardEvent) {
      const hit = event.key === "F11"
        || (event.key.toLowerCase() === "f" && (event.metaKey || event.ctrlKey) && event.shiftKey);
      if (!hit) return;
      event.preventDefault();
      void toggleFullscreen();
    }
    document.addEventListener("keydown", onFullscreenKey);
    return () => document.removeEventListener("keydown", onFullscreenKey);
    // toggleFullscreen 只读 stageRef 与 document，闭包在第一帧即稳定。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 公司根节点：优先用脑图里选中的公司节点，否则用当前公司的树根。
  const rootNode = selectedOrgNode?.kind === "company" ? selectedOrgNode : displayOrgTree;
  const canAddTopDepartment = Boolean(selectedCompanyId) && rootNode !== null
    && (selectedOrgNode === null || selectedOrgNode.kind === "company");
  const canAddSubDepartment = selectedOrgNode !== null && selectedOrgNode.kind === "department";
  const canAddPerson = selectedOrgNode !== null && selectedOrgNode.kind !== "company";

  function addTopDepartment() {
    if (rootNode) onAddOrgChild(rootNode);
  }

  return (
    <section className="jd-panel">
      <div className="mapping-help">
        <strong>操作：</strong>Tab 新增子节点（公司→部门 · 部门→子部门 · 人员→下属）· Enter 新增同级 · 「＋部门」在选中节点下加部门 · 「＋人员」在部门下加人 · 双击/F2 改名 · 拖拽调整层级（可拖到公司根节点提升为顶层部门）· Shift 返回上级 · ←/→ 同级切换 · Space 折叠 · Ctrl/⌘+Z 撤销 · Ctrl/⌘+滚轮缩放 · 工具栏 ⛶ / F11 / Ctrl+⌘+Shift+F 全屏（Esc 退出）
      </div>

      <div className="toolbar" style={{ flexWrap: "wrap" }}>
        <form className="jd-form" style={{ display: "flex", gap: 8, alignItems: "center" }} onSubmit={(event) => void onCreateCompany(event)}>
          <input value={companyName} onChange={(e) => onCompanyNameChange(e.target.value)} placeholder="公司名称，如：字节跳动" aria-label="公司名称" style={{ width: 220, height: 32 }} />
          <button type="submit" className="btn btn-primary">新建公司</button>
        </form>
        <button type="button" className="btn btn-secondary" onClick={() => setOrgImportOpen((v) => !v)}>导入组织</button>
        <button type="button" className="btn btn-secondary" onClick={onLoadCompanies}>刷新公司</button>
        {canAddTopDepartment && (
          <button type="button" className="btn btn-secondary" onClick={addTopDepartment}>＋部门</button>
        )}
        {canAddSubDepartment && selectedOrgNode && (
          <button type="button" className="btn btn-secondary" onClick={() => onAddOrgChild(selectedOrgNode)}>＋子部门</button>
        )}
        {canAddPerson && selectedOrgNode && (
          <button type="button" className="btn btn-secondary" onClick={() => onAddOrgPerson(selectedOrgNode)}>＋人员</button>
        )}
        {selectedCompanyId && (
          <>
            <button type="button" className="btn btn-ghost btn-xs" onClick={onUndo}>撤销</button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={onRedo}>重做</button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onExportOrgInternal(selectedCompanyId)}>内部 Excel</button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onExportOrgClient(selectedCompanyId)}>客户 Excel</button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => onExportOrgArchPdf(selectedCompanyId)}>架构图 PDF</button>
          </>
        )}
      </div>

      {orgImportOpen && (
        <div className="section">
          <div className="section-head"><h3>导入组织</h3></div>
        <div className="jd-form">
          <textarea
            value={orgImportText}
            onChange={(e) => onOrgImportTextChange(e.target.value)}
            placeholder="粘贴组织描述文本（如：公司名、部门、人员）"
            aria-label="组织文本"
            rows={3}
          />
          <div className="jd-row">
            <label className="btn btn-secondary" style={{ cursor: "pointer" }}>
              上传文件导入
              <input
                type="file"
                accept=".txt,.docx"
                aria-label="导入组织文件"
                style={{ display: "none" }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) onParseOrgImportFile(f); }}
              />
            </label>
            {orgImportFileName && (
              <>
                <span className="muted">{orgImportFileName}</span>
                <Button variant="ghost" size="sm" onClick={() => onOrgImportFileNameChange("")}>移除</Button>
              </>
            )}
            <Button variant="secondary" disabled={orgImportBusy} onClick={onParseOrgImportText}>
              {orgImportBusy ? "解析中…" : "解析粘贴文本"}
            </Button>
            {orgImportDraft && (
              <Button variant="secondary" disabled={orgImportBusy} onClick={onCommitOrgImport}>
                确认导入
              </Button>
            )}
          </div>
        </div>
        {orgImportQuestions.length > 0 && (
          <div className="section">
            <div className="section-head"><h3>解析疑问</h3></div>
            {orgImportQuestions.map((q, i) => (
              <div key={i} className="jd-row" style={{ marginTop: 6 }}>
                <span className="muted">{q.question}{q.hint ? `（${q.hint}）` : ""}</span>
                <input
                  value={orgImportAnswers[i] ?? ""}
                  onChange={(e) => {
                    const next = [...orgImportAnswers];
                    next[i] = e.target.value;
                    onOrgImportAnswersChange(next);
                  }}
                  placeholder="请回答"
                  aria-label={`解析疑问${i}`}
                />
              </div>
            ))}
            <div className="jd-row" style={{ marginTop: 6 }}>
              <Button variant="secondary" disabled={orgImportBusy} onClick={onAnswerOrgImportDraft}>
                {orgImportBusy ? "解析中…" : "提交回答并继续解析"}
              </Button>
            </div>
          </div>
        )}
        {orgImportDraft && (
          <div className="section org-import-preview">
            <div className="section-head">
              <h3>解析结果（可编辑后导入）</h3>
              <span className="muted">目标公司：{selectedCompanyId ? (companies.find((c) => c.id === selectedCompanyId)?.name ?? "已选择") : "未选择"}</span>
            </div>
            <div className="jd-form" style={{ margin: "0 0 12px" }}>
              <div className="jd-row">
                <input
                  value={orgReviseInstruction}
                  onChange={(e) => onOrgReviseInstructionChange(e.target.value)}
                  placeholder="用一句话修正，如：把「叶程」改名为「贺喜」、合并 A 与 B 部门"
                  aria-label="修正指令"
                />
                <Button variant="secondary" disabled={orgImportBusy || !orgReviseInstruction.trim()} onClick={onReviseOrgImportDraft}>
                  {orgImportBusy ? "修订中…" : "应用修正"}
                </Button>
              </div>
            </div>
            <label className="org-field">
              <span className="org-field-label">公司名称</span>
              <input value={orgImportDraft.company_name} onChange={(e) => onUpdateOrgImportCompanyName(e.target.value)} aria-label="导入公司名称" />
            </label>

            <h4>部门（{orgImportDraft.departments.length}）</h4>
            {orgImportDraft.departments.length === 0 ? (
              <p className="muted">未识别到部门</p>
            ) : (
              orgImportDraft.departments.map((dept, i) => (
                <div className="case-row" key={i}>
                  <input value={dept.name} onChange={(e) => onUpdateOrgImportDepartment(i, "name", e.target.value)} aria-label={`部门${i}名称`} />
                  <input value={dept.parent_name ?? ""} onChange={(e) => onUpdateOrgImportDepartment(i, "parent_name", e.target.value || null)} placeholder="上级部门" aria-label={`部门${i}上级`} />
                  <input value={dept.leader_name ?? ""} onChange={(e) => onUpdateOrgImportDepartment(i, "leader_name", e.target.value || null)} placeholder="负责人" aria-label={`部门${i}负责人`} />
                  <input value={dept.team_size ?? ""} onChange={(e) => onUpdateOrgImportDepartment(i, "team_size", e.target.value ? Number(e.target.value) : null)} placeholder="人数" type="number" aria-label={`部门${i}人数`} />
                </div>
              ))
            )}

            <h4>人员（{orgImportDraft.employees.length}）</h4>
            {orgImportDraft.employees.length === 0 ? (
              <p className="muted">未识别到人员</p>
            ) : (
              orgImportDraft.employees.map((emp, i) => (
                <div className="case-row" key={i}>
                  <input value={emp.name} onChange={(e) => onUpdateOrgImportEmployee(i, "name", e.target.value)} aria-label={`人员${i}姓名`} />
                  <input value={emp.alias ?? ""} onChange={(e) => onUpdateOrgImportEmployee(i, "alias", e.target.value || null)} placeholder="花名" aria-label={`人员${i}花名`} />
                  <input value={emp.title ?? ""} onChange={(e) => onUpdateOrgImportEmployee(i, "title", e.target.value || null)} placeholder="职位" aria-label={`人员${i}职位`} />
                  <input value={emp.department_name ?? ""} onChange={(e) => onUpdateOrgImportEmployee(i, "department_name", e.target.value || null)} placeholder="部门" aria-label={`人员${i}部门`} />
                  <input value={emp.report_to_name ?? ""} onChange={(e) => onUpdateOrgImportEmployee(i, "report_to_name", e.target.value || null)} placeholder="汇报给" aria-label={`人员${i}汇报人`} />
                </div>
              ))
            )}
          </div>
        )}
        {orgImportMessage && <p role="status" className="muted">{orgImportMessage}</p>}
        </div>
      )}

      {companies.length === 0 ? (
        <div className="empty-state"><strong>还没有公司</strong><p>先在上方新建一家公司，再录入部门与人员。</p></div>
      ) : (
        <div className={["mapping-grid", leftCollapsed ? "no-left" : ""].join(" ").trim()}>
          {leftCollapsed ? (
            <button className="mapping-rail" onClick={() => onLeftCollapsedChange(false)} title="展开左侧">»</button>
          ) : (
            <div className="mapping-panel">
              <div className="mapping-panel-head">
                <h3>公司</h3>
                <button className="mapping-collapse" onClick={() => onLeftCollapsedChange(true)} title="收起左侧">«</button>
              </div>
              {companies.map((c) => (
                <div key={c.id} className="case-row">
                  <button
                    className={c.id === selectedCompanyId ? "nav-item active" : "nav-item"}
                    onClick={() => onSelectCompany(c.id)}
                  >
                    {c.name}
                  </button>
                  <Button variant="ghost" size="sm" onClick={() => onToggleOrgSource(c.id)}>原文</Button>
                  <Button variant="danger" size="sm" onClick={() => onDeleteCompany(c.id)}>删除</Button>
                </div>
              ))}
              {orgSource && orgSource.companyId && (
                <div className="org-source-preview">
                  <div className="section-head">
                    <strong>导入原文</strong>
                    <Button variant="ghost" size="sm" onClick={() => onOrgSourceChange(null)}>收起</Button>
                  </div>
                  {orgSource.text ? (
                    <pre className="jd-source-text">{orgSource.text}</pre>
                  ) : (
                    <p className="muted">该公司尚未保存导入原文。</p>
                  )}
                </div>
              )}

              <h4>搜索</h4>
              <input value={orgSearch} onChange={(e) => onOrgSearchChange(e.target.value)} placeholder="姓名 / 岗位 / 职级" aria-label="搜索节点" />

              <h4>筛选</h4>
              <select value={orgFilterKind} onChange={(e) => onOrgFilterKindChange(e.target.value)} aria-label="节点类型">
                <option value="">全部类型</option>
                <option value="department">部门</option>
                <option value="employee">人员</option>
              </select>
              <label className="org-filter-key">
                <input type="checkbox" checked={orgFilterKey} onChange={(e) => onOrgFilterKeyChange(e.target.checked)} /> 仅核心岗位
              </label>
            </div>
          )}

          <div className={["mapping-stage", rightCollapsed ? "no-right" : ""].join(" ").trim()} ref={stageRef}>
            {isFullscreen && (
              // 全屏作用域只包住脑图+右侧详情，页面顶部工具栏会随之隐藏，
              // 因此把关键操作以浮层形式留在全屏内。
              <div className="mapping-fs-toolbar" role="toolbar" aria-label="全屏操作">
                {canAddTopDepartment && (
                  <button type="button" className="btn btn-secondary btn-xs" onClick={addTopDepartment}>＋部门</button>
                )}
                {canAddSubDepartment && selectedOrgNode && (
                  <button type="button" className="btn btn-secondary btn-xs" onClick={() => onAddOrgChild(selectedOrgNode)}>＋子部门</button>
                )}
                {canAddPerson && selectedOrgNode && (
                  <button type="button" className="btn btn-secondary btn-xs" onClick={() => onAddOrgPerson(selectedOrgNode)}>＋人员</button>
                )}
                {selectedCompanyId && (
                  <>
                    <button type="button" className="btn btn-ghost btn-xs" onClick={onUndo}>撤销</button>
                    <button type="button" className="btn btn-ghost btn-xs" onClick={onRedo}>重做</button>
                  </>
                )}
                <button type="button" className="btn btn-ghost btn-xs" onClick={() => void toggleFullscreen()}>退出全屏</button>
              </div>
            )}
            <OrgMindMap
              tree={displayOrgTree}
              selectedId={selectedOrgNodeId}
              onSelect={selectOrgNode}
              onRename={(node, name) => onRenameOrgNode(node, name)}
              onAddChild={(node) => onAddOrgChild(node)}
              onAddSibling={(node) => onAddOrgSibling(node)}
              onDelete={(node) => onDeleteOrgNode(node)}
              onMove={(source, target) => onMoveOrgNode(source, target)}
              onUndo={onUndo}
              onRedo={onRedo}
              pendingEditId={pendingEditId}
              onPendingEditConsumed={onPendingEditConsumed}
              isFullscreen={isFullscreen}
              onToggleFullscreen={() => void toggleFullscreen()}
            />

            {rightCollapsed ? (
              <button className="mapping-rail" onClick={() => onRightCollapsedChange(false)} title="展开右侧">«</button>
            ) : (
              <OrgDetailPanel
                node={selectedOrgNode}
                employee={selectedOrgEmployee}
                department={selectedOrgDepartment}
                employees={employees}
                departments={departments}
                onUpdateEmployee={(id, changes) => onUpdateOrgEmployeeField(id, changes)}
                onUpdateDepartment={(id, changes) => onUpdateOrgDepartmentField(id, changes)}
                onDelete={(node) => onDeleteOrgNode(node)}
                onAddChild={(node) => onAddOrgChild(node)}
                onBindEmployee={(id, phone, name) => onBindOrgEmployee(id, phone, name)}
                onPreviewResume={(revisionId, name) => onPreviewResume(revisionId, name)}
                onCollapse={() => onRightCollapsedChange(true)}
              />
            )}
          </div>
        </div>
      )}
    </section>
  );
}
