# 个人招聘人才库桌面版设计规格

**状态：** 已确认，进入实施  
**日期：** 2026-08-29  
**依据：** `个人招聘人才库与人岗匹配系统设计方案-整合版.md` 及后续部署决策

## 1. 目标与冻结决策

本项目交付一个完全独立的单机招聘工作台。每位业务人员在自己的电脑上保存人才、简历原件、JD、流程、Mapping、BD 线索、索引和备份，不提供账号体系、团队同步、共享数据库或多租户能力。

冻结决策如下：

- 安装形态：Windows 与 macOS 原生安装包，双击安装和启动，不要求 Docker、Python、Node.js、Java 或数据库环境。
- 平台基线：Windows 10/11 x64；macOS Apple Silicon；16GB 内存；SSD；无独立 GPU 要求。
- 容量基线：单机 10 万名候选人，性能验证使用 12 万名候选人及每人 2–4 个检索块。
- 搜索目标：索引 READY 后首屏 P95 不高于 1.5 秒，P99 不高于 2.5 秒；详细推荐理由异步生成。
- 数据边界：各电脑完全独立；网络只用于模型、网页搜索、邮箱和升级检查。
- 底层路线：嵌入式事实库、嵌入式检索投影、本地文件存储和持久化任务表；不使用 PostgreSQL、OpenSearch、Redis、MinIO 或 Docker Compose。
- 模型路线：LLM、Embedding、Reranker 均通过可配置 API 调用，不在本机部署模型。

## 2. 产品范围

### 2.1 人才与简历

- PDF、DOC、DOCX 主动上传与文件夹批量导入。
- 邮箱附件被动入库，支持 IMAP 断线重连、幂等游标和白名单。
- SHA-256 原件去重，候选人、逻辑简历、简历版本和物理 Blob 分离。
- 文本提取、扫描件 OCR、结构化解析、人工修正、版本切换和历史查看。
- 姓名、联系方式、学历、学校、QS、毕业年份、技能、行业、公司、项目、地点、年限等字段维护。
- 软删除、30 天回收站、恢复、到期清理和操作撤销。

### 2.2 JD 与人岗匹配

- 文本、Word 和 Excel JD 导入、结构化解析、版本管理、启用与关闭。
- 关键词、自然语言、单 JD 和批量 JD 四类匹配入口。
- 硬条件过滤、BM25、向量召回、RRF、API 重排和证据化理由。
- 结果筛选、收藏、人工标记、推荐、面试流程和 Excel 导出。
- 候选人与 JD 关联流程、阶段事件、漏斗和项目看板。

### 2.3 Mapping、提醒与 BD

- 公司、部门、人员树；拖拽、文本快速建树、快照和 Excel/PDF 导出。
- 早间追反馈与晚间未处理提醒；模板可编辑。
- 以人找岗、以岗找公司、搜索计划、来源留痕、去重和人工核验。
- BD 搜索使用供应商适配层；首选 Tavily，SerpApi 可替换；不接入已停用的 Bing Web Search API。

### 2.4 本地运维

- 首次启动向导、API 测试、数据目录选择、资源检查和检索预热。
- 任务中心、失败重试、暂停、继续、取消和崩溃恢复。
- 自动增量备份、加密便携备份、恢复、数据迁移、诊断包和健康检测。
- Windows/macOS 分平台签名、增量升级、数据库迁移和索引版本切换。

## 3. 总体架构

```text
Tauri 2 / React 桌面界面
          │ 本机随机端口 + 临时会话令牌
          ▼
FastAPI 本地 Sidecar ─────► Provider Adapters ─────► 外部 API
          │
          ├────► SQLite 事实库 / 任务表
          ├────► LanceDB 检索投影
          └────► 哈希分片原件目录
                       ▲
后台 Worker ───────────┘
```

### 3.1 桌面层

Tauri 2 负责窗口、托盘、全局快捷键、Sidecar 生命周期、单实例锁、文件关联、安装、签名和升级。React + TypeScript 负责业务界面。桌面层不能直接访问数据文件，只能调用本地 API。

### 3.2 本地后端

Python 3.12 FastAPI Sidecar 负责领域逻辑、事务、校验、文件处理、检索编排、导出、Provider 调用和健康检查。Sidecar 只绑定 `127.0.0.1` 的随机端口；Tauri 启动时生成短期会话令牌并注入，所有请求必须携带令牌。

### 3.3 事实库

SQLite 是唯一业务事实来源。使用 SQLAlchemy 2、Alembic、外键约束、WAL 模式、单写入器和短事务。运行时固定到已修复 WAL 并发问题的 SQLite 安全版本。读操作可并发，所有写操作由后端写入调度器串行提交。

### 3.4 检索投影

LanceDB 保存候选人检索块、结构化过滤字段、全文索引和向量索引。它是可重建投影，不是事实来源。所有投影行携带 `candidate_id`、`resume_revision_id`、`index_generation`、`model_version` 和内容哈希。

如 12 万数据验证不达标，只替换 `SearchIndex` 接口实现，不改变 SQLite Schema、API 或界面。

### 3.5 原件库

原件以 `blobs/{h0h1}/{h2h3}/{sha256}.{ext}` 保存。数据库记录原始文件名、展示文件名、MIME、大小、哈希、首次入库时间和引用计数。相同二进制只保存一次。

## 4. 数据目录与安全

默认目录：

- Windows：`%LOCALAPPDATA%\KeRuiRecruit\`
- macOS：`~/Library/Application Support/KeRuiRecruit/`

结构：

```text
KeRuiRecruit/
├─ db/recruit.sqlite3
├─ search/
├─ blobs/
├─ exports/
├─ backups/
├─ logs/
├─ temp/
└─ config/
```

禁止选择系统目录、网络盘、OneDrive、iCloud、Dropbox 或其他同步目录。设置页支持维护模式迁移：复制到新目录、校验数量与哈希、健康检查、原子切换，失败继续使用旧目录。

API Key 和系统主密钥分别存入 Windows Credential Manager 与 macOS Keychain。数据库中的身份证号、手机号等高敏字段使用 AES-256-GCM 字段级加密；日志、诊断包和错误信息必须脱敏。服务不得绑定 `0.0.0.0`。

## 5. 核心数据模型

主要实体：

- `candidate`：稳定候选人主档案。
- `resume_document`：候选人的逻辑简历。
- `resume_revision`：不可变简历版本、解析结果和当前版本标记。
- `blob`：物理原件元数据和引用计数。
- `candidate_contact`：加密联系方式与置信度。
- `candidate_education`、`candidate_experience`、`candidate_project`、`candidate_skill`：结构化经历。
- `jd`、`jd_revision`、`jd_requirement`：岗位及版本。
- `match_run`、`match_result`：匹配批次和结果快照。
- `case`、`stage_event`：候选人-JD 流程和事件。
- `mapping_project`、`mapping_node`、`mapping_snapshot`：Mapping 树与快照。
- `bd_search_run`、`bd_lead`：BD 搜索与来源。
- `task`、`task_event`：持久化任务及事件。
- `correction_log`：人工修改和可撤销记录。
- `app_setting`、`schema_version`、`index_version`：配置与版本。

所有可删除业务实体使用 `deleted_at` 软删除。所有人工修改记录旧值、新值、实体版本和时间；撤销使用乐观并发检查。

## 6. 入库与解析数据流

1. API 接收文件或目录清单，计算 SHA-256，校验类型和大小。
2. 在 SQLite 事务中创建/复用 Blob、简历版本和 `PENDING` 任务。
3. 原件写入临时文件，`fsync` 后原子移动到 Blob 路径。
4. Worker 提取文本；可读 PDF/DOCX 本地提取，扫描件进入 OCR Provider，DOC 进入受控转换器。
5. LLM Provider 按 JSON Schema 返回结构化简历；服务端完成类型校验、标准化和置信度处理。
6. 写入结构化事实表并生成 2–4 个检索块。
7. Embedding Provider 批量生成向量，写入新的索引 generation。
8. 抽样校验成功后将该简历版本切换为 READY；失败保留原件和错误，可重试。

批量入库支持批次、暂停、继续、断点续传和速率限制。任务幂等键由任务类型、内容哈希和模型版本组成。

## 7. 检索与匹配

### 7.1 查询理解

规则解析器优先识别年限、学历、地点、学校等级、QS、状态等硬条件；LLM 只处理歧义和自然语言改写。API 不可用时仍可使用表单过滤和关键词检索。

### 7.2 主链路

```text
查询 → 规则/意图解析
     ├─ 硬条件 → 预过滤
     ├─ 实体词 → BM25
     └─ 语义 → Embedding ANN
BM25 + ANN → RRF → Top 100 → Reranker → Top N → SQLite 补全
```

硬条件必须在候选集合生成时生效。Reranker 超时则返回 RRF 结果并标注降级；长推荐理由异步生成，不阻塞首屏。

### 7.3 性能预算

- 规则与意图：P95 80ms；LLM 意图解析不作为唯一入口。
- 查询 Embedding：P95 180ms。
- 本地 BM25 + ANN + 过滤：P95 350ms。
- Top 100 Rerank：P95 450ms。
- 补全与序列化：P95 190ms。
- 首屏总目标：P95 1.5s，P99 2.5s。

压测数据为 12 万候选人、24–48 万检索块、10 并发、5 QPS，并同时运行普通后台任务。标注集不少于 800 条，验收 Recall@300 ≥ 98%、nDCG@10 ≥ 0.85、Top20 覆盖人工 shortlist ≥ 95%。

## 8. 持久化任务系统

任务状态：

```text
PENDING → QUEUED → RUNNING → SUCCESS
                   ├─ RETRY_WAIT → QUEUED
                   ├─ FAILED
                   ├─ CANCELLED
                   └─ DEAD_LETTER
```

任务表是唯一状态来源。Worker 领取任务时使用租约、心跳和幂等键；应用重启后回收超时租约。队列分为交互、普通、批量和导出四类，通过数据库优先级和并发配额调度。用户可查看、暂停、继续、取消和重试。

关闭窗口时默认最小化到托盘；显式退出时停止领取新任务、等待安全点并持久化进度。强制退出后依靠任务租约恢复。

## 9. Provider 适配层

统一接口：

- `LLMProvider.parse_resume()`、`parse_jd()`、`generate_reason()`、`parse_query()`。
- `EmbeddingProvider.embed_documents()`、`embed_query()`。
- `RerankerProvider.rerank()`。
- `OCRProvider.extract()`。
- `WebSearchProvider.search()`。
- `MailProvider.poll()`。

每个 Provider 配置 Base URL、模型、密钥引用、超时、并发、重试和熔断。实现 DeepSeek、OpenAI-compatible、SiliconFlow、Tavily、SerpApi 和标准 IMAP。测试使用确定性的 Fake Provider，不依赖真实网络。

错误统一映射为稳定错误码：配置、鉴权、余额、限流、超时、上游故障、Schema 不符和网络不可达。429 使用带抖动指数退避；鉴权和余额错误不自动重试。

## 10. 备份与恢复

- 自动增量备份：默认每天，保留 7 个每日版本和 4 个每周版本。
- 手动便携备份：生成加密 `.krbackup`，可迁移到另一台受支持电脑。
- 内容：SQLite 一致性快照、LanceDB 数据、原件、词典、非敏感配置、版本清单和加密后的主密钥。
- 恢复：解密到新目录，校验清单、哈希、Schema 与索引版本，健康检查成功后原子切换；不直接覆盖旧目录。
- 未执行的定时备份在下次启动补跑。备份目标位于同一磁盘时给出风险提示。

## 11. 安装、升级与双平台约束

Windows 构建产出签名安装包，macOS 构建产出签名且公证的 DMG。Python Sidecar 必须分别在 Windows x64 与 macOS arm64 构建，不进行跨系统冻结。

CI 至少包含：

- Windows x64：单元、集成、E2E、安装、升级和卸载保留数据测试。
- macOS arm64：单元、集成、E2E、安装、签名、公证前检查和升级测试。
- 路径、大小写、Unicode 文件名、长路径、休眠唤醒、端口占用、杀进程和磁盘满专项测试。

升级前自动创建数据库快照。Alembic 迁移失败时回滚应用版本和数据快照。索引格式变化采用新 generation 构建、校验和原子切换。

## 12. 测试与完成标准

### 12.1 自动化

- Python：pytest 单元测试、Provider 合约测试、SQLite/LanceDB 集成测试。
- TypeScript：组件与状态测试。
- Playwright：上传、解析、检索、JD、匹配、推荐、看板、Mapping、BD、备份恢复等 E2E。
- 故障注入：API 401/402/429/500/503、断网、进程崩溃、索引损坏、数据库锁、磁盘满和端口冲突。

### 12.2 交付完成

只有以下证据全部存在时才能宣称完成：

1. 原设计方案列出的功能验收项均有实现和自动化或 UAT 证据。
2. Windows x64 与 macOS arm64 安装包能在干净机器安装、启动、升级和卸载；卸载默认保留数据。
3. 12 万数据性能测试达到 P95/P99、召回和排序指标。
4. 真实或脱敏样本通过解析、去重、版本、搜索、匹配和导出验收。
5. 备份能恢复到新目录，恢复前后实体数量、Blob 哈希和关键查询结果一致。
6. API Key、候选人敏感字段和原件不出现在日志、诊断包、安装包或版本库中。

## 13. 实施分解

- Phase 0：桌面骨架、事实库、Blob、任务、Provider 基础、简历入库、结构化、LanceDB 和检索性能基线。
- Phase 1：JD、匹配、流程、导出、看板、回收站、撤销、设置和快捷键。
- Phase 2：IMAP、Mapping、提醒、备份恢复、数据迁移、诊断和健康检测。
- Phase 3：BD 搜索、供应商切换、升级、安全加固、安装包和完整验收。

每个阶段必须产生可运行、可测试的软件，不允许用静态界面或占位响应代替业务能力。
