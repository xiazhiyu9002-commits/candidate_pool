# 给 Trae 的执行提示词：双 AI 服务、国产主流模型兼容与自动故障切换

你现在要在当前 `candidate_pool` 工作区中真正实现“国内主流生成式 AI 双服务接入与自动故障切换”，不是再写一份新方案。

开始前先完整阅读并以这两份文件为执行依据：

1. `docs/superpowers/specs/2026-09-12-dual-ai-provider-failover-design.md`
2. `docs/superpowers/plans/2026-09-12-dual-ai-provider-failover.md`

两份文件已经在 2026-09-13 按当前代码修订。详细接口、文件清单、测试步骤和 11 个 Task 以执行计划为准。旧的 `docs/superpowers/plans/2026-09-11-resilient-ai-provider-architecture.md` 中“生成式 AI 供应商改造”部分已被取代，不得与新计划并行实施。

## 先做只读基线审计

不要立即改代码。先重新读取当前版本的以下文件，避免覆盖刚完成的搜索、画像和解析改动：

- `.trae/search-implementation-progress.md`
- `backend/src/kerui_recruit/runtime.py`
- `backend/src/kerui_recruit/providers/factory.py`
- `backend/src/kerui_recruit/providers/openai_compatible.py`
- `backend/src/kerui_recruit/providers/deepseek.py`
- `backend/src/kerui_recruit/search/rewrite.py`
- `backend/src/kerui_recruit/search/service.py`
- `backend/src/kerui_recruit/resumes/profile.py`
- `backend/src/kerui_recruit/jd/profile.py`
- `backend/src/kerui_recruit/backfill/service.py`
- `backend/src/kerui_recruit/resumes/validity.py`
- `backend/src/kerui_recruit/resumes/pipeline.py`
- `desktop/src/App.tsx`
- `desktop/src/api/client.ts`
- `desktop/src/pages/SettingsPage.tsx`
- `kerui-recruit-sidecar.spec`

确认当前生成式 AI 改造尚未落地：现有业务仍由静态 `OpenAICompatibleClient`、DeepSeek 专用类及多处 `/chat/completions` 直连承担；设置保存仍提示重启。若你发现其中任何文件在本次执行期间被其他任务继续修改，先停下对该文件的编辑，重新读取并合并当前内容，不得用旧快照覆盖。

按执行计划的 Preflight 先跑针对性基线和后端全量基线，把命令、通过数、失败数和完整失败 node ID 写入 `.trae/dual-ai-provider-progress.md`。2026-09-13 审计时，针对性基线为 `44 passed, 1 failed`；失败是 `test_rewrite_cache_misses_on_lexicon_version_change` 把版本硬编码为与当前 `LEXICON_VERSION="2"` 相同。该测试应在改为 `cache_identity` 时修正，但不得弱化缓存失效断言。此前全量记录为 4 个时间敏感搜索失败；你必须重新运行并以新鲜结果为准，不能预设仍是 4 个。

当前目录不是 Git 仓库。每个 Task 后执行 `Test-Path .git`；如果仍为 `False`，不要初始化 Git，不要伪造提交，把变更文件、测试命令和结果追加到 `.trae/dual-ai-provider-progress.md`。

## 必须实现的结果

- 新用户默认推荐 DeepSeek，只填一个 Key 就能用；允许只配置一个服务，也允许配置两个。两个连接按顺序为主服务、备用服务，优先建议不同供应商。
- 内置七个入口：DeepSeek、Kimi 开放平台、Kimi Code 订阅、通义千问、智谱 GLM、硅基流动、自定义 OpenAI-compatible。
- 业务层只声明 `fast_text`、`reasoning_text`、`vision` 和执行场景，不再依赖供应商名或模型名。
- 所有生成式请求集中到一个 Chat Completions 适配器；业务文件不得继续直接 POST `/chat/completions`。
- 主服务只调用一次；仅遇到可切换错误并且任务未取消、共享 deadline 仍有时间时，备用服务再调用一次。单个业务请求远程生成总调用数最多两次。
- 网络、408/429/5xx、鉴权、额度、模型下架以及传输/JSON Schema 无效可切换；输入、策略拒绝、取消、总 deadline 耗尽不切换。
- `asyncio.CancelledError` 必须原样传播。搜索现有绝对 deadline、子预算和取消逻辑必须保留；主服务耗尽搜索预算后不得启动备用服务。
- `check_parsed_resume` 返回的 `E_PARSE_INCOMPLETE` 是业务质量失败，不是通用供应商故障。不要自动向备用服务重复发送简历；继续保留现有“不合格 → `REPARSE_FAILED`/用户触发 → `use_vision=True`”流程。
- 配置热更新后下一次请求立即生效，进行中的请求继续使用旧不可变快照。`SemanticQueryRewriter` 的缓存键从静态 `.model` 改为不含密钥的 `cache_identity`，配置/模型路由变化时必须 cache miss。
- 候选人和 JD 画像必须保留当前 `generate(data, instruction=None, previous=None)` 的增量更新语义；手动重生成是 `interactive`，自动回填是 `batch`。
- 搬迁简历/JD 解析提示词时，从实施时源码完整复制，保留当前 `direction` 及所有其他字段。不要照抄旧计划里的过时提示词，也不要用构造器不兼容的简单 class alias。
- 配置保存到独立、加密、原子替换的 `ai-providers.json`；API、日志、异常、前端状态、备份清单均不得泄露明文 Key、密文、候选人原文、最终模型正文或 `reasoning_content`。
- 设置页默认只展示主/备服务、状态、添加/更换、测试连接和高级设置。普通用户只需选择供应商并粘贴 Key；Base URL、模型 ID 和兼容风格放高级设置。

## 模型目录基线

模型名是可变数据，实施当天必须只用官方文档和供应商 `/models` 结果复核，并执行最小能力探测。内置目录只作为离线种子：

- DeepSeek：快速 `deepseek-v4-flash`，思考 `deepseek-v4-pro`，视觉 `deepseek-v4-flash-vision-exp`。
- Kimi 开放平台：快速/视觉使用仍可关闭思考的 `kimi-k2.6`，思考使用始终思考的 `kimi-k3`。不得把 K3 分配给 `reasoning=off`。
- Kimi Code：保留 `kimi-for-coding` 兼容入口，但放在高级/警告位置，固定 `interactive_only`，不作为默认备用，不用于简历/JD 后台解析、邮箱、批量、视觉重解析或定时任务；Key 和 Base URL 与 Kimi 开放平台完全分离，不能伪造 User-Agent。
- 通义千问：快速/视觉 `qwen3.8-flash`，思考 `qwen3.8-max`；单轮业务调用不要持久化或回传历史思考内容。
- 智谱：快速种子 `glm-4.7-flashx`，思考 `glm-5.3`，视觉 `glm-5.3-flash`；如官方 `/models` 与文档更新不同，以真实探测结果更新目录数据。
- 硅基流动：必须优先发现当前账户可用模型并探测能力，不能依赖容易下线的永久硬编码；保留仍可用的 DeepSeek V4/GLM 视觉模型作为离线种子即可。

模型档案必须明确声明 `supported_reasoning_modes` 和 `supported_reasoning_efforts`，不能只用“是否支持思考”一个布尔值。始终思考、可关闭思考、非思考模型要能被路由器正确区分。任何未知自定义 OpenAI-compatible 模型默认只发送标准字段，探测或用户明确选择兼容风格后才发送供应商扩展参数。

## 严格保护范围

本任务不更换或修改 Embedding、Rerank、Tavily、SerpApi，不改数据库结构，不改匹配阈值、父子 chunk、LanceDB schema、查询计划或十种检索/匹配方式。当前必须保持：

- `INDEX_SCHEMA_VERSION="8"`
- `INDEX_CHUNK_VERSION="5"`
- `query_plan`、关键词 smart/AND/OR、向量/混合语义改写
- `direction` 字段和筛选
- `REPARSE_FAILED`、`E_PARSE_INCOMPLETE`、视觉重解析
- 当前 `kerui-recruit-sidecar.spec` 中的 jieba data/submodule 收集
- `App.tsx` 和 `api/client.ts` 中现有搜索、匹配、回填相关类型与交互

不要顺手修复无关搜索阈值、性能或 UI 问题。不要通过扩大超时、降低断言、跳过或删除测试来制造通过结果。

## 执行方式与完成门槛

严格按执行计划 Task 1 到 Task 11 顺序实施，先写失败测试，再写最小实现，再跑该 Task 的聚焦回归；Task 1–8 通过后再改前端。每个检查点都重新扫描并解释残留的 `OpenAICompatibleClient`、`chat/completions`、旧 Key 字段，同时确认 schema/chunk、query_plan、direction、REPARSE_FAILED 和 E_PARSE_INCOMPLETE 没有被破坏。

最终必须完成：

1. 所有新增与受影响 AI 测试全绿。
2. 前端单测、构建和 AI 设置 E2E 通过。
3. 取消与 deadline 用例证明备用调用数为 0；429 用例证明总调用数恰为 2。
4. 热加载、熔断半开并发、自动回切、Kimi Code 场景限制、Key 脱敏、缓存身份失效均有测试。
5. PyInstaller 打包成功，离线启动可读取七个目录项，同时 jieba 与现有搜索冒烟仍通过。
6. 后端全量相对 Preflight 不得新增失败。若相同的既有时间敏感失败仍能复现，验收报告必须列出前后 node ID 和次数，不得写“全部通过”。
7. 创建 `docs/verification/dual-ai-provider-acceptance.md`，记录基线、最终测试、打包产物 SHA-256、模型目录版本、直接调用扫描、密钥扫描和所有关键调用次数。

持续执行到 Task 11 完成；只有遇到需要用户提供真实 API Key、外部账号授权或会改变上述范围的决定时才停下说明。自动化测试不得使用真实 Key 或真实候选人数据，真实供应商验证应作为单独的人工验收项，不阻塞可完全模拟的实现与测试。
