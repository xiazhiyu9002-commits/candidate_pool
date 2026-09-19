import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { RecruitmentApi } from "../src/App";
import { ConnectionWizard } from "../src/ai/ConnectionWizard";
import type { AiCatalog } from "../src/ai/types";

const catalog: AiCatalog = {
  version: 1,
  default_provider_id: "deepseek",
  providers: [
    {
      provider_id: "kimi_code",
      label: "Kimi Code 订阅",
      base_url: "https://api.kimi.com/coding/v1",
      parameter_style: "kimi_code",
      allowed_contexts: ["interactive", "background", "batch"],
      models: {},
      recommended_models: {},
      help_url: "",
      key_help_url: "",
      subscription_warning: "订阅版受会员额度与并发频率限制，后台批量解析可能触发限流；大批量任务建议使用 Kimi 开放平台。",
      deprecated: false,
    },
  ],
};

function wizardProps(slot: "primary" | "secondary") {
  const api = {
    probeAiConnection: vi.fn(async () => ({
      auth: { ok: true, error_code: null, suggested_action: null },
      text: { ok: true, error_code: null, suggested_action: null },
      json: { ok: true, error_code: null, suggested_action: null },
      reasoning: { ok: true, error_code: null, suggested_action: null },
      vision: { ok: true, error_code: null, suggested_action: null },
      role_models: { fast_text: "deepseek-v4-flash", reasoning_text: "deepseek-v4-pro", vision: "deepseek-v4-flash-vision-exp" },
      models: [],
    })),
  } as unknown as RecruitmentApi;
  return { api, catalog, slot, onClose: vi.fn(), onSave: vi.fn(async () => null) };
}

describe("ConnectionWizard", () => {
  test("Kimi Code requires acknowledgement and is not recommended as automatic backup", async () => {
    const user = userEvent.setup();
    render(<ConnectionWizard {...wizardProps("secondary")} />);
    await user.click(screen.getByRole("button", { name: /Kimi Code 订阅/ }));
    expect(screen.getByText(/订阅版受会员额度与并发频率限制/)).toBeVisible();
    expect(screen.getByRole("button", { name: "检测并继续" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /我已了解使用范围/ }));
    expect(screen.getByRole("button", { name: "检测并继续" })).toBeEnabled();
  });

  test("secondary wizard deprioritizes DeepSeek as a backup", () => {
    render(<ConnectionWizard {...wizardProps("secondary")} />);
    const buttons = screen.getAllByRole("button").filter((b) => /DeepSeek|Kimi|通义|智谱|硅基/.test(b.textContent ?? ""));
    // DeepSeek 仍可选作备用（同一供应商双 Key），但排在最后，不作为推荐。
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons[0].textContent).not.toContain("DeepSeek");
  });

  test("save returns the typed key and friendly display name", async () => {
    const props = wizardProps("primary");
    const user = userEvent.setup();
    render(<ConnectionWizard {...props} />);
    await user.type(screen.getByLabelText("API Key"), "sk-abc123");
    await user.click(screen.getByRole("button", { name: "检测并继续" }));
    await user.click(screen.getByRole("button", { name: "保存为主服务" }));
    expect(props.onSave).toHaveBeenCalledWith({
      provider_id: "deepseek",
      display_name: "DeepSeek",
      api_key: "sk-abc123",
      models: { fast_text: "deepseek-v4-flash", reasoning_text: "deepseek-v4-pro", vision: "deepseek-v4-flash-vision-exp" },
    });
  });

  test("primary slot preselects DeepSeek", () => {
    render(<ConnectionWizard {...wizardProps("primary")} />);
    // 主服务为空时预选 DeepSeek，直接显示 Key 输入，不显示供应商列表。
    expect(screen.getByLabelText("API Key")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Kimi 开放平台/ })).not.toBeInTheDocument();
  });

  test("custom_openai exposes technical fields and saves them", async () => {
    const props = wizardProps("secondary");
    const user = userEvent.setup();
    render(<ConnectionWizard {...props} />);
    await user.click(screen.getByRole("button", { name: /自定义 OpenAI 兼容/ }));
    await user.type(screen.getByLabelText("Base URL"), "https://custom.example.com/v1");
    await user.type(screen.getByLabelText("快速模型 ID"), "my-fast-model");
    await user.type(screen.getByLabelText("API Key"), "sk-custom");
    await user.click(screen.getByRole("button", { name: "检测并继续" }));
    await user.click(screen.getByRole("button", { name: "保存为备用服务" }));
    expect(props.onSave).toHaveBeenCalledWith(expect.objectContaining({
      provider_id: "custom_openai",
      base_url_override: "https://custom.example.com/v1",
      parameter_style: "standard",
      api_key: "sk-custom",
      models: expect.objectContaining({ fast_text: "deepseek-v4-flash" }),
    }));
  });
});
