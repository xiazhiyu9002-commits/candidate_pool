# 发布就绪清单

**日期：** 2026-08-30

## 已闭环

| 范围 | 证据 | 结果 |
| --- | --- | --- |
| 后端单元/集成 | `pytest -q` | 194 passed，1 个 performance 用例按标记排除 |
| 12,000 候选性能门槛 | `pytest tests/performance -m performance -q` | 1 passed |
| React 单元 | `npm test` | 30 passed |
| TypeScript / Vite | `npm run build` | 通过 |
| Rust 桌面壳 | `cargo test` | 6 passed |
| 浏览器主流程 | `npm run test:e2e` | 7 passed |
| 真实语料预检 | `corpus-preflight-2026-08-30.json` | 2,068 文件；2,052 可直接提取；4 需 OCR；0 提取失败 |
| 真实语料本地全量处理 | 独立数据目录任务聚合 | 2,052 SUCCESS；4 E_OCR_REQUIRED；0 卡死任务 |
| 旧版 DOC | 源文件哈希不变；开发环境与冻结 sidecar 冒烟 | 5/5 成功 |
| Windows NSIS | `verify-windows-release.ps1 -RunInstallCycle` | 安装、启动建库、卸载、保留数据全部通过 |
| 密钥扫描 | 工作树与 Git 历史模式扫描 | 0 个非测试密钥命中 |

Windows 最新功能验收产物为 `kerui-recruit-desktop_0.1.0_x64-setup.exe`，大小 161,854,067 字节，SHA-256 为 `e0d93aec818083721340da302530a143750591ca3d95587b4e47321d88b1e0c2`。该哈希只对应本次未签名内部验收包。

## 外部阻塞

| 阻塞 | 当前状态 | 解除条件 |
| --- | --- | --- |
| SiliconFlow Embedding / Reranker | 密钥认证成功，但推理返回 HTTP 402 余额不足 | 充值当前账号或提供有余额的新 Key |
| 4 份扫描 PDF OCR | 本地流程正确进入 `E_OCR_REQUIRED`；真实视觉 OCR 未跑 | 提供与图片输入兼容且可用的视觉模型 API，并完成 4 份重试 |
| Windows 生产签名 | 当前 `NotSigned` | 提供 Windows 代码签名证书/签名服务 |
| macOS arm64 实包与实机 | 配置、构建脚本和 CI job 已提交，本机无法原生生成或安装 DMG | 在 Apple Silicon runner/实机运行，之后提供 Developer ID 与公证凭据 |
| 升级安装 | 新安装与卸载保留数据已通过，但没有上一正式版本安装包 | 提供上一版本安装包后执行真实升级门槛 |

## 安全发布前必须处理

- 用户在对话中提供过 API Key。虽然仓库和 Git 历史扫描无真实密钥命中，仍建议在正式交付前轮换这些 Key。
- 当前敏感字段和设置使用 AES-256-GCM 加密，但主密钥仍存放在本机数据目录；Windows Credential Manager / macOS Keychain 的主密钥托管尚未实现，不能将当前状态表述为系统凭据库保护。
- Windows 和 macOS 安装包在签名完成前仅作为内部验收产物，不作为公开分发版本。
