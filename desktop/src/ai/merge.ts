import type { AiConnectionUpdate } from "./types";

/**
 * 把新连接追加到连接列表末尾。
 *
 * 连接数量不设上限，列表顺序即主备顺序：新服务默认排在末尾作为候补，
 * 需要它参与路由时由用户在高级设置里上移，绝不静默顶替现有主服务。
 */
export function mergeAiConnection(
  current: AiConnectionUpdate[],
  connection: AiConnectionUpdate,
): AiConnectionUpdate[] {
  return [...current, connection];
}
