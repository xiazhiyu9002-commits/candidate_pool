import { useState } from "react";
import type { RecruitmentApi } from "../App";
import type { AiCatalog, AiConnectionUpdate, AiProviderId, ConnectionProbeReport, ModelRole } from "./types";
import { providerLabel } from "./AiServicesPanel";
import { Button } from "../components/ui";

const PROVIDER_ORDER: AiProviderId[] = [
  "deepseek", "kimi_open", "qwen", "zhipu", "siliconflow",
  "kimi_code", "qwen_code", "zhipu_code", "custom_openai",
];

const COMPAT_STYLES = ["standard", "deepseek", "kimi_open", "kimi_code", "qwen", "zhipu", "siliconflow"];

const ROLES: { key: ModelRole; label: string }[] = [
  { key: "fast_text", label: "快速模型" },
  { key: "reasoning_text", label: "思考模型" },
  { key: "vision", label: "视觉模型" },
];

export interface ConnectionWizardProps {
  api: RecruitmentApi;
  catalog: AiCatalog | null;
  slot: "primary" | "secondary";
  onClose: () => void;
  onSave: (connection: AiConnectionUpdate) => Promise<string | null>;
}

export function ConnectionWizard(props: ConnectionWizardProps) {
  const { api, catalog, slot, onClose, onSave } = props;
  const [providerId, setProviderId] = useState<AiProviderId | null>(slot === "primary" ? "deepseek" : null);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [parameterStyle, setParameterStyle] = useState("standard");
  const [roleDrafts, setRoleDrafts] = useState<Partial<Record<ModelRole, string>>>({});
  const [acknowledged, setAcknowledged] = useState(false);
  const [probe, setProbe] = useState<ConnectionProbeReport | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [error, setError] = useState("");

  const providers: AiProviderId[] = slot === "secondary"
    ? [...PROVIDER_ORDER.filter((id) => id !== "deepseek"), "deepseek"]
    : PROVIDER_ORDER;

  const isCustom = providerId === "custom_openai";
  const preset = catalog?.providers.find((p) => p.provider_id === providerId);
  const subscriptionWarning = preset?.subscription_warning ?? null;
  const canProceed = !subscriptionWarning || acknowledged;
  const roleModels = probe?.role_models ?? {};
  const allFailed = !!probe && probe.auth.ok === false && !probe.text.ok && !probe.json.ok && !probe.reasoning.ok && !probe.vision.ok;

  const explicitModels = (Object.fromEntries(
    Object.entries(roleDrafts).filter(([, v]) => v && v.trim() !== "")
  ) as Record<string, string>);

  async function runProbe() {
    if (!providerId) return;
    setProbeBusy(true);
    setError("");
    try {
      const report = await api.probeAiConnection({
        provider_id: providerId,
        api_key: apiKey,
        base_url_override: isCustom ? baseUrl || null : null,
        parameter_style: isCustom ? parameterStyle : null,
        models: isCustom ? explicitModels : {},
      });
      setProbe(report);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "检测失败");
    } finally {
      setProbeBusy(false);
    }
  }

  async function save() {
    if (!providerId || !probe) return;
    if (allFailed || Object.keys(roleModels).length === 0) {
      setError("检测未通过，无法保存：请检查 API Key、模型可用性或网络连接后重新检测。");
      return;
    }
    const error = await onSave({
      provider_id: providerId,
      display_name: providerLabel(providerId),
      api_key: apiKey,
      ...(probe?.probe_token ? { probe_token: probe.probe_token } : {}),
      ...(isCustom ? { base_url_override: baseUrl || null, parameter_style: parameterStyle } : {}),
      models: roleModels,
    });
    if (error === null) {
      // 保存成功后才清空明文 Key。
      setApiKey("");
    } else {
      setError(error);
    }
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" role="dialog" aria-label="添加 AI 服务" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h2>添加 {slot === "primary" ? "主" : "备用"} AI 服务</h2>
          <div className="actions">
            {providerId && !probe && (
              <Button variant="ghost" size="sm" onClick={() => setProviderId(null)}>更换供应商</Button>
            )}
            <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭">×</button>
          </div>
        </div>

        {!providerId && (
          <div className="row-list">
            {providers.map((id) => (
              <button key={id} type="button" className="row-item" onClick={() => setProviderId(id)}>
                <div>
                  <strong>{providerLabel(id)}</strong>
                  {id === "deepseek" && <small>推荐主服务</small>}
                </div>
              </button>
            ))}
          </div>
        )}

        {providerId && subscriptionWarning && (
          <div className="callout">
            <p>{subscriptionWarning}</p>
            <label className="switch">
              <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} aria-label="我已了解使用范围" />
              <span className="slider" />
              <span className="txt">我已了解使用范围</span>
            </label>
          </div>
        )}

        {providerId && !probe && (
          <div className="form-grid" style={{ marginTop: 12 }}>
            <input
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="API Key"
              aria-label="API Key"
              type="password"
            />
            {isCustom && (
              <>
                <input
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="Base URL（仅 HTTPS）"
                  aria-label="Base URL"
                />
                <select
                  aria-label="兼容风格"
                  value={parameterStyle}
                  onChange={(e) => setParameterStyle(e.target.value)}
                >
                  {COMPAT_STYLES.map((style) => <option key={style} value={style}>{style}</option>)}
                </select>
                {ROLES.map(({ key, label }) => (
                  <input
                    key={key}
                    value={roleDrafts[key] ?? ""}
                    onChange={(e) => setRoleDrafts((prev) => ({ ...prev, [key]: e.target.value }))}
                    placeholder={`${label} ID`}
                    aria-label={`${label} ID`}
                  />
                ))}
              </>
            )}
            <Button variant="primary" disabled={!canProceed || probeBusy} onClick={() => void runProbe()}>
              {probeBusy ? "检测中…" : "检测并继续"}
            </Button>
          </div>
        )}

        {probe && (
          <div>
            <div className="metric-grid">
              {(["auth", "text", "json", "reasoning", "vision"] as const).map((capability) => {
                const result = probe[capability];
                return (
                  <div key={capability} className="metric">
                    <small>{capability}</small>
                    <strong className={result.ok ? "ok" : "bad"}>{result.ok ? "正常" : result.error_code ?? "异常"}</strong>
                    {!result.ok && result.suggested_action && <p>{result.suggested_action}</p>}
                  </div>
                );
              })}
            </div>
            {Object.keys(roleModels).length > 0 && (
              <div className="row-list" style={{ marginTop: 12 }}>
                <div className="row-item">
                  <div>
                    <strong>模型分配</strong>
                    {Object.entries(roleModels).map(([role, model]) => (
                      <small key={role}>{role} = {model}</small>
                    ))}
                  </div>
                </div>
              </div>
            )}
            {allFailed && <p className="muted" style={{ color: "#b00020", marginTop: 12 }}>检测未通过，无法保存。</p>}
            <div className="actions" style={{ marginTop: 12 }}>
              <Button variant="primary" disabled={allFailed} onClick={save}>
                {slot === "primary" ? "保存为主服务" : "保存为备用服务"}
              </Button>
            </div>
          </div>
        )}

        {error && <p role="status" className="muted">{error}</p>}
      </div>
    </div>
  );
}
