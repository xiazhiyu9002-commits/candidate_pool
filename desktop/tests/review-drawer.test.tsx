import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ResumeReviewDrawer } from "../src/resumes/ResumeReviewDrawer";
import type { RecruitmentApi, ResumeReview } from "../src/App";

const initial: ResumeReview = { candidate_id: "c", revision_id: "r", status: "FAILED", review_required: true, raw_text: "old text", parsed_data: null, review_data: null, manual_overrides: {}, extraction_diagnostics: {}, error_code: "E_PARSE_INCOMPLETE", error_message: "解析结果不完整，已归为不合格，可重新解析" };

test("renders read-only review with original text and error", () => {
  const close = vi.fn();
  render(<ResumeReviewDrawer api={{} as RecruitmentApi} initialReview={initial} onClose={close} />);
  expect(screen.getByText("解析结果不完整，已归为不合格，可重新解析")).toBeVisible();
  expect(screen.getByText("old text")).toBeVisible();
  expect(screen.queryByRole("button", { name: "重新解析" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(close).toHaveBeenCalledOnce();
});

test("reparses and reloads review evidence", async () => {
  let finish!: () => void;
  const work = new Promise<void>((resolve) => { finish = resolve; });
  const api = { getResumeReview: vi.fn(async () => ({ ...initial, raw_text: "new text", status: "READY" })) } as unknown as RecruitmentApi;
  const onReparse = vi.fn(() => work);
  render(<ResumeReviewDrawer api={api} initialReview={initial} onClose={() => {}} onReparse={onReparse} onForceReparse={() => Promise.resolve()} />);
  fireEvent.click(screen.getByRole("button", { name: "重新解析" }));
  expect(screen.getByRole("button", { name: "解析中…" })).toBeDisabled();
  await act(async () => finish());
  await waitFor(() => expect(screen.getByText("new text")).toBeVisible());
  // 「重新解析」走常规解析（异常页才 OCR），不是强制 OCR。
  expect(onReparse).toHaveBeenCalledWith("r");
});

test("offers force OCR as a separate action for scans and garbled text layers", async () => {
  const api = { getResumeReview: vi.fn(async () => initial) } as unknown as RecruitmentApi;
  const onForceReparse = vi.fn(async () => {});
  render(<ResumeReviewDrawer api={api} initialReview={initial} onClose={() => {}} onReparse={async () => {}} onForceReparse={onForceReparse} />);
  fireEvent.click(screen.getByRole("button", { name: "强制 OCR" }));
  await waitFor(() => expect(onForceReparse).toHaveBeenCalledWith("r"));
});
