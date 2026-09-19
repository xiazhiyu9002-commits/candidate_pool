import type { AiConnectionUpdate } from "./types";

/**
 * 把新连接并入现有连接列表（最多两个槽位）。
 *
 * 存在禁用槽位时替换它，而非 append 后 slice(0,2) 静默丢弃新连接。
 */
export function mergeAiConnection(
  current: AiConnectionUpdate[],
  connection: AiConnectionUpdate,
): AiConnectionUpdate[] {
  const disabledIndex = current.findIndex((c) => c.enabled === false);
  if (disabledIndex !== -1) {
    return current.map((c, i) => (i === disabledIndex ? connection : c));
  }
  return [...current, connection];
}
