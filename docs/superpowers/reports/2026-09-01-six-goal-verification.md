# 六项目标最终验收

验收日期：2026-09-01。所有数据库、索引、浏览器端到端数据均位于测试临时目录；没有写入正式业务库或正式索引，没有调用付费或远程模型，也没有执行正式全库向量重建。

## 逐项目结论

| 目标 | 已验证行为 | 主要证据 |
| --- | --- | --- |
| 看板与可变面试流程 | 周/月/季度及公司、岗位、日期使用同一筛选范围；按案例快照解释不同轮次；作废事件、首次进入、Offer 发放与接受口径分离；前端可完成最终轮、补充轮、待反馈与纠错 | `tests/dashboard`、`tests/cases`、`desktop/tests/App.test.tsx` |
| 状态联动 | 入职事实阻止重复推荐；岗位关闭、案例、候选人、提醒、删除/恢复在事务边界同步；启动时修复旧入职事实的候选人投影 | `tests/cases`、`tests/reminders`、`tests/test_runtime.py` |
| 检索正确性与时限 | 条件按候选人聚合后过滤，避免多 chunk 挤占；前端传递年限、学历、城市、意向地、学校、QS 与排除技能；分页候选人列表有总数与上/下一页；外部依赖受硬截止约束 | `tests/search`、`tests/api/test_candidate_paging.py`、`desktop/tests/api-client.test.ts` |
| 索引生命周期 | SQLite outbox 带请求/应用版本；失败可重试；删除、恢复、当前版本和过期任务一致；候选人与岗位索引检查模型、维度、schema 与 chunk 版本 | `tests/search/test_sync.py`、`tests/search/test_consistency.py`、`tests/match/test_reverse_index.py` |
| PDF 与人工复核 | 指定两页图片型 PDF 会识别重复水印并逐页走 OCR；正文或结构化结果不足不会发布 READY；机器草稿、原文、诊断可见；人工资料不会被重解析覆盖 | `tests/api/test_real_pdf_review.py`、`tests/resumes/test_pipeline_ocr.py`、`2026-08-31-pdf-review-verification.md` |
| 反向匹配 | 候选人直接从增量岗位索引召回开放、未删除、READY 岗位；即时与历史复用相同评分；不逐岗位调用人才搜索 | `tests/match/test_reverse_index.py`、`tests/match/test_match.py` |

## 最终复审修复

- 备份恢复先暂存到下次启动；启动前检查 SQLite 完整性、必要表和最高 schema 版本。未来版本或损坏的恢复意图会改名隔离，不能替换当前数据库或让应用反复启动失败。恢复后清空旧搜索投影并逐实体重新入队。
- 工作任务有心跳与取消围栏。取消简历解析时会补偿已经提交的 `PROCESSING` 状态，使首次解析进入可复核的 `FAILED`，重新解析则保留既有 `READY` 资料。
- 调度器把邮件、提醒、备份和清理等阻塞调用移到线程，避免卡住事件循环。
- 单份简历解析结束和 JD 导入后，前端自动刷新对应列表。Playwright 脚本改用隔离端口与隔离数据目录，不会误连本机正在运行的正式 sidecar。

## 验证结果

- 后端全量（排除性能标记）：`449 passed, 1 deselected`，包含用户指定真实 PDF 与本机离线 OCR 结果。
- 前端组件/API/平台测试：`46 passed`；TypeScript 与 Vite 生产构建通过。
- 隔离浏览器端到端：`7 passed`，覆盖简历导入与搜索、人工复核、JD 导入、看板、组织 Mapping、BD 空结果、健康与索引状态。
- 十万候选人性能门禁：`1 passed in 28.77s`。独立受控报告中 100,000 候选人、300,000 chunks 的并发 P95 为 830.58ms、最大 975.65ms，低于十秒目标。
- Windows/macOS Tauri 配置校验通过；目标文件 `git diff --check` 无空白错误。

十万数据报告使用 64 维本地哈希向量、ASGI 请求和合成短文本，用来验证算法规模与截止机制，不代表真实长简历、远程 embedding/reranker 网络时延或语义准确率。详见 `docs/verification/2026-09-01-search-100k.md`。

## 六项目标之外仍建议处理

这些问题不属于本次六项修复，当前代码中仍可确认存在：

1. 数据目录迁移没有拒绝“目标等于源目录”或源目录内部路径；现有删除目标逻辑可能误删源数据，需先做路径边界和活动进程保护。
2. 组织 Mapping 在创建/更新部门和人员时没有完整校验父节点、汇报人同公司及无环；错误数据可能让树递归失败。
3. BD SSE 的 agent 任务异常时不会向队列写终止事件，生成器会永久等待；客户端断开后的 `finally` 还可能继续等待任务。
4. 邮箱同步在附件真正入库前就标记邮件已读并推进 UID；附件导入失败后无法自动重试，应把确认游标放到成功入库之后或增加逐邮件状态表。
