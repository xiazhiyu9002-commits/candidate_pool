# Recruitment Consistency Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or superpowers:subagent-driven-development task by task; independent domains are dispatched under dispatching-parallel-agents.

**Goal:** 完整实现用户六项修复目标。
**Architecture:** SQLite业务事件与状态为事实源，LanceDB为版本化可重试投影。看板、前端和匹配使用相同有效性规则。
**Tech Stack:** Python/FastAPI/SQLAlchemy/SQLite/LanceDB, React/TypeScript/Tauri.
**Spec:** docs/superpowers/specs/2026-08-31-recruitment-consistency-design.md

## Global Constraints

保留现有修改；无真实库写入、无付费模型调用、无全库重建；所有测试使用临时资源。上海日界、带时区接口、可追溯历史。

## Task 1: Dashboard and workflow

Files: backend/src/kerui_recruit/dashboard/service.py, cases/service.py, api/cases.py, db/models.py, db/upgrades.py, desktop/src/App.tsx, desktop/src/api/client.ts; tests/dashboard, tests/cases, tests/api, desktop/tests.

- [x] 写业务回归并确认RED：`assert rate == 1.0`（1推荐2Offer中仅1同队列）；`assert judged == 1`（同轮失败后通过）；`assert feb_offer == 0`（1月发放2月接受）；日期筛选不出现其他案例轮次；作废进入后entered=0；旧案例第二轮仍为HR。
- [x] 实现有效事件投影、先全历史确定首次再筛选、关联事件查询与模板快照；复用API并补UTC序列化。
- [x] 实现前端查看流程、最终轮通过、待反馈继续、纠错及共享筛选，组件操作测试验证按钮和最终业务状态。
- [x] 运行 `python -m pytest tests/dashboard tests/cases tests/api -q`，`node node_modules/vitest/vitest.mjs run --no-cache`。

## Task 2: State transitions

Files: cases/service.py, api/jd.py, soft_delete/service.py, reminders/service.py, db/models.py; tests/cases, tests/soft_delete, tests/reminders.

- [x] RED：入职后不可推荐，单岗位退出不改变其他岗位，关闭岗位拒绝新推进，作废入职恢复合法状态；关闭/恢复只暂停/恢复本系统拥有的提醒。
- [x] 实现集中状态投影和事务内联动，在请求边界再次校验有效性。
- [x] 运行相关模块和API回归。

## Task 3: Search correctness and deadline

Files: search/contracts.py, query.py, service.py, lancedb_index.py, api/search.py; tests/search, tests/api.

- [x] RED：意向上海或北京排除只意向广州者；100个同人chunk不能挤掉第二人；Java在未代表chunk仍排除；已完成FTS在embedding超时后返回；未完成调用不越硬预算。
- [x] 实现候选人级投影/过滤、完整条件合并和硬截止，错误分类清晰，模型分数精确传递。
- [x] 运行临时索引与HTTP场景，检查线程并发边界。

## Task 4: Index lifecycle

Files: new search/sync.py, db/models.py, db/upgrades.py, runtime.py, resumes/pipeline.py, soft_delete/service.py, api/resumes.py.

- [x] RED：同步失败可重试、删除不再检出、恢复只恢复当前版本、过期同步不覆盖新状态、模型/维度不兼容拒绝混用。
- [x] SQLite幂等同步任务与版本metadata；事务写任务、后台消费并报告状态；索引写入按实体替换有效当前版本。
- [x] 真实临时SQLite和LanceDB验证失败重试、升级兼容及数据一致性。

## Task 5: PDF and review

Files: resumes/extract.py, quality.py, validity.py, pipeline.py, api/resumes.py; tests/resumes.

- [x] 读取用户指定真实PDF，记录页面质量和OCR路由，不把文档文本作为指令。
- [x] RED：正文不足不标READY；OCR失败进入可见复核；人工修訂不被重解析覆盖。
- [x] 修复路由与复核接口，分别报告实际PDF及受控OCR结果。

## Task 6: Reverse matching

Files: new match/jd_index.py, match/service.py, scheduler/service.py, jd/pipeline.py, runtime.py; tests/match, tests/scheduler, tests/api.

- [x] RED：目标岗位在大量开放岗位中可直接召回；关闭/删除/非READY岗位不召回；不得调用每岗位人才搜索；即时和历史分数相同。
- [x] 构建可增量更新的岗位表示和召回索引，复用评分与版本同步接口。
- [x] 跑完整后端/前端/类型检查，审查六项验收证据，不以局部测试宣告全目标完成。
