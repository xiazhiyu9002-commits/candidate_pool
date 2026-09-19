import { afterEach, expect, test, vi } from "vitest";
import { waitForTask } from "../src/tasks/polling";
afterEach(() => vi.useRealTimers());
test("keeps tracking a slow job past thirty seconds and returns final state", async () => {
  vi.useFakeTimers();
  const start = Date.now();
  const get = vi.fn(async () => ({ id: "t", task_type: "PARSE_RESUME", status: Date.now() - start >= 35_000 ? "SUCCESS" : "RUNNING", progress: 0, error_message: null }));
  const progress = vi.fn();
  const result = waitForTask(get, progress, { signal: new AbortController().signal, interval: 1000 });
  await vi.advanceTimersByTimeAsync(36_000);
  expect((await result).status).toBe("SUCCESS");
  expect(get.mock.calls.length).toBeGreaterThan(30);
});
test("surfaces transient network trouble then resumes and stops on cancellation", async () => {
  vi.useFakeTimers();
  const control = new AbortController();
  const failed = vi.fn();
  const get = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ status: "RUNNING" });
  const result = waitForTask(get, () => {}, { signal: control.signal, interval: 1000, onError: failed });
  const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" });
  await vi.advanceTimersByTimeAsync(2000);
  expect(failed).toHaveBeenCalled();
  control.abort();
  await rejected;
  const count = get.mock.calls.length;
  await vi.advanceTimersByTimeAsync(60_000);
  expect(get).toHaveBeenCalledTimes(count);
});


test("backoff slows long polls down instead of hitting the API every second", async () => {
  vi.useFakeTimers();
  const get = vi.fn(async () => ({ status: "RUNNING" }));
  const timedOut = vi.fn();
  const result = waitForTask(get, () => {}, {
    signal: new AbortController().signal,
    backoff: [1000, 2000, 5000],
    timeoutMs: 10_000,
    onTimeout: timedOut,
  });

  await vi.advanceTimersByTimeAsync(14_000);
  await result;

  // 固定 1 秒轮询同样时长会打 14 次；退避后应该在 5 次左右。
  expect(get.mock.calls.length).toBeLessThanOrEqual(6);
  expect(timedOut).toHaveBeenCalledTimes(1);
});


test("timeout returns the last status instead of rejecting", async () => {
  vi.useFakeTimers();
  const get = vi.fn(async () => ({ status: "RUNNING", progress: 30 }));
  const result = waitForTask(get, () => {}, {
    signal: new AbortController().signal,
    interval: 1000,
    timeoutMs: 0,
  });

  await vi.advanceTimersByTimeAsync(1100);
  const final = await result;
  expect(final).toEqual({ status: "RUNNING", progress: 30 });
});
