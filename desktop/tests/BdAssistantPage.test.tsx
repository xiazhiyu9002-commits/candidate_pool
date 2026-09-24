import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { BdAssistantPage } from "../src/pages/BdAssistantPage";

function renderPage(overrides: Partial<Parameters<typeof BdAssistantPage>[0]> = {}) {
  const noop = vi.fn();
  return render(
    <BdAssistantPage
      bdQuery=""
      onBdQueryChange={noop}
      bdFollowUp=""
      onBdFollowUpChange={noop}
      bdSessionId={null}
      bdLeads={[]}
      bdDegradedReason={null}
      bdLoading={false}
      bdProgress={null}
      bdPoolByLead={{}}
      bdPoolBusyId={null}
      collapsedPool={{}}
      onSearchBd={noop}
      onFollowUpBd={noop}
      onLookupPool={noop}
      onTogglePoolCollapse={noop}
      onOpenExternal={noop}
      onCopyLink={noop}
      onPreviewResume={noop}
      {...overrides}
    />
  );
}

describe("BdAssistantPage 空态", () => {
  test("没有降级原因时显示「暂无线索」", () => {
    renderPage();

    expect(screen.getByText("暂无线索")).toBeInTheDocument();
    expect(screen.queryByText("线索综合失败")).not.toBeInTheDocument();
  });

  test("有降级原因时显示原因，而不是「暂无线索」", () => {
    // 综合失败必须与「确实没搜到」区分开：前者要去看 AI 配置，后者换关键词就行。
    renderPage({ bdDegradedReason: "线索综合失败：没有可用的 AI 供应商（E_AI_NO_PROVIDER）" });

    expect(screen.getByText("线索综合失败")).toBeInTheDocument();
    expect(
      screen.getByText("线索综合失败：没有可用的 AI 供应商（E_AI_NO_PROVIDER）")
    ).toBeInTheDocument();
    expect(screen.queryByText("暂无线索")).not.toBeInTheDocument();
  });

  test("已经拿到线索时不显示任何空态", () => {
    renderPage({
      bdDegradedReason: "线索综合超过 60 秒未返回",
      bdLeads: [
        {
          id: "lead-1",
          source: "agent",
          company_name: "A公司",
          job_title: "大模型工程师",
          posted_time: null,
          salary_range: null,
          level: null,
          requirements: [],
          summary: null,
          url: null,
          status: "新线索",
          confidence: 0.9,
          is_hiring: true,
          evidence: [],
        },
      ],
    });

    expect(screen.getByText("A公司")).toBeInTheDocument();
    expect(screen.queryByText("线索综合失败")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无线索")).not.toBeInTheDocument();
  });
});
