import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { RecruitmentApi } from "../src/App";
import { AdvancedAiSettings } from "../src/ai/AdvancedAiSettings";
import type { AiCatalog, AiConfig } from "../src/ai/types";

const catalog: AiCatalog = { version: 1, default_provider_id: "deepseek", providers: [] };
const api = { probeAiConnection: vi.fn() } as unknown as RecruitmentApi;

function probeReport() {
  return {
    probe_token: "receipt-123",
    auth: { ok: true, error_code: null, suggested_action: null },
    text: { ok: true, error_code: null, suggested_action: null },
    json: { ok: true, error_code: null, suggested_action: null },
    reasoning: { ok: true, error_code: null, suggested_action: null },
    vision: { ok: true, error_code: null, suggested_action: null },
    role_models: { fast_text: "deepseek-v4-flash" },
    models: [],
  };
}

function config(connections: AiConfig["connections"]): AiConfig {
  return {
    protection_level: connections.length >= 2 ? "dual" : connections.length === 1 ? "single" : "none",
    connections,
    catalog_version: 1,
  };
}

function singleConnection(): AiConfig["connections"][number] {
  return {
    connection_id: "conn-1",
    provider_id: "deepseek",
    display_name: "DeepSeek",
    masked_api_key: "sk-****value",
    has_api_key: true,
    base_url_override: null,
    parameter_style: null,
    models: { fast_text: "deepseek-v4-flash" },
    probed_roles: ["fast_text"],
    enabled: true,
  };
}

describe("AdvancedAiSettings", () => {
  test("prompts to add a primary service when empty", () => {
    render(<AdvancedAiSettings api={api} catalog={catalog} config={config([])} onSave={vi.fn()} onRefreshCatalog={vi.fn()} />);
    expect(screen.getByText(/尚未配置 AI 服务/)).toBeVisible();
  });

  test("shows editable model ids and reorder/refresh actions", () => {
    render(
      <AdvancedAiSettings
        api={api}
        catalog={catalog}
        config={config([singleConnection()])}
        onSave={vi.fn()}
        onRefreshCatalog={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("deepseek-v4-flash")).toBeVisible();
    expect(screen.getByRole("button", { name: "刷新目录" })).toBeVisible();
    expect(screen.getByRole("button", { name: "保存高级设置" })).toBeVisible();
  });

  test("reordering decides which two connections are primary and backup", async () => {
    const user = userEvent.setup();
    const second = { ...singleConnection(), connection_id: "conn-2", provider_id: "qwen" as const, display_name: "通义千问" };
    render(
      <AdvancedAiSettings
        api={api}
        catalog={catalog}
        config={config([singleConnection(), second])}
        onSave={vi.fn()}
        onRefreshCatalog={vi.fn()}
      />,
    );
    expect(screen.getByText("DeepSeek（主）")).toBeVisible();
    expect(screen.getByText("通义千问（备）")).toBeVisible();
    const moveUp = screen.getAllByRole("button", { name: "上移" });
    expect(moveUp[0]).toBeDisabled();
    await user.click(moveUp[1]);
    expect(screen.getByText("通义千问（主）")).toBeVisible();
    expect(screen.getByText("DeepSeek（备）")).toBeVisible();
  });

  test("provides a change API key entry without deleting the connection", async () => {
    const user = userEvent.setup();
    render(
      <AdvancedAiSettings
        api={api}
        catalog={catalog}
        config={config([singleConnection()])}
        onSave={vi.fn()}
        onRefreshCatalog={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "更换 Key" }));
    await user.type(screen.getByLabelText("新 API Key"), "sk-new-key");
    await user.click(screen.getByRole("button", { name: "确认更换" }));
    expect(screen.getByText(/已记录新 API Key/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "删除" })).toBeVisible();
  });

  test("new key is submitted to re-probe request", async () => {
    const probeAiConnection = vi.fn(async () => probeReport());
    const user = userEvent.setup();
    render(
      <AdvancedAiSettings
        api={{ probeAiConnection } as unknown as RecruitmentApi}
        catalog={catalog}
        config={config([singleConnection()])}
        onSave={vi.fn(async () => null)}
        onRefreshCatalog={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "更换 Key" }));
    await user.type(screen.getByLabelText("新 API Key"), "sk-new-key");
    await user.click(screen.getByRole("button", { name: "确认更换" }));
    await user.click(screen.getByRole("button", { name: "重新探测" }));
    expect(probeAiConnection).toHaveBeenCalledWith(expect.objectContaining({ api_key: "sk-new-key" }));
  });

  test("save failure preserves drafts", async () => {
    const user = userEvent.setup();
    render(
      <AdvancedAiSettings
        api={api}
        catalog={catalog}
        config={config([singleConnection()])}
        onSave={vi.fn(async () => "保存失败")}
        onRefreshCatalog={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByDisplayValue("deepseek-v4-flash"), { target: { value: "changed-model" } });
    await user.click(screen.getByRole("button", { name: "保存高级设置" }));
    expect(screen.getByText(/保存失败/)).toBeVisible();
    expect(screen.getByDisplayValue("changed-model")).toBeVisible();
  });
});
