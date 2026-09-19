import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { AiServicesPanel } from "../src/ai/AiServicesPanel";
import type { AiCatalog, AiConfig, AiConnectionView, AiStatus } from "../src/ai/types";

const catalog: AiCatalog = { version: 1, default_provider_id: "deepseek", providers: [] };

function emptyStatus(): AiStatus {
  return { connections: [], last_fallback: null };
}

function connection(providerId: AiConnectionView["provider_id"], connectionId: string): AiConnectionView {
  return {
    connection_id: connectionId,
    provider_id: providerId,
    display_name: providerId,
    masked_api_key: "sk-****value",
    has_api_key: true,
    base_url_override: null,
    parameter_style: null,
    models: { fast_text: "model" },
    probed_roles: ["fast_text"],
    enabled: true,
  };
}

function configFor(connections: AiConnectionView[]): AiConfig {
  return {
    protection_level: connections.length >= 2 ? "dual" : connections.length === 1 ? "single" : "none",
    connections,
    catalog_version: 1,
  };
}

describe("AiServicesPanel", () => {
  test("shows DeepSeek as recommended without technical fields", () => {
    render(<AiServicesPanel catalog={catalog} config={configFor([])} status={emptyStatus()} />);
    expect(screen.getByText("DeepSeek")).toBeVisible();
    expect(screen.getByText("推荐主服务")).toBeVisible();
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
    expect(screen.queryByText("reasoning_effort")).not.toBeInTheDocument();
  });

  test("a single service works but recommends backup protection", () => {
    render(<AiServicesPanel catalog={catalog} config={configFor([connection("deepseek", "conn-1")])} status={emptyStatus()} />);
    expect(screen.getByText("单服务可用")).toBeVisible();
    expect(screen.getByRole("button", { name: "添加备用 AI 服务" })).toBeVisible();
    expect(screen.getByText("当前没有备用保护")).toBeVisible();
  });

  test("two providers show dual protection and explicit order", () => {
    render(
      <AiServicesPanel
        catalog={catalog}
        config={configFor([connection("deepseek", "conn-1"), connection("qwen", "conn-2")])}
        status={emptyStatus()}
      />,
    );
    expect(screen.getByText("双服务保护已开启")).toBeVisible();
    expect(screen.getByText("主服务")).toBeVisible();
    expect(screen.getByText("备用服务")).toBeVisible();
  });

  test("recent fallback overrides the static status copy", () => {
    render(
      <AiServicesPanel
        catalog={catalog}
        config={configFor([connection("deepseek", "conn-1"), connection("qwen", "conn-2")])}
        status={{
          connections: [],
          last_fallback: {
            primary_provider_id: "deepseek",
            primary_model: "deepseek-v4-flash",
            backup_provider_id: "qwen",
            backup_model: "qwen-flash",
            error_code: "E_API_RATE_LIMIT",
            occurred_at: Date.now() / 1000,
          },
        }}
      />,
    );
    expect(screen.getByText("DeepSeek 当前不可用，本次已由 通义千问 完成")).toBeVisible();
  });

  test("primary open circuit shows partial outage", () => {
    render(
      <AiServicesPanel
        catalog={catalog}
        config={configFor([connection("deepseek", "conn-1"), connection("qwen", "conn-2")])}
        status={{
          connections: [
            { connection_id: "conn-1", model: null, circuit_state: "open", consecutive_failures: 1, last_error_code: "rate_limit" },
            { connection_id: "conn-2", model: null, circuit_state: "closed", consecutive_failures: 0, last_error_code: null },
          ],
          last_fallback: null,
        }}
      />,
    );
    expect(screen.getByText("部分 AI 服务异常")).toBeVisible();
    expect(screen.getByText(/DeepSeek 当前不可用/)).toBeVisible();
  });

  test("all connections open shows all unavailable", () => {
    render(
      <AiServicesPanel
        catalog={catalog}
        config={configFor([connection("deepseek", "conn-1"), connection("qwen", "conn-2")])}
        status={{
          connections: [
            { connection_id: "conn-1", model: null, circuit_state: "open", consecutive_failures: 2, last_error_code: "rate_limit" },
            { connection_id: "conn-2", model: null, circuit_state: "open", consecutive_failures: 2, last_error_code: "server" },
          ],
          last_fallback: null,
        }}
      />,
    );
    expect(screen.getByText("全部 AI 服务当前不可用")).toBeVisible();
  });

  test("model-level circuit downgrades the role from dual to single", () => {
    render(
      <AiServicesPanel
        catalog={catalog}
        config={configFor([connection("deepseek", "conn-1"), connection("qwen", "conn-2")])}
        status={{
          connections: [
            { connection_id: "conn-1", model: "model", circuit_state: "open", consecutive_failures: 1, last_error_code: "model" },
          ],
          last_fallback: null,
        }}
      />,
    );
    expect(screen.getByText("部分能力异常")).toBeVisible();
    // conn-1 的 fast_text 模型熔断 → 该角色只剩 conn-2 → 单路（非主备）。
    expect(screen.getByText("单路")).toBeVisible();
    expect(screen.queryByText("主备")).not.toBeInTheDocument();
  });
});
