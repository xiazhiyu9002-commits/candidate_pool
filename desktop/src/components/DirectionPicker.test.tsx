import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { expect, test, vi } from "vitest";

import { BusinessDirectionPicker, CareerDirectionPicker } from "./DirectionPicker";

function setup(ui: ReactElement) {
  const user = userEvent.setup();
  render(ui);
  return user;
}

test("renders every 大类 with its 细分 nested in the same row", () => {
  setup(<CareerDirectionPicker directions={["BACKEND"]} specializations={[]} onChange={vi.fn()} />);
  const group = screen.getByRole("group", { name: "职业方向" });

  // 二级结构：后端这一行里同时有大类 chip 与它的 3 个细分 chip。
  const backendChip = within(group).getByRole("button", { name: "后端" });
  const row = backendChip.parentElement as HTMLElement;
  expect(within(row).getByRole("button", { name: /服务端架构/ })).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: /全栈交付/ })).toBeInTheDocument();
  // 管理类细分不会跑到后端行里。
  expect(within(row).queryByRole("button", { name: /团队管理/ })).not.toBeInTheDocument();
  expect(backendChip).toHaveAttribute("aria-pressed", "true");
});

test("selecting a 细分 automatically selects its parent 大类", async () => {
  const onChange = vi.fn();
  const user = setup(
    <CareerDirectionPicker directions={[]} specializations={[]} onChange={onChange} />,
  );

  await user.click(screen.getByRole("button", { name: /推荐与搜索算法/ }));

  // 细分依赖大类：一次点击同时补上 ALGORITHM 与 ALGORITHM_RECSYS。
  expect(onChange).toHaveBeenCalledWith({
    directions: ["ALGORITHM"],
    specializations: ["ALGORITHM_RECSYS"],
  });
});

test("dropping a 大类 also drops its 细分", async () => {
  const onChange = vi.fn();
  const user = setup(
    <CareerDirectionPicker
      directions={["BACKEND", "DATA"]}
      specializations={["BACKEND_SERVICE", "DATA_WAREHOUSE"]}
      onChange={onChange}
    />,
  );

  await user.click(screen.getByRole("button", { name: "后端" }));

  expect(onChange).toHaveBeenCalledWith({
    directions: ["DATA"],
    specializations: ["DATA_WAREHOUSE"],
  });
});

test("caps 大类 count and per-大类 细分 count", async () => {
  const onChange = vi.fn();
  const user = setup(
    <CareerDirectionPicker
      maxDirections={1}
      directions={["BACKEND"]}
      specializations={["BACKEND_SERVICE", "BACKEND_FULL_STACK"]}
      onChange={onChange}
    />,
  );

  // 大类已到上限：其他大类的 chip 被禁用。
  expect(screen.getByRole("button", { name: "前端" })).toBeDisabled();
  // 该大类下细分已到上限：第三个细分被禁用，已选中的仍可取消。
  expect(screen.getByRole("button", { name: /AI 应用集成/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: /服务端架构/ })).toBeEnabled();

  await user.click(screen.getByRole("button", { name: /AI 应用集成/ }));
  expect(onChange).not.toHaveBeenCalled();
});

test("hides the second level for the 大类-only confirmation flow", () => {
  setup(
    <CareerDirectionPicker
      directions={["BACKEND"]}
      specializations={[]}
      onChange={vi.fn()}
      showSpecializations={false}
    />,
  );

  expect(screen.getByRole("button", { name: "后端" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /服务端架构/ })).not.toBeInTheDocument();
});

test("business picker toggles values and respects the cap", async () => {
  const onChange = vi.fn();
  const user = setup(
    <BusinessDirectionPicker maxValues={2} values={["BANKING", "INSURANCE"]} onChange={onChange} />,
  );
  const group = screen.getByRole("group", { name: "业务方向" });

  expect(within(group).getByRole("button", { name: "电商" })).toBeDisabled();
  await user.click(within(group).getByRole("button", { name: "银行" }));
  expect(onChange).toHaveBeenCalledWith(["INSURANCE"]);
});
