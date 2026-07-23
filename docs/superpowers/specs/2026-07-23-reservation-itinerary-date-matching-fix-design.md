# Reservation Itinerary Date Matching and Excel Upload Design

Date: 2026-07-23  
Branch: `feature/image-reservation-reminders`

## Background

The reservation draft flow currently retrieves a generic document context for each attraction and asks an LLM to extract only complete calendar dates. This fails for itinerary documents that contain one complete trip range, such as `2026年8月16日—8月22日`, followed by daily headings such as `8月17日` and `8月18日`.

Because the daily headings omit the year, the extractor rejects them but accepts the trip start date. Generic document retrieval also falls back to recent document previews or chunks when an attraction has no match. The combined behavior can assign the trip start date to several unrelated attractions and can produce evidence for attractions that do not appear in the itinerary.

The ordinary document question-answering path can reason over the full itinerary correctly. This change must therefore isolate reservation date resolution from generic document retrieval instead of changing global chunking or question-answer behavior.

The bot must also accept structured Excel itinerary files so that clearer row-based schedules can enter the same group knowledge base and reservation workflow.

## Goals

- Resolve attraction visit dates from actual daily itinerary entries rather than from generic document context.
- Infer the year for partial daily dates only when an explicit trip range makes the year unambiguous.
- Exclude attractions that are explicitly not visited, not entered, cancelled, only passed, viewed from a distance, or seen from outside.
- Never assign a date to an attraction that is absent from the selected itinerary document.
- Use exactly one source document for each reservation draft and avoid mixing dates across itinerary versions.
- Accept `.xlsx` uploads through the existing document upload paths.
- Store Excel contents in the existing group knowledge base so both ordinary questions and reservation resolution can use them.
- Preserve existing document upload, knowledge retrieval, image extraction, confirmation, reminder scheduling, and Outbox behavior.
- Keep existing Word documents usable without re-uploading or migrating database data.

## Non-goals

- Supporting `.xls`, `.xlsm`, `.csv`, or other spreadsheet formats.
- Parsing hidden worksheets, embedded images, charts, comments, colors, or other formatting.
- Recalculating spreadsheet formulas or refreshing external links.
- Reworking global document chunking or generic knowledge retrieval.
- Letting an LLM freely choose visit dates from a whole document chunk.
- Adding a database schema migration.

## Chosen Approach

Add a deterministic, reservation-specific itinerary resolver and a format-specific `.xlsx` extractor.

`DocumentService` remains the only document ingestion boundary. It converts all supported formats into the existing in-memory `full_text`, chunks, and summary representation. SQLite persists the preview, summary, and ordered chunks. The reservation flow reads all stored chunks grouped by document through a narrow store query, selects one itinerary document for the whole draft, parses daily schedule entries, and resolves attraction dates from positive evidence in those entries.

The alternatives were rejected for these reasons:

- Prompt-only changes cannot prevent irrelevant fallback chunks or model variability.
- Global chunking and retrieval changes risk regressions in ordinary document question-answering, which already behaves correctly.
- Sending spreadsheets to a multimodal model is less deterministic, more expensive, and unnecessary for structured cell data.

## Architecture

```text
.docx/.txt/.md/.xlsx attachment
            |
            v
DocumentService format extraction
            |
            v
full_text + chunks + summary
            |
            v
existing group document storage
       /                         \
      v                           v
generic document context     reservation itinerary resolver
                                  |
                                  v
                    one selected itinerary document
                                  |
                                  v
                    daily segments and visit dates
```

The generic document context builder remains unchanged. The reservation service no longer calls it once per attraction to obtain date evidence.

The store exposes a narrowly scoped read operation that returns the group's stored documents with their identifiers, filenames, ordered chunks, and creation order. The resolver scans all chunks in a document and deduplicates any date evidence repeated by chunk overlap. No table or column changes are required.

## Excel Ingestion

### Supported input

- Only filenames ending in `.xlsx` are accepted.
- The existing 5 MB attachment limit and HTTPS download checks remain in force.
- `.xls` receives a specific response asking the user to save it as `.xlsx` and upload again.
- Existing upload instructions and supported-format messages are updated to include `.xlsx`.

### Workbook extraction

Use `openpyxl` in read-only, cached-value mode.

- Read every visible, non-empty worksheet.
- Ignore worksheets whose state is not visible.
- Preserve the worksheet name, header row, and cell order within each row.
- Emit one normalized text row per non-empty worksheet row, separated with ` | ` so existing chunking remains usable.
- Emit a worksheet marker before its rows, for example `[工作表：每日行程]`.
- Normalize date and datetime cell values to ISO-style strings such as `2026-08-17` and `2026-08-17 09:30`.
- Convert text, numbers, and booleans to stable text values.
- Read the saved result of formula cells. Do not include formula expressions and do not recalculate formulas.
- Treat a formula with no saved cached result as empty.
- Rely on the top-left value of merged cells and do not duplicate merged content.
- Omit completely empty rows and trailing empty cells.

Example normalized content:

```text
[工作表：每日行程]
日期 | 行程 | 住宿 | 重点安排
2026-08-17 | 西宁 → 青海湖 → 茶卡盐湖 → 都兰 | 都兰 | 青海湖约1.5小时
2026-08-18 | 都兰 → 察尔汗盐湖 → 大柴旦 | 大柴旦 | 察尔汗优先级最高
```

The normalized text then follows the existing hash deduplication, summary, chunking, and database storage flow.

### Excel failures

- Encrypted, corrupt, or unreadable workbooks fail with a clear upload error.
- A workbook that produces less than the existing minimum useful text fails without storing a partial document.
- A failed extraction does not create a document record.
- Existing private-upload binding and replay semantics remain unchanged.

## Reservation Itinerary Resolution

### Resolution input

The resolver receives:

- the image storage scope;
- the distinct attraction names whose reservation items require a visit date;
- all stored documents for that scope, newest first, with each document's chunks in chunk-index order.

Items that do not require reservations continue to bypass visit-date resolution.

### Daily segment parsing

The resolver identifies daily itinerary segments from lines or normalized spreadsheet rows containing a date expression.

Supported sources include:

- complete dates such as `2026年8月17日`, `2026-08-17`, or equivalent unambiguous forms;
- partial daily headings such as `8月17日`;
- Word table rows flattened with ` | `;
- Excel rows whose date cell has already been normalized.

Partial dates receive a year only when the same document contains an explicit trip range that unambiguously covers the partial date. A general document creation date, upload date, preview date, or unrelated complete date is not a valid year source. If the trip year is absent or conflicting, the partial date remains unresolved.

The trip range is context for year completion only. Its start date is never treated as the visit date of every attraction in the document.

### Positive and negative attraction evidence

An attraction is positively matched only when its name appears in a dated daily segment as an actual route or activity. Substring matches cover normal naming variants, for example:

- `青海湖` matches `青海湖二郎剑景区`;
- `鸣沙山` matches `鸣沙山月牙泉`.

An occurrence is excluded when the local statement describes the attraction with a non-visit meaning, including:

- `不去`;
- `不进入`;
- `取消`;
- `不安排`;
- `仅路过`;
- `外围经过`;
- `远观`.

An attraction mentioned only in a general attraction list, booking policy, document overview, summary, or undated introduction is not considered scheduled.

### Selecting one source document

For each document, count the distinct reservation-required attractions that have at least one positive dated match. Negative and undated mentions do not count.

- Select the document with the highest distinct-match count.
- Break a tie by choosing the most recently stored document.
- If every document scores zero, select no document and leave all visit dates unresolved.
- Once selected, resolve every item only from that document.
- Do not fill a missing attraction from an older or lower-scoring document.

This makes a newer itinerary version authoritative only when its positive coverage is at least as strong as another version, while preventing cross-version date mixtures.

### Resolution outcomes

For each attraction, the resolver returns the date candidates and a reason category:

- exactly one positive date: use it as the visit date;
- more than one different positive date: keep all candidates and require manual confirmation;
- only negative/non-visit occurrences: mark the item as not scheduled and show `行程未安排该景点，需要手动决定`;
- no occurrence in the selected document: leave the date undetermined;
- no source document: leave the date undetermined.

The existing reservation item status field can distinguish unresolved and not-scheduled outcomes; no schema change is required. Booking dates and reminder occurrences are calculated only after exactly one visit date is resolved.

The first version uses deterministic Python parsing for these outcomes. It does not use the existing LLM date extractor as a fallback, because a fallback could reintroduce unsupported inference.

## Expected Qinghai-Gansu Result

For `青甘七日自驾行程更新版V3.docx`, the reservation resolver must produce:

| Attraction | Result |
| --- | --- |
| 青海湖 | `2026-08-17` |
| 茶卡盐湖 | `2026-08-17` |
| 察尔汗盐湖 | `2026-08-18` |
| 翡翠湖 | `2026-08-19` |
| 莫高窟 | `2026-08-20` |
| 鸣沙山 | `2026-08-20` |
| 嘉峪关 | Not scheduled; manual decision required |
| 水上雅丹 | Date undetermined |

None of these attractions may inherit the trip start date `2026-08-16` unless a dated daily segment explicitly schedules it on that day.

## Error and Compatibility Behavior

- Existing `.docx`, `.txt`, and `.md` ingestion behavior remains unchanged.
- Existing stored documents work from their current ordered chunks; re-upload is not required.
- Generic `build_document_context` behavior remains unchanged for ordinary questions.
- Failure to resolve an itinerary date is not a workflow error. The draft is created with an explicit manual-input state.
- Multiple date candidates are retained for review instead of choosing the earliest or latest candidate.
- The change does not alter reservation policy extraction from images, booking-date calculations, reminder defaults, confirmation commands, or scheduled delivery.

## Dependencies

Add `openpyxl` to `requirements.txt`. `requirements-onebot.txt` already includes `requirements.txt`, so GitHub Actions and OneBot installations receive the dependency without workflow-specific installation steps.

## Test Strategy

Implementation follows strict test-driven development: add failing tests, confirm the expected failure, make the smallest production change, and rerun focused tests before the full suite.

### Itinerary parser tests

- Reproduce the Qinghai-Gansu itinerary and assert every expected date.
- Assert that `2026-08-16` is not assigned merely because it appears in the trip range.
- Cover complete dates, partial dates with an explicit trip range, missing years, conflicting years, and multiple candidate dates.
- Cover all defined non-visit phrases.
- Assert that undated lists and overviews do not schedule attractions.
- Assert that an absent attraction produces no date evidence.

### Document selection tests

- Prefer the document with the highest number of distinct positive matches.
- Prefer the newest document when scores tie.
- Ignore negative matches when scoring.
- Never combine dates from two documents.
- Select no source when all scores are zero.

### Excel extraction tests

Generate small workbooks in tests and verify:

- all visible, non-empty worksheets are extracted;
- hidden and empty worksheets are ignored;
- worksheet names, headers, and row relationships are preserved;
- Excel date and datetime values are normalized;
- text, numeric, boolean, and cached formula results are handled as specified;
- merged content is not duplicated;
- corrupt or insufficient workbooks do not create stored documents;
- `.xls` receives the conversion guidance.

### Integration and regression tests

- Equivalent Word-style and Excel itineraries resolve to the same attraction dates.
- Excel documents are searchable through the ordinary group knowledge context.
- Existing direct and private upload workflows accept `.xlsx`.
- Existing image recognition, reservation editing, confirmation, reminder scheduling, and Outbox tests remain green.
- Run `python -m unittest discover -s tests -v`; all current 196 tests and all new tests must pass.

## Expected Change Scope

Production changes are limited to:

- `document_service.py` for `.xlsx` recognition and extraction;
- `memory_store.py` for the narrow stored-document read operation;
- `reservation_service.py` for deterministic itinerary resolution and draft formatting;
- `requirements.txt` for `openpyxl`;
- existing upload guidance strings that enumerate supported formats;
- focused and integration test files.

No database migration, global retrieval rewrite, or unrelated refactor is included.

## Acceptance Criteria

- The Qinghai-Gansu document produces the exact expected mapping above.
- 嘉峪关 is not assigned `2026-08-21` because the itinerary explicitly does not enter it.
- 水上雅丹 receives no fallback date because it is absent.
- A structured `.xlsx` itinerary can be uploaded, saved, queried, and used for reservation dates.
- Multiple itinerary versions never contribute dates to the same draft.
- Existing Word documents do not need re-uploading.
- The complete automated test suite passes.
