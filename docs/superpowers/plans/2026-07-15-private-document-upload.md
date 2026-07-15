# Private Document Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add QQ private-message document upload using a short-lived, one-time code that binds the upload to a target group.

**Architecture:** `UploadBindingService` owns code issuance, redemption, pending-upload lookup, and consumption. `MemoryStore` persists binding state in SQLite. `bot.py` maps the group upload command and C2C events into this service, while the existing `DocumentService` remains the only document parser and long-term-memory writer.

**Tech Stack:** Python 3.10+, `qq-botpy`, SQLite, `unittest`, existing `DocumentService`.

---

### Task 1: Recognize the group upload command

**Files:**
- Modify: `commands.py`
- Test: `tests/test_commands.py`

- [x] Add a failing test asserting `上传文档`, `文档上传`, and `导入文档` parse as `upload_document`.
- [x] Run the command tests and verify the new test fails because the command is unknown.
- [x] Add the minimal parser branch and list `上传文档` in `HELP_TEXT`.
- [x] Re-run the command tests and verify they pass.

### Task 2: Persist one-time upload bindings

**Files:**
- Modify: `memory_store.py`
- Test: `tests/test_memory_store.py`

- [x] Add failing tests for redeeming a valid code, rejecting an expired code, preventing reuse by another user, and consuming a pending binding.
- [x] Run the memory-store tests and verify failures are caused by missing binding APIs.
- [x] Add the `upload_bindings` table, result dataclass, and transactional create/redeem/get-pending/consume methods.
- [x] Re-run memory-store tests and verify they pass.

### Task 3: Implement the private-upload workflow

**Files:**
- Create: `upload_binding.py`
- Create: `tests/test_upload_binding.py`

- [x] Add failing tests for code issuance, code redemption, upload without binding, successful attachment ingestion, unsupported attachment handling, and one-time consumption.
- [x] Run the workflow tests and verify they fail because `upload_binding` does not exist.
- [x] Implement `UploadBindingService` with a 10-minute TTL, `QG-XXXXXX` codes, SHA-256 hashing, and calls to the existing `DocumentService`.
- [x] Re-run workflow tests and verify they pass.

### Task 4: Connect group and C2C events

**Files:**
- Modify: `bot.py`
- Test: `tests/test_bot.py`

- [x] Add failing async tests using fake QQ messages for issuing a group code, redeeming it in C2C, and ingesting a subsequent private attachment.
- [x] Run the bot tests and verify failures are caused by missing C2C handling.
- [x] Instantiate `UploadBindingService`, handle `upload_document` before travel/LLM routing, and implement `on_c2c_message_create` with private replies.
- [x] Normalize empty message content so standalone file events cannot fail on `.strip()`.
- [x] Re-run bot tests and verify they pass.

### Task 5: Document and verify

**Files:**
- Modify: `README.md`

- [x] Replace the unusable same-message group attachment instructions with the group-code/private-upload flow.
- [x] Document expiry, supported types, one-time behavior, and troubleshooting logs.
- [x] Run `conda run -n agent python -m unittest discover -s tests -v` and verify the complete suite passes.
- [x] Run `conda run -n agent python -m compileall -q .` and verify all Python files compile.
