# Desktop reliability implementation plan

> Execute with superpowers:subagent-driven-development; verify each boundary with regression tests.

**Goal:** Implement the user's approved review fixes, with Word opened by the local default application, evening and next-morning reports, and no SerpApi UI.

**Architecture:** Retain React/Tauri/Python. Resolve original document metadata on the backend, open only managed Word copies through the native shell. Keep report scheduling local. Make backup and migration operate on consistent snapshots rather than copying live database/index handles.

**Spec:** User approval in this task dated 2026-09-05 and the preceding read-only findings.

**Constraints:** Preserve existing business data. No real email, external model calls or production restore during testing. Keep legacy backups readable. Support Windows and macOS in shared source. Existing worktree at commit 180a95a; no new checkout needed.

## Tasks / acceptance ledger

- [ ] Native document viewing: every details entry consults original metadata; DOC/DOCX opens with OS default application without save dialog or Word COM conversion; PDF retains inline preview. Reject unrelated native paths. Show backend errors and release preview URLs. Terminate backend after failed readiness and on exit; test lifecycle cleanup.
- [ ] Followup reporting: latest interview round and latest result determine pending state; future-today interviews are not overdue; explicit pending result is pending. One report per evening slot and next morning slot; no morning catch-up after evening window opens. Test with fake sender/clock. Mail test reads latest saved settings.
- [ ] Data protection: reject portable restore to current root, ancestors, descendants and unsafe roots before mutation. Preserve unrelated target data through safe backup/validation. Snapshot database consistently; rebuild search indexes when restoring/migrating rather than trusting copied live projections. Stream archive/encryption work with bounded memory and retain legacy compatibility. Test corrupt archives, rollback and concurrent writes.
- [ ] UI fixes: load settings automatically; remove SerpApi controls and update payload. Allow filters without text (frontend and API), clearly distinguish no matches. Keep task polling beyond 30 seconds with visible status on errors; deduplicate/clean up polling. Await OCR and refresh review details; protect unsaved review edits.
- [ ] Verification: focused red/green regression cycles, backend suite, frontend suite/build, Rust tests/platform validation; inspect final diff and independent review. Record limitations instead of claiming unperformed Mac hardware validation.

## Ownership

Root: native document opening, React/client and search API; final integration.
Followup worker: daily_followup, api/settings mail tests and associated backend tests.
Data worker: backup, migration, snapshot helper, startup reindex integration and associated backend tests (coordinate runtime.py before edits).

## Test scenarios

Report tests seed a two-round case and assert second-round inclusion, explicit pending inclusion, future-today exclusion, and exactly one evening plus one next-morning send.
Restore tests use temporary directories with sentinels and assert unsafe targets rejected without changing sentinel bytes. New encrypted format tests round-trip, corruption rejection and legacy decrypt compatibility. Migration tests keep a live SQLite writer and validate target SQLite and counts.
UI tests use fake APIs for Word metadata/default open, settings loading, filter-only requests, deferred OCR refresh and discard confirmation. Native tests use temporary managed paths and child processes, never real documents.

## Rulings

Reports are recomputed at each slot; morning shows today's upcoming interviews as well as tomorrow's, so a next-morning report does not omit the interviews identified the evening before. The evening window starts at 21:30 and morning at 09:00, Asia/Shanghai. Missed morning slots are not sent at night.
