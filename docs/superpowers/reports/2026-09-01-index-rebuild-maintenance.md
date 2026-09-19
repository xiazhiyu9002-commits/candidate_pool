# 索引诊断与隔离重建维护入口

此入口默认只读诊断，不会重建、清空、切换正在使用的索引。实现位于 `backend/src/kerui_recruit/search/rebuild_maintenance.py`。本轮仅在临时 SQLite/LanceDB 上验收，没有操作正式库或正式索引，没有调用远程服务。

## 支持范围

- 诊断任意指定 embedding 模型与维度的元数据兼容性，显示 SQLite 版本、实体总数、候选人与岗位索引元数据。
- 可执行构建支持本机 `local-hash-v1`（64 维）与经 SiliconFlow 提供的远程 embedding（如 `BAAI/bge-m3`，1024 维），索引 schema `2`、chunk version `2`。
- 远程执行时从环境变量 `SILICONFLOW_API_KEY` 或 `<data-root>/config/settings.json`（AES 解密）读取密钥；缺少密钥则失败，不读取 DeepSeek/OCR 凭据、不调用解析或结构化模型。
- 不重新解析简历/JD，不调用 OCR 或结构化模型；只从 SQLite 已有结构化数据生成 chunk 并重新生成向量。
- 来源 SQLite 必须已通过应用的独立升级流程达到当前 schema；工具绝不迁移源数据库。

## 只读诊断

在 backend 目录执行。下面路径是占位示例，需替换为明确要检查的数据位置。

```powershell
$env:PYTHONPATH = 'src'
& '../.venv/Scripts/python.exe' -m kerui_recruit.search.rebuild_maintenance --database 'D:/ApprovedData/db/recruit.sqlite3' --index-root 'D:/ApprovedData/search' --model 'BAAI/bge-m3' --dimension 1024
```

不传 `--execute` 时不会实例化索引或 provider，也不会创建索引目录。`missing-metadata-or-empty` 表示元数据缺失或空索引，`rebuild-required` 表示元数据不匹配。即使元数据吻合，诊断也只报告 `metadata-matches-physical-schema-not-checked`，不会冒充已验证物理 schema 或检索质量。

## 隔离离线构建

先完整退出应用及后台进程，并保持关闭直到完成切换。`--app-stopped` 是操作者对这一前提的明确确认，工具不会杀进程。构建还会检测源 SQLite 在运行期间是否被其他连接写入；发生写入即标记失败，不能切换。

```powershell
$env:PYTHONPATH = 'src'
& '../.venv/Scripts/python.exe' -m kerui_recruit.search.rebuild_maintenance --database 'D:/ApprovedData/db/recruit.sqlite3' --index-root 'D:/ApprovedData/search' --model 'local-hash-v1' --dimension 64 --execute --app-stopped --output 'D:/ApprovedData-rebuild-unique'
```

远程 embedding（BAAI/bge-m3，1024 维，密钥取自环境变量或 `<data-root>/config/settings.json`）：

```powershell
$env:PYTHONPATH = 'src'
& '../.venv/Scripts/python.exe' -m kerui_recruit.search.rebuild_maintenance --database 'D:/ApprovedData/db/recruit.sqlite3' --index-root 'D:/ApprovedData/search' --model 'BAAI/bge-m3' --dimension 1024 --execute --app-stopped --output 'D:/ApprovedData-rebuild-bge-unique'
```

输出目录必须尚不存在，且不能与原索引或源数据库重叠。工具使用 SQLite backup API 建立一致快照，不直接复制活动 SQLite 文件。只在快照的 outbox 中安排工作，复用 `IndexSyncService` 的当前版本、候选人状态、岗位开放状态和删除过滤规则，保留多份当前简历证据及意向城市。原库的任务、同步状态与数据保持不变。

输出内容：

- `source-snapshot.sqlite3`：构建时业务快照，包含敏感个人信息，仅限本机受控存放。绝不能覆盖正式数据库。
- `search/`：候选人新索引，`search/jobs/` 为岗位新索引，必须作为整体切换。
- `manifest.json`：状态、模型/维度/schema/chunk 版本、验证后的实体数和 chunk 数；不包含简历正文或 API 密钥。

完成实体/chunk 数校验、元数据兼容检查和 FTS 优化，且源库未变化后，才写入 `READY_FOR_OFFLINE_SWITCH`。任一 provider、写入、校验或源库变化问题都会标记 `FAILED`，退出非零；旧索引原样保留。失败目录保留以便检查，不会自动删除或再次覆盖。

## 人工切换与恢复

1. 确认应用一直处于关闭状态，manifest 为 `READY_FOR_OFFLINE_SWITCH`，且应用将使用与构建一致的 embedding 模型/维度（本地 `local-hash-v1/64` 或远程 `BAAI/bge-m3/1024`）。如果构建后又启动了应用或修改了业务数据，丢弃此次切换计划，保持原索引并重新构建。
2. 核实三个绝对路径：原 `search` 目录、唯一的旧索引备份位置、输出中的 `search` 目录。确保备份目标不存在、没有路径重叠，且同一文件系统上有足够空间。
3. 将原 `search` 整体重命名为旧索引备份；将新 `search` 整体移动到原位置。不要只移动候选人 Lance 表，也不要混合新旧模型的数据文件。若第二步移动失败，立即把旧备份恢复为原路径。
4. 保留旧索引备份、manifest 和快照，启动应用后核对索引状态、候选人过滤/搜索和开放岗位匹配。全部验收后再按本机数据保留策略处理快照和旧备份。
5. 如需恢复，先退出应用，将新索引移到另一个保留目录，把旧备份恢复为原 `search`，然后恢复旧索引对应的模型设置并重启。旧索引若本来不兼容，恢复只回到原状态，不会自动修复兼容性。

工具不会自动执行上述移动，也没有在本轮对正式目录执行这些步骤。

## 验证

`backend/tests/search/test_rebuild_maintenance.py` 使用临时 SQLite/LanceDB，覆盖只读诊断、本地与远程（注入 FakeEmbeddingProvider）离线构建、远程元数据/维度写入、远程执行缺少密钥拒绝、元数据和 FTS/意向城市检索、原库/outbox/旧索引不变、失败不晋升、构建期间源写入拒绝、路径重叠/未停机拒绝与源 schema 不匹配拒绝。初始缺失模块的测试已记录 RED，再实现并运行验收。
