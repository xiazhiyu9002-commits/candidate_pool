# 国内主流 AI 双服务接入与自动故障切换设计

> 修订：2026-09-13。本文已按当前工作区重新审计；后续若与 2026-09-12 的旧描述冲突，以本次修订为准。

## 1. 背景与目标

当前系统的生成式 AI 配置分散在 `Settings`、供应商预设、`runtime.py` 和多个直接发送 HTTP 请求的业务类中。文本、视觉和 Agent 任务共用一个静态模型；保存设置后必须重启；部分调用固定发送 `temperature` 或 `response_format`；系统也没有真正的主备切换。因此，供应商限流、服务繁忙、模型下架、余额不足或 API Key 失效都会直接导致当前任务失败。

2026-09-13 当前代码审计还确认了以下基线，实施时必须保留：

- 搜索索引已升级到 `INDEX_SCHEMA_VERSION="8"`、`INDEX_CHUNK_VERSION="5"`，十种检索/匹配入口、词级检索、查询计划和可选语义改写已经存在。
- 搜索服务已经使用单一绝对 deadline、子预算和取消传播；AI 路由不得吞掉 `CancelledError`，也不能在预算耗尽后再调用备用服务。
- 候选人/JD 画像生成已支持 `instruction` 与 `previous` 增量更新；迁移 AI 客户端时必须保留现有方法签名与提示词语义。
- 简历解析已有 `check_parsed_resume`、`E_PARSE_INCOMPLETE`、失败项批量重解析和默认视觉重解析流程；生成式 AI 网关不能绕过这些业务质量门槛。
- 简历和 JD 的结构化字段已经包含 `direction`，迁移解析提示词时必须以实施时源码为准完整搬迁，不能使用旧方案中的提示词快照。

本次改造建立一套面向无技术背景个人猎头的生成式 AI 配置：

- 新安装默认推荐 DeepSeek，用户只填一个 API Key 即可使用。
- 设置页允许配置一个或两个 AI 服务，明确推荐配置两个不同供应商。
- 配置两个服务时，第一项为主服务，第二项为备用服务；主服务暂时不可用时自动切换。
- 内置 DeepSeek、Kimi 开放平台、Kimi Code 订阅、通义千问、智谱 GLM、硅基流动六类预设，并保留自定义 OpenAI-compatible 入口。
- 业务代码只声明“快速文本、思考文本、视觉”能力，不依赖具体供应商和模型名。
- 模型或推荐项变化优先通过模型发现、能力探测和可更新目录解决，不要求升级整个应用。

## 2. 本次范围

本次只改造生成式 AI：

- `fast_text`：简历/JD/组织结构化解析、搜索改写、标签与线索提取、画像生成。
- `reasoning_text`：BD Agent 规划、证据综合、复杂分析和匹配解释。
- `vision`：扫描版 PDF、图片简历和图片 JD 的 OCR/结构化识别。

明确不在本次范围：

- 不更换 Embedding 模型。
- 不修改 Rerank 配置和实现。
- 不修改当前搜索索引元数据（审计基线为 Schema v8 / chunk v5）、LanceDB schema、关键词检索、向量检索、混合检索、查询计划、方向筛选和匹配阈值。
- 不改 Tavily、SerpApi、邮箱、备份和招聘业务数据库结构。
- 不接入 OpenAI Responses API 或 Anthropic Messages API；首版统一使用各供应商的 OpenAI Chat Completions 兼容接口。
- 不提供 API Key 代购、充值、额度查询或账号共享功能。

## 3. 产品决策

### 3.1 默认与主备

- 新用户打开 AI 设置时，主服务预选 DeepSeek，但只有填写并验证 Key 后才启用。
- 用户可只保存一个服务；界面显示“可以使用，但没有备用保护”。
- 用户可添加第二个服务；界面使用“备用 AI 服务”而不是“API 2”等技术名称。
- 推荐主备来自不同供应商。允许同一供应商使用两个连接，但必须提示这不能防止供应商整体故障，也不能用来规避供应商限流或服务条款。
- 三项能力分别生成主备路由。某连接不支持视觉时，视觉路由自动跳过它，不因文本主服务缺少视觉而让整个配置失效。

推荐的初始组合是：

1. 主服务：DeepSeek。
2. 备用服务：Kimi 开放平台、通义千问、智谱 GLM 或硅基流动中的任意一家。

### 3.2 Kimi 两类账号必须分离

系统将 Kimi 分成两个不同的 `provider_id`：

- `kimi_open`：Kimi 开放平台，Base URL 为 `https://api.moonshot.cn/v1`，按量付费，允许产品集成、后台任务和批量任务。
- `kimi_code`：Kimi Code 会员订阅，OpenAI-compatible Base URL 为 `https://api.kimi.com/coding/v1`，Key 与开放平台不互通。

`kimi_code` 标记为 `interactive_only`：

- 不进入邮箱自动解析、批量回填、失败任务批量重跑和定时任务路由。
- 只有显式标记为前台用户触发的请求可以选择它。
- 设置页展示“订阅版受使用范围和会员额度限制，不建议作为招聘系统的默认备用服务”。
- 用户启用前必须勾选一次确认；系统不得伪造或篡改客户端身份标识。
- HTTP 402 映射为“会员状态/订阅额度异常”，与开放平台的“余额不足”分开显示。

正式的双服务保障不应依赖 `kimi_code`；默认推荐 Kimi 开放平台或其他产品 API。

## 4. 用户体验

AI 设置默认页只显示：

- 总状态：未配置、单服务可用、双服务保护、部分异常、全部不可用。
- 主服务卡片。
- 可选的备用服务卡片。
- “添加/更换服务”“测试连接”“高级设置”三个动作。

添加服务采用四步向导：

1. 选择供应商；DeepSeek 位于第一项并带“推荐”标签。
2. 粘贴 API Key；普通预设不显示 Base URL 和模型 ID。
3. 自动发现模型并探测文本、结构化 JSON、思考开关和视觉能力。
4. 显示系统分配结果并保存；密钥输入框保存后立即清空。

当只配置一个服务时，显示非阻塞提示“建议添加备用服务”。配置两个服务时显示“主服务优先，异常时自动切换到备用服务”。

发生切换后，本次业务正常完成，同时显示类似“DeepSeek 当前繁忙，本次已由通义千问完成”的非阻塞消息。设置页记录最近一次切换时间和原因，但不得记录简历、JD、模型思考内容或完整上游响应。

高级设置允许：

- 调整主备顺序。
- 为快速、思考和视觉能力选择模型。
- 自定义 OpenAI-compatible Base URL 与模型 ID。
- 为自定义连接选择参数兼容风格：`standard`、`deepseek`、`qwen`、`zhipu`、`siliconflow` 或 `kimi`。
- 手动刷新模型目录和重新检测。

## 5. 核心模型

### 5.1 连接

`AiConnection` 表示一个用户授权的供应商连接：

- `id`：稳定 ID，迁移和更新时不随显示名改变。
- `provider_id`：`deepseek`、`kimi_open`、`kimi_code`、`qwen`、`zhipu`、`siliconflow`、`custom_openai`。
- `display_name`：面向用户的名称。
- `encrypted_api_key`：仅存在磁盘配置，API 永不返回。
- `base_url_override`：仅自定义或高级设置使用。
- `models`：快速、思考、视觉三个模型 ID。
- `enabled`：是否进入路由。
- `usage_policy`：`product` 或 `interactive_only`，由预设决定，不能由前端绕过。

配置最多保存两个启用连接，顺序就是全局主备顺序。每个能力的实际目标列表由“连接顺序 + 模型能力 + 使用场景”计算。

### 5.2 任务角色与执行场景

`GenerationRequest` 必须同时携带：

- `role`：`fast_text`、`reasoning_text` 或 `vision`。
- `task_kind`：稳定业务标签，如 `resume_parse`、`jd_parse`、`query_rewrite`、`bd_plan`、`bd_synthesis`、`org_parse`、`mail_resume_gate`。
- `execution_context`：`interactive`、`background` 或 `batch`。
- `reasoning`：`off`、`auto` 或 `required`，以及可选 `low/high/max` 强度。
- `output_mode`：`text` 或 `json`。
- `deadline_monotonic`：可选的绝对单调时钟截止时间；存在时主、备两次尝试共享同一预算。
- 消息与可选图片内容。

业务层不能发送供应商专有参数。供应商适配器把统一请求转换为对应格式。

### 5.3 供应商目录与模型档案

应用内置声明式目录，至少包含：

- 固定官方 Base URL。
- OpenAI Chat Completions 路径。
- 模型列表路径是否可用。
- 当前推荐的快速、思考、视觉模型。
- 模型是否支持视觉、JSON、思考开关、思考强度和不支持的常见参数。
- 官方帮助与 Key 申请链接。
- Kimi Code 的使用限制。

初始目录以实施当天的官方文档复核结果为准。目录不是能力真相：连接保存前仍必须做真实最小探测。

为了降低应用不升级带来的影响，目录采用三层来源：

1. 安装包内置目录，保证离线可配置。
2. 最近一次验证成功的本地目录缓存。
3. 可选的 HTTPS 远程签名目录，只允许更新模型 ID、能力声明、弃用状态和官方链接；不能下发代码或任意 Header。

即使没有远程目录，模型发现和高级设置中的手动模型 ID 仍能接入新模型。若供应商改变认证方式或彻底改变协议，仍需要应用升级；方案不承诺永久兼容未知协议。

截至 2026-09-13 的内置首选只作为离线种子，不作为永久真相：

| 入口 | 快速文本 | 思考文本 | 视觉 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek-v4-flash` | `deepseek-v4-pro` | `deepseek-v4-flash-vision-exp` |
| Kimi 开放平台 | `kimi-k2.6` | `kimi-k3` | `kimi-k2.6` |
| Kimi Code | `kimi-for-coding` | `kimi-for-coding` | 仅探测成功且为前台交互时可选 |
| 通义千问 | `qwen3.8-flash` | `qwen3.8-max` | `qwen3.8-flash` |
| 智谱 | `glm-4.7-flashx` | `glm-5.3` | `glm-5.3-flash` |
| 硅基流动 | 以 `/models` 发现结果优先；离线种子使用当前仍在服务的 DeepSeek V4 / GLM 视觉模型 | 同左 | 同左 |

选择上述组合的原因是“快速请求必须能关闭思考”。例如 Kimi K3 始终思考，因此只进入 `reasoning_text`，不能因为它版本更新就错误地承担 `fast_text + reasoning=off`。模型档案应声明允许的思考模式集合和强度集合，而不是仅用一个模糊的 `supports_reasoning` 布尔值。连接保存前仍以 `/models` 和真实最小探测纠正目录；目录中已下线模型必须标记弃用，不能继续推荐。

## 6. 请求适配与结构化输出

所有预设首版都通过 OpenAI Chat Completions 调用，但可选字段由模型档案决定：

- DeepSeek：按模型能力发送 `thinking.type` 和 `reasoning_effort`。
- Kimi 开放平台：对支持切换的模型发送 `thinking`；需要思考强度时发送 `reasoning_effort`。
- Kimi Code：按订阅模型发送支持的思考强度，保留真实客户端身份。
- 通义千问：发送 `enable_thinking`，多轮请求需要时按模型要求处理 `reasoning_content`。
- 智谱：发送 `thinking.type` 及模型支持的强度字段。
- 硅基流动：发送模型明确支持的 `enable_thinking` 与 `thinking_budget`。
- 自定义 OpenAI：默认只发送标准字段，只有探测确认或用户选择兼容风格后才发送扩展字段。

结构化输出顺序：

1. 模型档案声明支持 JSON Schema 时使用 JSON Schema。
2. 否则使用 JSON Object。
3. 否则依赖严格 JSON 提示词并在本地用 Pydantic 校验。

结构化提取默认使用 `fast_text + reasoning=off`；复杂规划使用 `reasoning_text + reasoning=required`。不再无条件发送 `temperature=0` 或 `response_format`。

响应只读取最终 `message.content`。`reasoning_content` 不写日志、不进入数据库、不返回前端，也不能在 `content` 为空时替代最终答案。

当前系统的生成调用均为单轮业务任务。通义千问等模型不得默认启用“保留历史思考”；若未来新增多轮 Agent 协议，必须另行设计受控的内存态消息回传，不能为了兼容多轮而把思考内容写入数据库。Kimi K3、仅思考模型和可关闭思考模型必须由能力档案区分：`reasoning=off` 只能选支持关闭思考的模型，`reasoning=required` 只能选已探测支持思考的模型。

## 7. 自动故障切换

### 7.1 单次请求算法

1. 根据角色、执行场景和能力档案生成最多两个候选目标。
2. 先调用主目标。
3. 主目标成功则直接返回。
4. 主目标产生“可切换错误”时记录脱敏诊断；仅当请求未取消且共享 deadline 尚有剩余时间时，才调用备用目标一次。
5. 备用目标成功则返回结果并附带 `fallback_used=true` 与脱敏切换摘要。
6. 两个目标均失败时返回最后错误，并在详情中列出两个供应商的错误类别，不含输入和 Key。

每个业务请求最多进行两次远程生成调用，不进行无限重试，也不在备用失败后重新回到主服务。业务数据只有在最终响应解析、校验成功后才写入数据库。

外层任务取消必须原样向上传播，不能被宽泛的 `except Exception` 转换为可切换错误。适配器每次请求的超时取“自身上限”和“共享 deadline 剩余时间”的较小值；主服务用尽总预算后备用调用次数必须为零。

### 7.2 可切换错误

以下情况允许切换到用户配置并授权的备用服务：

- DNS、连接、TLS、读取超时等网络错误。
- HTTP 408、429、500、502、503、504。
- HTTP 401/403：Key 失效或权限不足；切换后同时将主连接标记异常。
- HTTP 402：余额不足或会员额度异常。
- 明确的模型不存在、模型下架、模型无权限或能力不支持。
- 上游返回空正文、响应结构错误或 JSON 结果无法通过本地 Schema 校验。

以下情况不切换：

- 本地输入校验失败。
- 文件过大、图片格式不支持、上下文明确超过限制。
- 内容安全/合规拒绝。
- 用户取消任务。
- 共享 deadline 已耗尽。
- 配置中的 Base URL 不是允许的 HTTPS 地址。

必须区分两类“校验失败”：

- 传输/协议/JSON Schema 失败属于供应商输出不可用，可以触发一次主备切换。
- `check_parsed_resume` 判定的 `E_PARSE_INCOMPLETE` 属招聘业务质量门槛，不在通用路由器中当作供应商故障；继续走现有“不合格 → 用户或批量任务发起视觉重解析”流程，避免无提示地重复计费和绕过质量控制。

### 7.3 熔断与自动回切

路由器在内存中维护连接健康状态：

- 429：优先采用 `Retry-After`；没有时冷却 60 秒，最长不超过 5 分钟。
- 网络、408、5xx：冷却 30 秒；连续三次失败后冷却 2 分钟。
- 401/403：冷却 15 分钟，保存新 Key 或手动探测成功立即恢复。
- 402：冷却 5 分钟。
- 模型不存在/无权限：该模型保持不可用，直到重新发现模型、修改配置或探测成功。

冷却期间有备用服务时直接使用备用。冷却结束后的下一次合格请求对主服务做一次半开探测；成功后自动恢复主服务优先级，失败则重新进入冷却。没有备用服务时不因熔断永久阻断用户，冷却结束可再次尝试主服务。

运行时同时存在 API 请求、任务 worker 和调度器，熔断状态变更与半开探测占用必须具备并发保护，确保同一连接同一时刻最多一个半开探测，且任何竞态都不能突破“每个业务请求最多两次远程调用”。

## 8. 配置存储与迁移

生成式 AI 配置使用独立文件：

`data_root/config/ai-providers.json`

磁盘结构版本为 `schema_version: 1`，包含最多两个连接、三项能力的主备路由、最后成功探测摘要和目录版本。API Key 继续使用现有 `EncryptionService` 的 AES-256-GCM 加密。

候选人、JD、组织、任务和搜索数据库不增加表或字段。

首次启动时若新文件不存在，执行一次幂等迁移：

1. 先按当前实际优先级迁移旧 `text_*` 连接，避免升级后突然换模型。
2. 再迁移旧 DeepSeek 连接作为主或备用。
3. 若仍有空位，再迁移旧 SiliconFlow 文本连接。
4. 视觉配置合并到相同 Base URL 与 Key 的连接；无法合并时仅保留为对应连接的视觉模型。
5. 旧 Embedding、Rerank、Tavily、SerpApi 和邮箱字段完全不动。
6. 新文件存在后不再重复迁移；旧字段保留一个兼容周期，旧前端仍可读取但新前端不再写入生成式 AI 字段。

所有 JSON 保存采用同目录临时文件、刷新写盘和原子替换，并保留单份 `.bak`。

## 9. 后端服务与热加载

新增 `AiProviderManager`，持有：

- 当前不可变配置快照。
- `AiProviderRouter`。
- 每个连接的适配器。
- 熔断状态和最近切换摘要。
- 独立的共享 `httpx.AsyncClient`。
- 不含密钥的 `routing_revision/cache_identity`，供搜索改写缓存识别路由配置版本。

业务服务注入稳定的 `GenerationClient` 代理。保存 AI 配置时：

1. 校验并加密保存候选配置。
2. 构建新适配器并完成探测。
3. 在锁内原子替换快照。
4. 下一次业务请求立即使用新配置，无需重启应用。

`SemanticQueryRewriter` 当前缓存键依赖单个客户端的 `.model`。改造后它必须改用 task client 暴露的稳定 `cache_identity`（至少包含配置 revision、任务类型、角色和已解析模型 ID，不包含 Key），这样主备或模型热切换后不会错误复用旧路由缓存。搜索传入的 deadline 与外层取消仍需贯穿 task client、router 和 adapter。

现有 Embedding/Rerank provider bundle 与搜索索引生命周期保持原样，不并入本次 manager。

## 10. API

新增：

- `GET /api/ai/catalog`：供应商预设、推荐顺序、能力与目录版本。
- `POST /api/ai/catalog/refresh`：刷新可选远程目录。
- `GET /api/ai/config`：返回脱敏主备配置和自动生成的能力路由。
- `PUT /api/ai/config`：保存最多两个连接；省略 Key 表示保留原 Key，显式 `clear_api_key=true` 才删除。
- `POST /api/ai/probe`：使用无个人信息的固定测试数据探测一个尚未保存或已保存连接。
- `GET /api/ai/status`：连接健康、熔断状态、最近切换摘要。

旧 `/api/settings` 继续处理邮箱、搜索、备份相关字段。旧生成式 AI 字段在一个兼容周期内只读迁移，不再由新设置页写入。

## 11. 安全与隐私

- GET 接口、日志、异常、测试快照和前端状态不得包含完整 API Key 或加密密文。
- 探测只使用固定的虚构简历片段、固定 JSON 和内置测试图片，不上传真实候选人资料。
- 备用服务必须由用户主动配置和启用；不能把数据自动发送给未配置供应商。
- 自定义 Base URL 生产环境仅允许 HTTPS；开发测试只允许显式开启的 loopback HTTP。
- 日志只记录内部请求 ID、连接 ID、供应商、模型、错误类别、耗时和是否切换。
- 不记录请求消息、模型最终内容、`reasoning_content` 或上游原始错误正文。

## 12. 验收标准

1. 新用户打开设置页时 DeepSeek 被标记为推荐主服务。
2. 只填一个 DeepSeek Key，完成探测和保存后，快速文本、思考文本和可用视觉能力立即生效。
3. 只配置一个服务时正常使用，同时清楚提示没有备用保护。
4. 配置两个不同供应商后显示“双服务保护已开启”。
5. 主服务出现网络错误、429 或 503 时，本次请求自动由备用服务完成，远程调用总数不超过两次。
6. 主服务 Key 失效、余额不足或模型下架时允许备用完成任务，同时设置页显示主服务异常。
7. 输入错误、内容拒绝、文件过大和用户取消不会触发备用调用。
8. 冷却结束且主服务恢复后，系统自动回切主服务。
9. 快速模型不因强制思考或固定温度失败；思考任务确实发送供应商支持的思考参数；非思考模型正常工作。
10. 视觉请求只发送给已探测支持图片的模型。
11. Kimi 开放平台与 Kimi Code 的 Key、Base URL、错误提示和使用策略完全分离。
12. Kimi Code 不被后台、批量、邮箱或定时任务选中。
13. 保存 AI 配置后无需重启；现有进行中的请求不受快照切换破坏。
14. 旧生成式 AI 设置只迁移一次；Embedding、Rerank、Schema v8/chunk v5、查询计划、十种检索/匹配方式和搜索结果不发生非预期变化。
15. API Key 和思考内容不会出现在响应、日志、备份清单或错误消息中。
16. 新增与受影响的 AI/业务测试必须全部通过；全量测试相对实施前基线不得新增失败。若实施前即可复现时间敏感失败，验收报告必须列出前后同名用例和复现次数，不得宣称“全绿”。
17. 搜索 deadline 到期或任务取消后不会调用备用 AI；路由配置变化后语义改写缓存自动失效。
18. 画像手动增量更新仍保留上一版内容语义；批量回填仍为 batch；`E_PARSE_INCOMPLETE` 与视觉重解析行为保持不变。

## 13. 官方资料基线

- DeepSeek 模型与 OpenAI-compatible API：<https://api-docs.deepseek.com/quick_start/pricing>
- Kimi 开放平台模型：<https://platform.kimi.com/docs/models>
- Kimi K3 能力与始终思考限制：<https://platform.kimi.com/docs/guide/kimi-k3-quickstart>
- Kimi Code 接入及双平台区别：<https://www.kimi.com/code/docs/>
- Kimi Code 使用范围：<https://www.kimi.com/code/docs/kimi-code/community-guidelines.html>
- 通义千问 OpenAI Chat Completions：<https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions>
- 通义千问模型列表：<https://help.aliyun.com/zh/model-studio/text-generation-model>
- 智谱模型概览：<https://docs.bigmodel.cn/cn/guide/start/model-overview>
- 智谱 Chat Completions：<https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8>
- 硅基流动 Chat Completions：<https://docs.siliconflow.cn/docs/api/chat-completions-post>
- 硅基流动模型上下线公告：<https://docs.siliconflow.cn/docs/release-notes/overview>

模型名称和参数支持是可变数据。实施者必须以这些官方页面和真实探测结果校正内置目录，但不得因此扩大本设计的供应商范围。
