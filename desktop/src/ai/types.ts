export type AiProviderId =
  | "deepseek"
  | "kimi_open"
  | "kimi_code"
  | "qwen"
  | "qwen_code"
  | "zhipu"
  | "zhipu_code"
  | "siliconflow"
  | "custom_openai";

export type ModelRole = "fast_text" | "reasoning_text" | "vision";
/** 思考强度档位，取值来自模型档案声明的 `supported_reasoning_efforts`。 */
export type ReasoningEffort = "low" | "high" | "max";
export type ProtectionLevel = "none" | "single" | "dual";
export type CircuitState = "closed" | "open" | "half_open";
/** 探测能力项：鉴权、文本、JSON、思考、视觉。 */
export type AiCapabilityKey = "auth" | "text" | "json" | "reasoning" | "vision";

export interface AiConnectionView {
  connection_id: string;
  provider_id: AiProviderId;
  display_name: string;
  masked_api_key: string;
  has_api_key: boolean;
  base_url_override: string | null;
  parameter_style: string | null;
  models: Partial<Record<ModelRole, string>>;
  /** 每个角色槽位的思考强度；缺省表示按模型自身默认行为。 */
  reasoning_efforts?: Partial<Record<ModelRole, ReasoningEffort>>;
  probed_roles: ModelRole[];
  /** 上次探测的五项能力结果；老配置可能为空（按旧口径保存过）。 */
  probed_capabilities?: Partial<Record<AiCapabilityKey, boolean>>;
  probe_version?: number | null;
  enabled: boolean;
}

export interface AiConfig {
  protection_level: ProtectionLevel;
  connections: AiConnectionView[];
  catalog_version: number;
}

export interface AiConnectionUpdate {
  connection_id?: string | null;
  provider_id: AiProviderId;
  display_name: string;
  api_key?: string;
  clear_api_key?: boolean;
  probe_token?: string | null;
  base_url_override?: string | null;
  parameter_style?: string | null;
  models?: Partial<Record<ModelRole, string>>;
  reasoning_efforts?: Partial<Record<ModelRole, ReasoningEffort>>;
  enabled?: boolean;
}

export interface AiConfigUpdate {
  connections: AiConnectionUpdate[];
}

export interface AiModelProfile {
  model_id: string;
  roles: ModelRole[];
  supported_reasoning_modes: string[];
  supported_reasoning_efforts: string[];
  supports_json_schema: boolean;
  supports_json_object: boolean;
  supports_temperature: boolean;
  deprecated: boolean;
}

export interface AiProviderPreset {
  provider_id: AiProviderId;
  label: string;
  base_url: string;
  parameter_style: string;
  allowed_contexts: string[];
  models: Record<string, AiModelProfile>;
  recommended_models: Partial<Record<ModelRole, string>>;
  help_url: string;
  key_help_url: string;
  subscription_warning: string | null;
  deprecated: boolean;
}

export interface AiCatalog {
  version: number;
  default_provider_id: AiProviderId;
  providers: AiProviderPreset[];
}

export interface CapabilityProbe {
  ok: boolean;
  error_code: string | null;
  suggested_action: string | null;
}

export interface AiDiscoveredModel {
  model_id: string;
  source: string;
}

export interface ConnectionProbeReport {
  probe_token?: string;
  auth: CapabilityProbe;
  text: CapabilityProbe;
  json: CapabilityProbe;
  reasoning: CapabilityProbe;
  vision: CapabilityProbe;
  role_models: Partial<Record<ModelRole, string>>;
  models: AiDiscoveredModel[];
}

export interface AiStatusConnection {
  connection_id: string;
  model: string | null;
  circuit_state: CircuitState;
  consecutive_failures: number;
  last_error_code: string | null;
}

export interface AiFallbackSummary {
  primary_provider_id: AiProviderId;
  primary_model: string;
  backup_provider_id: AiProviderId;
  backup_model: string;
  error_code: string;
  occurred_at: number;
}

export interface AiStatus {
  connections: AiStatusConnection[];
  last_fallback: AiFallbackSummary | null;
}
