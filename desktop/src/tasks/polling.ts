const terminal = new Set(["SUCCESS", "FAILED", "DEAD_LETTER", "CANCELLED", "PAUSED"]);

type StatusCarrier = { status: string };

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException("Stopped", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
  });
}

/**
 * Track durable work until it finishes, is paused, or the owning UI goes away.
 *
 * 默认固定间隔；给了 ``backoff`` 就按档位递进（长任务不再每秒打一次接口），
 * 给了 ``timeoutMs`` 就设总时长上限——超时不算失败，交给 ``onTimeout`` 提示即可。
 */
export async function waitForTask<T extends StatusCarrier>(
  getTask: () => Promise<T>,
  onStatus: (task: T) => void,
  options: {
    signal: AbortSignal;
    interval?: number;
    /** 退避档位（毫秒）：依次递进，超出后固定用最后一档。 */
    backoff?: number[];
    /** 总时长上限（毫秒）：超出后停止轮询并回调 onTimeout。 */
    timeoutMs?: number;
    onError?: (error: unknown) => void;
    onTimeout?: (task: T) => void;
  },
): Promise<T> {
  const startedAt = Date.now();
  let step = 0;
  while (true) {
    options.signal.throwIfAborted();
    try {
      const task = await getTask();
      options.signal.throwIfAborted();
      onStatus(task);
      if (terminal.has(task.status)) return task;
      if (options.timeoutMs != null && Date.now() - startedAt > options.timeoutMs) {
        options.onTimeout?.(task);
        return task;
      }
    } catch (error) {
      options.signal.throwIfAborted();
      options.onError?.(error);
    }
    const delay = options.backoff?.length
      ? options.backoff[Math.min(step, options.backoff.length - 1)]
      : options.interval ?? 1500;
    step += 1;
    await pause(delay, options.signal);
  }
}
