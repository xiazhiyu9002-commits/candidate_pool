import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, test, vi } from "vitest";

import type { BindEmployeeResult, OrgCompany, OrgTreeNode } from "../App";
import { MappingPage, type MappingPageProps } from "./MappingPage";

// 脑图用 canvas 量文字宽度来算布局；jsdom 没有 canvas，会刷一屏 "Not implemented"。
// 这里显式让 getContext 返回 null，走组件里的等宽估算分支。
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});

const ROOT: OrgTreeNode = {
  id: "c1", kind: "company", name: "字节跳动",
  title: null, job_level: null, team_size: null, is_key: false,
  children: [{
    id: "d1", kind: "department", name: "技术部",
    title: null, job_level: null, team_size: 10, is_key: false, children: [],
  }],
};

function makeProps(overrides: Partial<MappingPageProps> = {}): MappingPageProps {
  const noop = vi.fn();
  const companies: OrgCompany[] = [{ id: "c1", name: "字节跳动" }];
  return {
    companyName: "", onCompanyNameChange: noop,
    companies, selectedCompanyId: "c1",
    selectedOrgNode: null, selectedOrgNodeId: null,
    selectedOrgEmployee: null, selectedOrgDepartment: null,
    employees: [], departments: [],
    displayOrgTree: ROOT,
    orgSearch: "", orgFilterKind: "", orgFilterKey: false,
    leftCollapsed: false, rightCollapsed: false, pendingEditId: null,
    orgImportText: "", orgImportFileName: "", orgImportDraft: null,
    orgImportQuestions: [], orgImportAnswers: [], orgImportBusy: false, orgImportMessage: "",
    orgReviseInstruction: "", orgSource: null,
    onCreateCompany: noop, onLoadCompanies: noop, onAddOrgPerson: noop, onUndo: noop, onRedo: noop,
    onOrgImportTextChange: noop, onParseOrgImportFile: noop, onOrgImportFileNameChange: noop,
    onParseOrgImportText: noop, onCommitOrgImport: noop, onAnswerOrgImportDraft: noop,
    onOrgImportAnswersChange: noop, onReviseOrgImportDraft: noop, onOrgReviseInstructionChange: noop,
    onUpdateOrgImportCompanyName: noop, onUpdateOrgImportDepartment: noop, onUpdateOrgImportEmployee: noop,
    onSelectCompany: noop, onToggleOrgSource: noop, onDeleteCompany: noop, onOrgSourceChange: noop,
    onOrgSearchChange: noop, onOrgFilterKindChange: noop, onOrgFilterKeyChange: noop,
    onLeftCollapsedChange: noop, onRightCollapsedChange: noop, onSelectOrgNode: noop,
    onRenameOrgNode: noop, onAddOrgChild: noop, onAddOrgSibling: noop, onDeleteOrgNode: noop,
    onMoveOrgNode: noop, onPendingEditConsumed: noop, onUpdateOrgEmployeeField: noop,
    onUpdateOrgDepartmentField: noop,
    onBindOrgEmployee: async () => ({ matched: false } as BindEmployeeResult),
    onPreviewResume: noop,
    onExportOrgInternal: noop, onExportOrgClient: noop, onExportOrgArchPdf: noop,
    ...overrides,
  };
}


test("只选了公司、没有选中节点时也能加部门（从公司开始加部门）", async () => {
  const onAddOrgChild = vi.fn();
  const user = userEvent.setup();
  render(<MappingPage {...makeProps({ onAddOrgChild })} />);

  await user.click(screen.getByRole("button", { name: "＋部门" }));
  // 根节点未知时以公司树根作为父级（parent_id 为空＝挂在公司下）。
  expect(onAddOrgChild).toHaveBeenCalledWith(ROOT);
});


test("点部门会展开右侧信息，而不是把已展开的面板收起", async () => {
  const onRightCollapsedChange = vi.fn();
  const user = userEvent.setup();
  // 面板默认是展开的（rightCollapsed=false），旧逻辑会在首次点击时把它关掉。
  render(<MappingPage {...makeProps({ onRightCollapsedChange })} />);

  await user.click(screen.getByText("技术部"));
  expect(onRightCollapsedChange).toHaveBeenCalledWith(false);
});


test("重复点击同一个已选中的部门才收起右侧信息", async () => {
  const onRightCollapsedChange = vi.fn();
  const user = userEvent.setup();
  render(<MappingPage {...makeProps({
    onRightCollapsedChange,
    selectedOrgNodeId: "d1",
    selectedOrgNode: ROOT.children[0],
  })} />);

  await user.click(screen.getByText("技术部"));
  expect(onRightCollapsedChange).toHaveBeenCalledWith(true);
});


test("面板已收起时点部门会重新展开", async () => {
  const onRightCollapsedChange = vi.fn();
  const user = userEvent.setup();
  render(<MappingPage {...makeProps({
    onRightCollapsedChange,
    rightCollapsed: true,
    selectedOrgNodeId: "d1",
    selectedOrgNode: ROOT.children[0],
  })} />);

  await user.click(screen.getByText("技术部"));
  expect(onRightCollapsedChange).toHaveBeenCalledWith(false);
});


test("全屏时用浮层保留关键操作按钮", async () => {
  const onAddOrgChild = vi.fn();
  const { container } = render(<MappingPage {...makeProps({ onAddOrgChild })} />);

  expect(screen.queryByRole("toolbar", { name: "全屏操作" })).not.toBeInTheDocument();

  // 模拟进入全屏：fullscreenElement 指向 .mapping-stage 并派发 fullscreenchange。
  const stage = container.querySelector(".mapping-stage");
  Object.defineProperty(document, "fullscreenElement", { value: stage, configurable: true });
  document.dispatchEvent(new Event("fullscreenchange"));

  const bar = await screen.findByRole("toolbar", { name: "全屏操作" });
  const user = userEvent.setup();
  await user.click(withinBar(bar, "＋部门"));
  expect(onAddOrgChild).toHaveBeenCalledWith(ROOT);

  Object.defineProperty(document, "fullscreenElement", { value: null, configurable: true });
});

function withinBar(bar: HTMLElement, name: string): HTMLElement {
  const button = Array.from(bar.querySelectorAll("button")).find((b) => b.textContent === name);
  if (!button) throw new Error(`全屏浮层里找不到按钮：${name}`);
  return button;
}


test("在空白画布按住拖动可平移视图", () => {
  const { container } = render(<MappingPage {...makeProps()} />);
  const canvas = container.querySelector(".org-mindmap") as HTMLElement;
  // jsdom 没有布局，scrollLeft 恒为 0；在实例上放替身属性才能验证平移算式。
  Object.defineProperty(canvas, "scrollLeft", { value: 100, writable: true });
  Object.defineProperty(canvas, "scrollTop", { value: 50, writable: true });

  fireEvent.mouseDown(canvas, { button: 0, clientX: 200, clientY: 200 });
  expect(canvas.className).toContain("is-panning");

  // 指针右移 30、上移 20，视图反向滚动。
  fireEvent.mouseMove(document, { clientX: 230, clientY: 180 });
  expect(canvas.scrollLeft).toBe(70);
  expect(canvas.scrollTop).toBe(70);

  // 松手监听挂在 document 上，指针移出容器也能正常结束。
  fireEvent.mouseUp(document);
  expect(canvas.className).not.toContain("is-panning");
});


test("节点名称与副标题共用一个纵向容器，不会各拿一半宽度被截断", () => {
  // 回归守卫：原先 CSS 写的是 `.org-node > span { flex: 1 }`，它同时命中名称与副标题两个 span，
  // 在横向 flex 容器里各拿一半宽度，于是双双被 `text-overflow: ellipsis` 截断
  // ——而节点宽度是按 max(名称宽, 副标题宽) 算出来的，等于白算，表现为「每个节点文字显示不全」。
  render(<MappingPage {...makeProps()} />);

  const name = screen.getByText("技术部");
  const sub = screen.getByText("10 人");
  expect(name).toHaveClass("org-node-name");
  // 同一个父容器 = 同一列纵向排列；分成两个并列子节点就会各自挤压。
  expect(name.parentElement).not.toBeNull();
  expect(name.parentElement).toBe(sub.parentElement);
  expect(name.parentElement).toHaveClass("org-node-text");
  expect(name.parentElement?.parentElement).toHaveClass("org-node");
});


test("在节点上按下不会触发平移", () => {
  const { container } = render(<MappingPage {...makeProps()} />);
  const canvas = container.querySelector(".org-mindmap") as HTMLElement;

  fireEvent.mouseDown(screen.getByText("技术部"), { button: 0 });
  expect(canvas.className).not.toContain("is-panning");
});
