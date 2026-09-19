/**
 * 方向词表（职业大类 / 职业细分 / 业务方向）。
 *
 * 与后端 `kerui_recruit.direction.policy` 的枚举保持一一对应；后端是唯一权威，
 * 这里只做展示与筛选用的标签映射。新增/改名时必须两侧同时改。
 */

export interface DirectionOption {
  value: string;
  label: string;
}

/** 职业方向大类（单选/多选的最大数量按场景：候选人 ≤2，JD ≤3）。 */
export const CAREER_DIRECTION_OPTIONS: DirectionOption[] = [
  { value: "", label: "全部" },
  { value: "BACKEND", label: "后端" },
  { value: "FRONTEND", label: "前端" },
  { value: "ALGORITHM", label: "算法" },
  { value: "DATA", label: "数据" },
  { value: "OPS", label: "运维" },
  { value: "QA", label: "测试" },
  { value: "PRODUCT", label: "产品" },
  { value: "MANAGEMENT", label: "管理" },
  { value: "OTHER", label: "其他" },
];

/** 职业方向大类（不含「全部」，供多选控件使用）。 */
export const CAREER_DIRECTION_VALUES = CAREER_DIRECTION_OPTIONS.filter((item) => item.value);

/** 职业方向细分 -> 所属大类。细分代码以大类名为前缀。 */
export const SPECIALIZATION_PARENT: Record<string, string> = {
  BACKEND_SERVICE: "BACKEND",
  BACKEND_AI_APPLICATION: "BACKEND",
  BACKEND_FULL_STACK: "BACKEND",
  FRONTEND_WEB: "FRONTEND",
  FRONTEND_CLIENT: "FRONTEND",
  FRONTEND_MINI_H5: "FRONTEND",
  ALGORITHM_TRAINING: "ALGORITHM",
  ALGORITHM_RECSYS: "ALGORITHM",
  ALGORITHM_INFERENCE: "ALGORITHM",
  ALGORITHM_VISION_SPEECH: "ALGORITHM",
  DATA_WAREHOUSE: "DATA",
  DATA_ENGINEERING: "DATA",
  DATA_ANALYSIS: "DATA",
  OPS_INFRA: "OPS",
  OPS_SRE: "OPS",
  OPS_DEVOPS: "OPS",
  QA_AUTOMATION: "QA",
  QA_QUALITY: "QA",
  QA_PERFORMANCE: "QA",
  PRODUCT_PLANNING: "PRODUCT",
  PRODUCT_DESIGN: "PRODUCT",
  PRODUCT_OPERATIONS: "PRODUCT",
  MANAGEMENT_TEAM: "MANAGEMENT",
  MANAGEMENT_TECH: "MANAGEMENT",
};

export const SPECIALIZATION_LABELS: Record<string, string> = {
  BACKEND_SERVICE: "服务端架构",
  BACKEND_AI_APPLICATION: "AI 应用集成",
  BACKEND_FULL_STACK: "全栈交付",
  FRONTEND_WEB: "Web 前端",
  FRONTEND_CLIENT: "客户端",
  FRONTEND_MINI_H5: "小程序与 H5",
  ALGORITHM_TRAINING: "模型训练与微调",
  ALGORITHM_RECSYS: "推荐与搜索算法",
  ALGORITHM_INFERENCE: "推理与部署优化",
  ALGORITHM_VISION_SPEECH: "视觉与语音",
  DATA_WAREHOUSE: "数仓与建模",
  DATA_ENGINEERING: "数据开发与管道",
  DATA_ANALYSIS: "业务分析",
  OPS_INFRA: "基础设施与云原生",
  OPS_SRE: "SRE 与稳定性",
  OPS_DEVOPS: "研发效能与工具链",
  QA_AUTOMATION: "自动化测试",
  QA_QUALITY: "质量保障",
  QA_PERFORMANCE: "性能测试",
  PRODUCT_PLANNING: "产品规划",
  PRODUCT_DESIGN: "产品设计",
  PRODUCT_OPERATIONS: "产品运营",
  MANAGEMENT_TEAM: "团队管理",
  MANAGEMENT_TECH: "技术管理",
};

export const SPECIALIZATION_OPTIONS: DirectionOption[] = Object.keys(SPECIALIZATION_PARENT).map(
  (value) => ({ value, label: SPECIALIZATION_LABELS[value] ?? value }),
);

/** 业务方向（单级多值，≤2）。 */
export const BUSINESS_DIRECTION_OPTIONS: DirectionOption[] = [
  { value: "BANKING", label: "银行" },
  { value: "INSURANCE", label: "保险" },
  { value: "SECURITIES", label: "证券与投资" },
  { value: "PAYMENT", label: "支付与清结算" },
  { value: "RISK_CREDIT", label: "风控与信贷" },
  { value: "ECOMMERCE", label: "电商" },
  { value: "SOCIAL_CONTENT", label: "社交与内容" },
  { value: "LOCAL_LIFE", label: "本地生活与出行" },
  { value: "RETAIL", label: "零售与快消" },
  { value: "MARKETING", label: "营销与广告" },
  { value: "ENTERPRISE_SAAS", label: "企业软件与 SaaS" },
  { value: "DATA_INTELLIGENCE", label: "数据智能与 BI" },
  { value: "SECURITY", label: "安全与合规" },
  { value: "MANUFACTURING", label: "制造与工业" },
  { value: "AUTOMOTIVE", label: "汽车" },
  { value: "ENERGY", label: "能源与电力" },
  { value: "LOGISTICS", label: "物流与供应链" },
  { value: "GOVERNMENT", label: "政务与智慧城市" },
  { value: "HEALTHCARE", label: "医疗健康" },
  { value: "EDUCATION", label: "教育" },
  { value: "REAL_ESTATE", label: "房地产与建筑" },
  { value: "GAMING", label: "游戏" },
  { value: "MEDIA", label: "文娱传媒" },
  { value: "TELECOM_CHIP", label: "通信与芯片" },
  { value: "OTHER", label: "其他" },
];

const CAREER_DIRECTION_LABELS: Record<string, string> = Object.fromEntries(
  CAREER_DIRECTION_OPTIONS.filter((item) => item.value).map((item) => [item.value, item.label]),
);
const BUSINESS_DIRECTION_LABELS: Record<string, string> = Object.fromEntries(
  BUSINESS_DIRECTION_OPTIONS.map((item) => [item.value, item.label]),
);

export function careerDirectionLabel(value: string): string {
  return CAREER_DIRECTION_LABELS[value] ?? value;
}

export function specializationLabel(value: string): string {
  return SPECIALIZATION_LABELS[value] ?? value;
}

export function businessDirectionLabel(value: string): string {
  return BUSINESS_DIRECTION_LABELS[value] ?? value;
}

/** 某职业大类下的全部细分选项（供「大类 -> 细分」联动筛选使用）。 */
export function specializationsOfDirections(directions: readonly string[]): DirectionOption[] {
  if (directions.length === 0) return SPECIALIZATION_OPTIONS;
  const allowed = new Set(directions);
  return SPECIALIZATION_OPTIONS.filter((item) => allowed.has(SPECIALIZATION_PARENT[item.value]));
}
