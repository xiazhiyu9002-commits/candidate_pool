# Real Data and Release Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current Windows candidate build into a user-facing release candidate validated against the supplied 2,068-file corpus and real model/search providers, while preparing an equivalent macOS arm64 build path.

**Architecture:** Keep SQLite as the business source, LanceDB as a rebuildable projection, and Tauri as the only user entry point. Secrets remain encrypted in the local data directory and are injected into acceptance runs only through process environment variables; validation reports contain counts and error codes but no resume plaintext, paths, or credentials.

**Tech Stack:** Python 3.12, FastAPI, SQLite, LanceDB, React 19, TypeScript, Tauri 2, Vitest, Playwright, pytest, PyInstaller, NSIS.

**Spec:** `docs/superpowers/specs/2026-08-29-embedded-recruitment-desktop-design.md`

## Global Constraints

- Production targets remain Windows 10/11 x64 and macOS Apple Silicon with 16 GB RAM and SSD.
- Never commit, print, export, or include API keys, candidate plaintext, contact data, or original resumes in reports.
- Real-provider tests use a separate disposable data root and preserve the supplied corpus unchanged.
- Every behavior change follows red-green-refactor and ends with a focused commit.
- A release is incomplete until clean-machine installation, upgrade, uninstall-data-retention, backup restore, and real-corpus evidence exist.

---

### Task 1: Safe settings round-trip and provider activation UX

**Files:**
- Modify: `backend/src/kerui_recruit/core/settings_service.py`
- Modify: `backend/src/kerui_recruit/api/settings.py`
- Modify: `desktop/src/App.tsx`
- Modify: `desktop/src/api/client.ts`
- Test: `backend/tests/core/test_settings_service.py`
- Test: `desktop/tests/App.test.tsx`
- Test: `desktop/tests/api-client.test.ts`

**Interfaces:**
- Produces: masked values are a no-op when submitted unchanged.
- Produces: `POST /api/onboarding/test-providers` is exposed in Settings.
- Produces: the UI explicitly reports that provider changes apply after restart.

- [ ] Write a failing backend test proving a masked value cannot replace the stored ciphertext.
- [ ] Run the focused test and verify the decrypted stored key changes incorrectly.
- [ ] Make masked sensitive fields no-op values and return an `application_restart_required` response flag.
- [ ] Run backend settings tests until green.
- [ ] Write failing React/API client tests for provider connectivity and restart-required feedback.
- [ ] Implement the smallest UI/client changes and rerun focused desktop tests.
- [ ] Commit with `fix: preserve configured provider secrets`.

### Task 2: Complete desktop access to existing business APIs

**Files:**
- Modify: `desktop/src/App.tsx`
- Modify: `desktop/src/api/client.ts`
- Modify: `desktop/src/styles.css`
- Test: `desktop/tests/App.test.tsx`
- Test: `desktop/tests/api-client.test.ts`
- Test: `desktop/tests/workflow.spec.ts`

**Interfaces:**
- Consumes: task list/cancel/retry/pause/resume, JD file import, batch match, match marking, resume revisions/switch, diagnostics export, portable backup/restore and soft-delete APIs.
- Produces: each operation is reachable without copying database identifiers manually.

- [ ] Add one failing component/client test per user-visible workflow.
- [ ] Verify every test fails because the corresponding control or method is absent.
- [ ] Add typed client methods and minimal controls for task operations and resume/JD versions.
- [ ] Add batch matching, result marking, JD Word/Excel import and diagnostics download.
- [ ] Add portable encrypted backup/restore and delete/recycle-bin actions.
- [ ] Extend Playwright to exercise the real sidecar for these workflows.
- [ ] Run `npm test`, `npm run build`, and `npm run test:e2e`.
- [ ] Commit in independently reviewable workflow groups.

### Task 3: Corpus preflight and privacy-safe acceptance harness

**Files:**
- Create: `backend/src/kerui_recruit/bench/corpus_acceptance.py`
- Create: `backend/tests/acceptance/test_corpus_acceptance.py`
- Create: `docs/verification/corpus-acceptance.schema.json`

**Interfaces:**
- Produces: `scan_corpus(root: Path) -> CorpusInventory`.
- Produces: a JSON report containing only aggregate format, extraction, OCR, duplicate, parse, index, task and latency metrics.
- Consumes: a corpus path and an isolated data root; never mutates source files.

- [ ] Write failing tests using generated PDF, DOCX, duplicate, scan and unsupported fixtures.
- [ ] Implement deterministic inventory and source hashing without emitting filenames or text.
- [ ] Add extraction/OCR classification and resumable import checkpoints.
- [ ] Add report secret/plaintext redaction assertions.
- [ ] Run preflight against `E:/traeWork/KeRui/测试简历` and record aggregate evidence.
- [ ] Commit with `test: add privacy-safe corpus acceptance`.

### Task 4: Real-provider staged acceptance

**Files:**
- Modify: `backend/src/kerui_recruit/bench/corpus_acceptance.py`
- Test: `backend/tests/providers/test_connectivity.py`
- Create: `docs/verification/real-provider-acceptance.json`

**Interfaces:**
- Consumes: DeepSeek `deepseek-v4-flash`, SiliconFlow `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`, and Tavily via environment-only secrets.
- Produces: connectivity, structured parsing, embedding dimension, reranking and web-search evidence without response plaintext.

- [ ] Run connectivity probes and stop on authentication, balance or model errors.
- [ ] Process a deterministic representative sample across PDF, DOCX and OCR-required groups.
- [ ] Verify facts, index readiness, query retrieval and task terminal states.
- [ ] Resume through the full 2,056 supported-file corpus with bounded concurrency and durable checkpoints.
- [ ] Record aggregate success/failure and error-code distribution only.
- [ ] Commit the redacted report, never credentials or source material.

### Task 5: Upgrade, backup and Windows clean-install gate

**Files:**
- Modify: `backend/src/kerui_recruit/db/migrate.py`
- Create: `backend/src/kerui_recruit/db/upgrades.py`
- Test: `backend/tests/db/test_upgrade.py`
- Create: `desktop/scripts/verify-windows-release.ps1`
- Create: `docs/verification/windows-release.md`

**Interfaces:**
- Produces: versioned, transactional schema upgrades with pre-upgrade snapshot and rollback evidence.
- Produces: installer smoke checks for install, launch, upgrade, uninstall and retained data.

- [ ] Write failing schema upgrade/rollback tests from version 1 to version 2.
- [ ] Implement a migration registry and snapshot-before-upgrade behavior.
- [ ] Verify old/new app compatibility and search projection rebuild behavior.
- [ ] Build a fresh NSIS installer and run clean Windows installation gates.
- [ ] Verify uninstall preserves the user data directory by default.
- [ ] Record signature status separately from functional acceptance.
- [ ] Commit with `build: add windows release acceptance gate`.

### Task 6: macOS arm64 packaging and CI path

**Files:**
- Modify: `desktop/src-tauri/tauri.conf.json`
- Create: `desktop/src-tauri/tauri.macos.conf.json`
- Create: `backend/packaging/build_sidecar_macos.sh`
- Modify: `.github/workflows/ci.yml`
- Create: `docs/verification/macos-release.md`

**Interfaces:**
- Produces: platform-specific sidecar resources and an Apple Silicon build job.
- Consumes later: Developer ID signing identity and notarization credentials.

- [ ] Add config validation that fails when a platform references another platform's executable suffix.
- [ ] Split Windows and macOS resource configuration.
- [ ] Add arm64 PyInstaller sidecar build and Tauri DMG commands.
- [ ] Add macOS unit, E2E, packaging and unsigned-artifact inspection CI.
- [ ] On an Apple Silicon builder, verify install, launch, upgrade and uninstall-data retention.
- [ ] Sign and notarize when credentials are supplied.
- [ ] Commit with `build: add macos arm64 release path`.

### Task 7: Final verification and branch readiness

**Files:**
- Modify: `docs/verification/phase1-3.md`
- Create: `docs/verification/release-readiness.md`

**Interfaces:**
- Produces: one evidence index mapping every completion requirement to a command, artifact and result.

- [ ] Run all Python tests including performance.
- [ ] Run React, TypeScript, Rust and Playwright suites.
- [ ] Run secret scanning against Git history, reports and installer contents.
- [ ] Verify encrypted backup restore preserves counts, Blob hashes and key queries.
- [ ] Link Windows and macOS installation artifacts and signing evidence.
- [ ] Record only genuine external blockers; do not mark the release complete while any required gate is missing.
