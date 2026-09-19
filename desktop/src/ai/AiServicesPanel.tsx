import type { AiCatalog, AiConfig, AiStatus, ModelRole } from "./types";
import { Button } from "../components/ui";

export const PROVIDER_LABELS: Record<string, string> = {
  deepseek: "DeepSeek",
  kimi_open: "Kimi 开放平台",
  kimi_code: "Kimi Code 订阅",
  qwen: "通义千问",
  zhipu: "智谱 GLM",
  siliconflow: "硅基流动",
  custom_openai: "自定义 OpenAI 兼容",
};

export function providerLabel(providerId: string): string {
  return PROVIDER_LABELS[providerId] ?? providerId;
}

const ROLES: { key: ModelRole; label: string }[] = [
  { key: "fast_text", label: "快速文本" },
  { key: "reasoning_text", label: "思考文本" },
  { key: "vision", label: "视觉" },
];

const FALLBACK_TTL_SECONDS = 60;

function statusCopy(config: AiConfig | null, status: AiStatus | null): { headline: string; detail?: string } {
  const fallback = status?.last_fallback;
  if (fallback && Date.now() / 1000 - fallback.occurred_at < FALLBACK_TTL_SECONDS) {
    return {
      headline: `${providerLabel(fallback.primary_provider_id)} 当前不可用，本次已由 ${providerLabel(fallback.backup_provider_id)} 完成`,
    };
  }
  const enabled = (config?.connections ?? []).filter((c) => c.enabled);
  if (enabled.length === 0) {
    return { headline: "未配置 AI 服务", detail: "添加一个 AI 服务即可开始解析简历与 JD。" };
  }

  // 结合熔断 circuit_state 展示“部分异常 / 全部不可用”，而非只看连接数量。
  const statusEntries = status?.connections ?? [];
  const connOpen = new Set(statusEntries.filter((s) => s.circuit_state === "open" && s.model === null).map((s) => s.connection_id));
  const modelOpen = statusEntries.filter((s) => s.circuit_state === "open" && s.model !== null);
  const connOpenCount = enabled.filter((c) => connOpen.has(c.connection_id)).length;
  if (enabled.length > 0 && connOpenCount === enabled.length) {
    return { headline: "全部 AI 服务当前不可用", detail: "所有已配置服务均处于冷却/异常状态，请稍后重试或检查 API 配置。" };
  }
  if (modelOpen.length > 0 && connOpenCount === 0) {
    return { headline: "部分能力异常", detail: "部分模型正在熔断冷却，其余能力继续可用。" };
  }
  if (connOpenCount > 0) {
    const downLabel = enabled.filter((c) => connOpen.has(c.connection_id)).map((c) => providerLabel(c.provider_id)).join("、");
    return { headline: "部分 AI 服务异常", detail: `${downLabel} 当前不可用，其余服务继续提供能力。` };
  }

  if (enabled.length === 1) {
    return { headline: "单服务可用", detail: "当前没有备用保护" };
  }
  const sameProvider = enabled[0].provider_id === enabled[1].provider_id;
  if (sameProvider) {
    return { headline: "已配置两个 Key，但同一供应商故障时可能同时不可用" };
  }
  return { headline: "双服务保护已开启" };
}

function roleStatus(config: AiConfig | null, status: AiStatus | null): Record<ModelRole, string> {
  const conns = config?.connections ?? [];
  const statusEntries = status?.connections ?? [];
  const result = {} as Record<ModelRole, string>;
  for (const { key } of ROLES) {
    const serving = conns.filter((c) => {
      if (!c.enabled || !(c.probed_roles ?? []).includes(key)) return false;
      // 该角色所用模型（或整个连接）正在熔断冷却时，不计作可用路由。
      const model = c.models?.[key];
      const cooling = statusEntries.some(
        (s) => s.connection_id === c.connection_id && s.circuit_state === "open" && (s.model === null || s.model === model),
      );
      return !cooling;
    });
    if (serving.length >= 2) result[key] = "主备";
    else if (serving.length === 1) result[key] = "单路";
    else result[key] = "不可用";
  }
  return result;
}

export interface AiServicesPanelProps {
  catalog: AiCatalog | null;
  config: AiConfig | null;
  status: AiStatus | null;
  busy?: boolean;
  message?: string;
  onAdd?: (slot: "primary" | "secondary") => void;
  onOpenAdvanced?: () => void;
}

export function AiServicesPanel(props: AiServicesPanelProps) {
  const { config, status, busy, message, onAdd, onOpenAdvanced } = props;
  const connections = config?.connections ?? [];
  const enabled = connections.filter((c) => c.enabled);
  const copy = statusCopy(config, status);
  // “是否有空位”按连接总数（含停用）判断，与后端 max_length=2 对齐；
  // 否则停用一条后前端会误显示“添加”按钮，新增第三条会被后端 422 拒绝。
  const hasPrimary = connections.length >= 1;
  const hasSecondary = connections.length >= 2;
  const roles = roleStatus(config, status);

  return (
    <div className="section" role="region" aria-label="AI 服务">
      <div className="section-head">
        <h2>AI 服务</h2>
        <div className="actions">
          <Button variant="secondary" onClick={() => onOpenAdvanced?.()}>高级设置</Button>
          {!hasPrimary && (
            <Button variant="primary" onClick={() => onAdd?.("primary")}>添加 AI 服务</Button>
          )}
          {hasPrimary && !hasSecondary && (
            <Button variant="primary" onClick={() => onAdd?.("secondary")}>添加备用 AI 服务</Button>
          )}
        </div>
      </div>

      <p className="muted">{copy.headline}</p>
      {copy.detail && <p className="muted">{copy.detail}</p>}
      {!hasPrimary && (
        <div className="row-list">
          <div className="row-item">
            <div>
              <strong>DeepSeek</strong>
              <small>推荐主服务</small>
            </div>
          </div>
        </div>
      )}

      {enabled.length > 0 && (
        <div className="row-list">
          {enabled.map((connection, index) => (
            <div key={connection.connection_id} className="row-item">
              <div>
                <strong>{providerLabel(connection.provider_id)}</strong>
                <small>{connection.masked_api_key || "未设置密钥"}</small>
              </div>
              {index === 0 && <span className="tag">主服务</span>}
              {index === 1 && <span className="tag">备用服务</span>}
            </div>
          ))}
        </div>
      )}

      {hasPrimary && (
        <div className="metric-grid" style={{ marginTop: 10 }}>
          {ROLES.map(({ key, label }) => (
            <div key={key} className="metric">
              <small>{label}</small>
              <strong className={roles[key] === "不可用" ? "bad" : "ok"}>{roles[key]}</strong>
            </div>
          ))}
        </div>
      )}

      {busy && <p className="muted">正在保存…</p>}
      {message && <p role="status" className="muted">{message}</p>}
    </div>
  );
}
