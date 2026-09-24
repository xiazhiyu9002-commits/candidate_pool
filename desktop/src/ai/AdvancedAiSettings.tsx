import { useState } from "react";
import type { RecruitmentApi } from "../App";
import type { AiCatalog, AiConfig, AiConnectionUpdate, AiProviderId, ModelRole, ReasoningEffort } from "./types";
import { providerLabel } from "./AiServicesPanel";
import { Button } from "../components/ui";

const ROLES: { key: ModelRole; label: string }[] = [
  { key: "fast_text", label: "快速文本" },
  { key: "reasoning_text", label: "思考文本" },
  { key: "vision", label: "视觉" },
];

const COMPAT_STYLES = ["standard", "deepseek", "kimi_open", "kimi_code", "qwen", "zhipu", "siliconflow"];

// 档位显示名。**选了档位就等于要求这个模型思考**（见 `reasoning_efforts` 的口径），
// 而思考 token 数几乎线性决定耗时，所以这里只给强度名，把耗时取舍放在下面那行提示里。
const EFFORT_LABELS: Record<string, string> = {
  low: "低",
  high: "中",
  max: "最高",
};

/**
 * 该槽位可选的思考强度档位。
 *
 * 唯一来源是**模型档案声明**（`AiModelProfile.supported_reasoning_efforts`）：
 * 后端保存时按同一份声明校验，前端若自己编档位，使用者选了会被 422 拒掉。
 * 目录外模型（如自定义连接的模型名）没有声明，因此返回空 —— 不给假选项。
 */
function effortOptions(catalog: AiCatalog | null, providerId: string, modelId: string): string[] {
  if (!modelId) return [];
  const preset = catalog?.providers.find((item) => item.provider_id === providerId);
  return preset?.models[modelId]?.supported_reasoning_efforts ?? [];
}

function slotLabel(index: number): string {
  if (index === 0) return "（主）";
  if (index === 1) return "（备）";
  return "（候补）";
}

export interface AdvancedAiSettingsProps {
  api: RecruitmentApi;
  catalog: AiCatalog | null;
  config: AiConfig | null;
  busy?: boolean;
  onSave: (connections: AiConnectionUpdate[]) => Promise<string | null>;
  onRefreshCatalog: () => void;
}

export function AdvancedAiSettings(props: AdvancedAiSettingsProps) {
  const { api, catalog, config, busy, onSave, onRefreshCatalog } = props;
  const connections = config?.connections ?? [];
  const [drafts, setDrafts] = useState<AiConnectionUpdate[] | null>(null);
  const [message, setMessage] = useState("");
  const [changingKey, setChangingKey] = useState<number | null>(null);
  const [newKey, setNewKey] = useState("");

  const rows: AiConnectionUpdate[] = drafts ?? connections.map((c) => ({
    connection_id: c.connection_id,
    provider_id: c.provider_id,
    display_name: c.display_name,
    base_url_override: c.base_url_override,
    parameter_style: c.parameter_style,
    models: { ...c.models },
    reasoning_efforts: { ...c.reasoning_efforts },
    enabled: c.enabled,
  }));

  if (connections.length === 0) {
    return <p className="muted">尚未配置 AI 服务，请先添加主服务。</p>;
  }

  function updateRow(index: number, patch: Partial<AiConnectionUpdate>) {
    const next = rows.map((row, i) => (i === index ? { ...row, ...patch } : row));
    setDrafts(next);
  }

  function moveUp(index: number) {
    if (index <= 0) return;
    const next = [...rows];
    [next[index - 1], next[index]] = [next[index], next[index - 1]];
    setDrafts(next);
  }

  function moveDown(index: number) {
    if (index >= rows.length - 1) return;
    const next = [...rows];
    [next[index], next[index + 1]] = [next[index + 1], next[index]];
    setDrafts(next);
  }

  async function reprobe(index: number) {
    const row = rows[index];
    setMessage("重新探测中…");
    try {
      const report = await api.probeAiConnection({
        provider_id: row.provider_id,
        connection_id: row.connection_id ?? null,
        api_key: row.api_key,  // 待保存的新 Key（若有）；否则后端按 connection_id 复用。
        base_url_override: row.base_url_override ?? null,
        parameter_style: row.parameter_style ?? null,
        models: row.models ?? {},
      });
      updateRow(index, { models: { ...report.role_models }, probe_token: report.probe_token ?? null });
      setMessage("探测完成，请保存以生效。");
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : "探测失败");
    }
  }

  function toggleEnabled(index: number) {
    updateRow(index, { enabled: !rows[index].enabled });
  }

  function removeRow(index: number) {
    setDrafts(rows.filter((_, i) => i !== index));
  }

  function confirmChangeKey(index: number) {
    if (!newKey.trim()) return;
    updateRow(index, { api_key: newKey.trim() });
    setNewKey("");
    setChangingKey(null);
    setMessage("已记录新 API Key，请保存以生效。");
  }

  async function save() {
    const error = await onSave(rows);
    if (error === null) {
      setDrafts(null);
    } else {
      setMessage(error);
    }
  }

  return (
    <div className="section" role="region" aria-label="AI 高级设置">
      <div className="section-head">
        <h3>AI 高级设置</h3>
        <div className="actions">
          <Button variant="secondary" onClick={() => void onRefreshCatalog()}>刷新目录</Button>
          <Button variant="primary" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存高级设置"}</Button>
        </div>
      </div>

      <p className="muted">顺序即主备顺序：最上面的两个服务参与路由（主+备），其余作为候补。保存后生效。</p>

      {rows.map((row, index) => {
        const isCustom = row.provider_id === "custom_openai";
        return (
          <div key={row.connection_id ?? index} className="row-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{providerLabel(row.provider_id)}{slotLabel(index)}</strong>
              <div className="actions">
                <Button variant="ghost" size="sm" disabled={index === 0} onClick={() => moveUp(index)}>上移</Button>
                <Button variant="ghost" size="sm" disabled={index === rows.length - 1} onClick={() => moveDown(index)}>下移</Button>
                <Button variant="ghost" size="sm" onClick={() => toggleEnabled(index)}>{row.enabled === false ? "启用" : "停用"}</Button>
                <Button variant="ghost" size="sm" onClick={() => { setChangingKey(index); setNewKey(""); }}>更换 Key</Button>
                <Button variant="ghost" size="sm" onClick={() => removeRow(index)}>删除</Button>
                <Button variant="ghost" size="sm" onClick={() => void reprobe(index)}>重新探测</Button>
              </div>
            </div>

            {changingKey === index && (
              <div className="form-grid" style={{ marginTop: 8 }}>
                <input
                  value={newKey}
                  onChange={(e) => setNewKey(e.target.value)}
                  placeholder="新 API Key"
                  aria-label="新 API Key"
                  type="password"
                />
                <Button variant="primary" size="sm" onClick={() => confirmChangeKey(index)}>确认更换</Button>
              </div>
            )}

            {isCustom && (
              <div className="form-grid" style={{ marginTop: 8 }}>
                <input
                  value={row.base_url_override ?? ""}
                  onChange={(e) => updateRow(index, { base_url_override: e.target.value })}
                  placeholder="Base URL（仅 HTTPS）"
                  aria-label="Base URL"
                />
                <select
                  aria-label="兼容风格"
                  value={row.parameter_style ?? "standard"}
                  onChange={(e) => updateRow(index, { parameter_style: e.target.value })}
                >
                  {COMPAT_STYLES.map((style) => <option key={style} value={style}>{style}</option>)}
                </select>
              </div>
            )}

            <div className="form-grid" style={{ marginTop: 8 }}>
              {ROLES.map(({ key, label }) => {
                const modelId = row.models?.[key] ?? "";
                const options = effortOptions(catalog, row.provider_id, modelId);
                const effort = row.reasoning_efforts?.[key] ?? "";
                return (
                  <div key={key} className="field">
                    <label>{label}模型</label>
                    <input
                      value={modelId}
                      onChange={(e) => {
                        const nextModel = e.target.value;
                        // 换模型时丢掉新模型不再支持的档位：留着会在保存时被后端 422 拒掉，
                        // 而使用者看到的只是「保存失败」，无从知道是档位不匹配。
                        const nextEfforts = { ...row.reasoning_efforts };
                        const chosen = nextEfforts[key];
                        if (chosen && !effortOptions(catalog, row.provider_id, nextModel).includes(chosen)) {
                          delete nextEfforts[key];
                        }
                        updateRow(index, {
                          models: { ...row.models, [key]: nextModel || undefined },
                          reasoning_efforts: nextEfforts,
                        });
                      }}
                      placeholder="模型 ID"
                      aria-label={`${label}模型`}
                    />
                    {options.length > 0 && (
                      <select
                        aria-label={`${label}思考强度`}
                        title="选了档位就等于该模型按此强度开启思考；不选则跟随模型默认（最快）。"
                        value={effort}
                        onChange={(e) => updateRow(index, {
                          reasoning_efforts: {
                            ...row.reasoning_efforts,
                            [key]: e.target.value ? (e.target.value as ReasoningEffort) : undefined,
                          },
                        })}
                      >
                        <option value="">强度：跟随模型默认</option>
                        {options.map((option) => (
                          <option key={option} value={option}>{EFFORT_LABELS[option] ?? option}</option>
                        ))}
                      </select>
                    )}
                    {options.length === 0 && (
                      <small className="muted">
                        {modelId ? "该模型未声明思考强度档位" : "填入模型后可设思考强度"}
                      </small>
                    )}
                  </div>
                );
              })}
            </div>

            {ROLES.some(({ key }) => effortOptions(catalog, row.provider_id, row.models?.[key] ?? "").length > 0) && (
              <p className="muted" style={{ marginTop: 4 }}>
                选了强度就等于让该模型按这个强度开思考：思考 token 越多越慢（实测同一段解析提示词，
                最高档的耗时是低档的数倍，而产出字段往往一样）。不选则跟随模型默认，也就是最快的那条路。
                AI 智能解析（查询框的自然语言解析）固定关思考优先，不受这里影响。
              </p>
            )}
          </div>
        );
      })}

      {message && <p role="status" className="muted">{message}</p>}
    </div>
  );
}
