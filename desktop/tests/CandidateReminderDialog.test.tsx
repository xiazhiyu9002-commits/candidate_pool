import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { CandidateReminderDialog } from "../src/reminders/CandidateReminderDialog";

function props(save: (content: string) => Promise<string | null> = vi.fn(async () => null)) {
  return {
    candidateName: "张三",
    onClose: vi.fn(),
    onSave: save,
  };
}

describe("建提醒", () => {
  test("人名自动带入标题，使用者只写内容", async () => {
    const user = userEvent.setup();
    const dialogProps = props();
    render(<CandidateReminderDialog {...dialogProps} />);

    expect(screen.getByRole("dialog", { name: "建提醒" })).toBeVisible();
    expect(screen.getByText("给 张三 建提醒")).toBeVisible();

    await user.type(screen.getByLabelText("提醒内容"), "下周一电话回访");
    await user.click(screen.getByRole("button", { name: "保存提醒" }));

    expect(dialogProps.onSave).toHaveBeenCalledWith("下周一电话回访");
  });

  test("内容为空白时不提交，只提示", async () => {
    const user = userEvent.setup();
    const dialogProps = props();
    render(<CandidateReminderDialog {...dialogProps} />);

    await user.type(screen.getByLabelText("提醒内容"), "   ");
    await user.click(screen.getByRole("button", { name: "保存提醒" }));

    expect(dialogProps.onSave).not.toHaveBeenCalled();
    expect(screen.getByText("请填写提醒内容")).toBeVisible();
  });

  test("保存失败时展示错误且不关闭", async () => {
    const user = userEvent.setup();
    const dialogProps = props(vi.fn(async () => "保存失败"));
    render(<CandidateReminderDialog {...dialogProps} />);

    await user.type(screen.getByLabelText("提醒内容"), "回访");
    await user.click(screen.getByRole("button", { name: "保存提醒" }));

    expect(screen.getByText("保存失败")).toBeVisible();
    expect(dialogProps.onClose).not.toHaveBeenCalled();
  });
});
