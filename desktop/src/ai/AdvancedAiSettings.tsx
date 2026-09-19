import { useState } from "react";
import type { RecruitmentApi } from "../App";
import type { AiCatalog, AiConfig, AiConnectionUpdate, AiProviderId, ModelRole } from "./types";
import { providerLabel } from "./AiServicesPanel";
import { Button } from "../components/ui";

const ROLES: { key: ModelRole; label: string }[] = [
  { key: "fast_text", label: "快速文本" },
  { key: "reasoning_text", label: "思考文本" },
  { key: "vision", label: "视觉" },
];

const COMPAT_STYLES = ["standard", "deepseek", "kimi_open", "qwen", "zhipu", "siliconflow"];

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
    enabled: c.enabled,
  }));

  if (connections.length === 0) {
    return <p className="muted">尚未配置 AI 服务，请先添加主服务。</p>;
  }

  function updateRow(index: number, patch: Partial<AiConnectionUpdate>) {
    const next = rows.map((row, i) => (i === index ? { ...row, ...patch } : row));
    setDrafts(next);
  }

  function swap() {
    const next = [...rows].reverse();
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
          <Button variant="secondary" disabled={connections.length < 2} onClick={swap}>交换主备顺序</Button>
          <Button variant="secondary" onClick={() => void onRefreshCatalog()}>刷新目录</Button>
          <Button variant="primary" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存高级设置"}</Button>
        </div>
      </div>

      {rows.map((row, index) => {
        const isCustom = row.provider_id === "custom_openai";
        return (
          <div key={row.connection_id ?? index} className="row-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{providerLabel(row.provider_id)}{index === 0 ? "（主）" : "（备）"}</strong>
              <div className="actions">
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
              {ROLES.map(({ key, label }) => (
                <input
                  key={key}
                  value={row.models?.[key] ?? ""}
                  onChange={(e) => updateRow(index, { models: { ...row.models, [key]: e.target.value || undefined } })}
                  placeholder={`${label}模型 ID`}
                  aria-label={`${label}模型`}
                />
              ))}
            </div>
          </div>
        );
      })}

      {message && <p role="status" className="muted">{message}</p>}
    </div>
  );
}
