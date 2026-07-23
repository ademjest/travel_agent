# Reservation Itinerary Date Matching and XLSX Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace generic LLM-based reservation date guessing with deterministic single-itinerary matching, and add `.xlsx` itinerary ingestion to the existing group document knowledge base.

**Architecture:** `MemoryStore` exposes ordered document chunks without changing the schema. `ReservationItineraryResolver` reconstructs each stored document, parses dated daily segments, excludes non-visit statements, selects one best-matching document for the whole draft, and returns deterministic resolution reasons. `DocumentService` uses `openpyxl` to normalize visible worksheet rows into the same text/chunk storage pipeline used by Word and text files.

**Tech Stack:** Python 3.11, SQLite, `unittest`, `python-docx`, `openpyxl`, existing QQ official/OneBot adapters.

---

## Execution Context

- Worktree: `E:\Agent\.worktrees\travel_agent\image-reservation-reminders`
- Branch: `feature/image-reservation-reminders`
- Design: `docs/superpowers/specs/2026-07-23-reservation-itinerary-date-matching-fix-design.md`
- Baseline verification: `python -m unittest discover -s tests -v`
- Editing rule: use `apply_patch`; do not modify `main` or the primary checkout.

## File Responsibility Map

- `memory_store.py`: return documents with ordered persisted chunks; no schema migration.
- `reservation_service.py`: deterministic itinerary parsing, document selection, draft integration, and user-visible unresolved reasons.
- `document_service.py`: recognize and normalize `.xlsx`; retain existing hash, summary, chunking, and persistence behavior.
- `bot.py`: stop wiring the removed LLM visit-date extractor.
- `onebot_app.py`: stop wiring the removed LLM visit-date extractor.
- `upload_binding.py`: advertise `.xlsx` and explain `.xls` conversion in private upload replies.
- `requirements.txt`: install `openpyxl` for every runtime, including `requirements-onebot.txt` consumers.
- `README.md`: document `.xlsx` support and the deterministic one-document reservation rule.
- `tests/test_memory_store.py`: ordered document-content query coverage.
- `tests/test_reservation_service.py`: deterministic parser, negative evidence, ambiguity, document selection, and draft formatting.
- `tests/test_document_service.py`: workbook extraction, visibility, dates, cached-value mode, corruption, ingestion, and `.xls` guidance.
- `tests/test_upload_binding.py`: private-upload format messaging.
- `tests/test_reservation_acceptance.py`: combined-document reservation regression and XLSX end-to-end acceptance.

### Task 1: Read Stored Documents With Ordered Chunks

**Files:**
- Modify: `memory_store.py`
- Test: `tests/test_memory_store.py`

- [ ] **Step 1: Write the failing group-scoped ordering test**

Add this test to `MemoryStoreTests` in `tests/test_memory_store.py`:

```python
def test_lists_document_contents_newest_first_with_ordered_chunks(self):
    self.store.add_document(
        "group-a",
        "member",
        "older.md",
        "older-hash",
        "older first\nolder second",
        ["older first", "older second"],
    )
    self.store.add_document(
        "group-a",
        "member",
        "newer.md",
        "newer-hash",
        "newer only",
        ["newer only"],
    )
    self.store.add_document(
        "group-b",
        "member",
        "secret.md",
        "secret-hash",
        "other group",
        ["other group"],
    )

    documents = self.store.list_document_contents("group-a")

    self.assertEqual(
        tuple(document.filename for document in documents),
        ("newer.md", "older.md"),
    )
    self.assertEqual(documents[0].chunks, ("newer only",))
    self.assertEqual(
        documents[1].chunks,
        ("older first", "older second"),
    )
```

- [ ] **Step 2: Run the new test and confirm RED**

Run:

```powershell
python -m unittest tests.test_memory_store.MemoryStoreTests.test_lists_document_contents_newest_first_with_ordered_chunks -v
```

Expected: `ERROR` with `AttributeError: 'MemoryStore' object has no attribute 'list_document_contents'`.

- [ ] **Step 3: Add the immutable document-content record**

Immediately after `StoredDocument` in `memory_store.py`, add:

```python
@dataclass(frozen=True)
class StoredDocumentContent:
    document_id: int
    filename: str
    chunks: tuple[str, ...]
```

- [ ] **Step 4: Implement the narrow read operation**

Add this method immediately before `build_document_context` in `memory_store.py`:

```python
def list_document_contents(
        self,
        group_openid: str) -> tuple[StoredDocumentContent, ...]:
    with self._connect() as connection:
        rows = connection.execute(
            """
            SELECT
                d.id AS document_id,
                d.filename,
                c.chunk_index,
                c.content
            FROM documents d
            LEFT JOIN document_chunks c ON c.document_id = d.id
            WHERE d.group_openid = ?
            ORDER BY d.id DESC, c.chunk_index ASC
            """,
            (group_openid,),
        ).fetchall()

    grouped: list[dict[str, object]] = []
    for row in rows:
        document_id = int(row["document_id"])
        if not grouped or grouped[-1]["document_id"] != document_id:
            grouped.append({
                "document_id": document_id,
                "filename": str(row["filename"]),
                "chunks": [],
            })
        if row["content"] is not None:
            grouped[-1]["chunks"].append(str(row["content"]))

    return tuple(
        StoredDocumentContent(
            document_id=int(item["document_id"]),
            filename=str(item["filename"]),
            chunks=tuple(item["chunks"]),
        )
        for item in grouped
    )
```

- [ ] **Step 5: Run focused storage tests and confirm GREEN**

Run:

```powershell
python -m unittest tests.test_memory_store.MemoryStoreTests.test_lists_document_contents_newest_first_with_ordered_chunks tests.test_memory_store.MemoryStoreTests.test_document_context_retrieves_relevant_chunk -v
```

Expected: both tests pass; the existing generic context behavior remains unchanged.

- [ ] **Step 6: Commit the storage API**

```powershell
git add memory_store.py tests/test_memory_store.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "feat: expose stored itinerary document chunks"
```

### Task 2: Parse One Itinerary Deterministically

**Files:**
- Modify: `reservation_service.py`
- Test: `tests/test_reservation_service.py`

- [ ] **Step 1: Add the Qinghai-Gansu regression test**

Update the imports in `tests/test_reservation_service.py` to include `StoredDocumentContent`, `ReservationItineraryResolver`, and `VisitDateResolution`, then add this test class before `ReservationDraftTests`:

```python
from memory_store import MemoryStore, StoredDocumentContent
from reservation_service import (
    ReservationExtractionItem,
    ReservationItineraryResolver,
    ReservationService,
    VisitDateResolution,
    build_reminder_occurrences,
    calculate_booking_date,
    normalize_extraction_item,
    parse_beijing_datetime_list,
)


class ReservationItineraryResolverTests(unittest.TestCase):
    def test_resolves_qinghai_daily_dates_without_using_trip_start(self):
        document = StoredDocumentContent(
            document_id=1,
            filename="青甘七日自驾行程更新版V3.docx",
            chunks=(
                (
                    "旅行日期：2026年8月16日—8月22日\n"
                    "8月16日｜曹家堡机场 → 西宁市区酒店；"
                    "当天不去青海湖或茶卡盐湖。\n"
                    "8月17日｜西宁 → 日月山 → 青海湖 → "
                    "茶卡盐湖 → 都兰。\n"
                    "8月18日｜都兰 → 察尔汗盐湖 → 大柴旦。\n"
                    "8月19日｜大柴旦 → 翡翠湖 → 黑独山 → 敦煌。\n"
                    "8月20日｜上午参观莫高窟，傍晚游览鸣沙山月牙泉。\n"
                    "8月21日｜敦煌 → 嘉峪关外围经过，但不进入关城 → 张掖。"
                ),
            ),
        )
        resolver = ReservationItineraryResolver()

        resolutions = resolver.resolve(
            (document,),
            (
                "青海湖",
                "茶卡盐湖",
                "察尔汗盐湖",
                "翡翠湖",
                "莫高窟",
                "鸣沙山",
                "嘉峪关",
                "水上雅丹",
            ),
        )

        self.assertEqual(resolutions["青海湖"].dates, (date(2026, 8, 17),))
        self.assertEqual(resolutions["茶卡盐湖"].dates, (date(2026, 8, 17),))
        self.assertEqual(resolutions["察尔汗盐湖"].dates, (date(2026, 8, 18),))
        self.assertEqual(resolutions["翡翠湖"].dates, (date(2026, 8, 19),))
        self.assertEqual(resolutions["莫高窟"].dates, (date(2026, 8, 20),))
        self.assertEqual(resolutions["鸣沙山"].dates, (date(2026, 8, 20),))
        self.assertEqual(resolutions["嘉峪关"].reason, "not_scheduled")
        self.assertEqual(resolutions["水上雅丹"].reason, "not_found")
        self.assertNotIn(
            date(2026, 8, 16),
            tuple(
                candidate
                for resolution in resolutions.values()
                for candidate in resolution.dates
            ),
        )
```

- [ ] **Step 2: Add ambiguity, missing-year, and overlap tests**

Add these methods to `ReservationItineraryResolverTests`:

```python
def test_keeps_multiple_positive_dates_for_manual_confirmation(self):
    document = StoredDocumentContent(
        document_id=1,
        filename="敦煌安排.md",
        chunks=(
            "旅行日期：2026年8月16日—8月22日\n"
            "8月20日或8月21日｜参观莫高窟。",
        ),
    )

    resolution = ReservationItineraryResolver().resolve(
        (document,),
        ("莫高窟",),
    )["莫高窟"]

    self.assertEqual(
        resolution.dates,
        (date(2026, 8, 20), date(2026, 8, 21)),
    )
    self.assertEqual(resolution.reason, "ambiguous")

def test_partial_date_without_explicit_trip_range_is_not_inferred(self):
    document = StoredDocumentContent(
        document_id=1,
        filename="无年份.md",
        chunks=("8月20日｜参观莫高窟。",),
    )

    resolution = ReservationItineraryResolver().resolve(
        (document,),
        ("莫高窟",),
    )["莫高窟"]

    self.assertEqual(resolution, VisitDateResolution((), "not_found"))

def test_overlapping_chunks_do_not_duplicate_date_candidates(self):
    overlap = "8月20日｜上午参观莫高窟，傍晚游览鸣沙山。"
    document = StoredDocumentContent(
        document_id=1,
        filename="重叠.md",
        chunks=(
            "旅行日期：2026年8月16日—8月22日\n" + overlap,
            overlap + "\n8月21日｜前往张掖。",
        ),
    )

    resolution = ReservationItineraryResolver().resolve(
        (document,),
        ("莫高窟",),
    )["莫高窟"]

    self.assertEqual(resolution.dates, (date(2026, 8, 20),))

def test_undated_booking_policy_does_not_inherit_previous_day(self):
    document = StoredDocumentContent(
        document_id=1,
        filename="预约说明.md",
        chunks=(
            "旅行日期：2026年8月16日—8月22日\n"
            "8月20日｜敦煌市内休整。\n"
            "预约说明：水上雅丹提前3天预约。",
        ),
    )

    resolution = ReservationItineraryResolver().resolve(
        (document,),
        ("水上雅丹",),
    )["水上雅丹"]

    self.assertEqual(resolution, VisitDateResolution((), "not_found"))
```

- [ ] **Step 3: Run the resolver tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_reservation_service.ReservationItineraryResolverTests -v
```

Expected: import failure because `ReservationItineraryResolver` and `VisitDateResolution` do not exist.

- [ ] **Step 4: Add resolver records, patterns, and marker constants**

In `reservation_service.py`, keep the existing `Literal`, `Mapping`, `Protocol`, and `Sequence` imports and add these definitions after `ReminderOccurrence`:

```python
ResolutionReason = Literal[
    "resolved",
    "ambiguous",
    "not_scheduled",
    "not_found",
]


@dataclass(frozen=True)
class VisitDateResolution:
    dates: tuple[date, ...]
    reason: ResolutionReason


@dataclass(frozen=True)
class _TripRange:
    start: date
    end: date


@dataclass(frozen=True)
class _DatedItinerarySegment:
    dates: tuple[date, ...]
    lines: tuple[str, ...]


_CHINESE_TRIP_RANGE_RE = re.compile(
    r"(?P<start_year>20\d{2})\s*年\s*"
    r"(?P<start_month>\d{1,2})\s*月\s*"
    r"(?P<start_day>\d{1,2})\s*日?\s*"
    r"(?:—|–|-|~|～|至|到)+\s*"
    r"(?:(?P<end_year>20\d{2})\s*年\s*)?"
    r"(?P<end_month>\d{1,2})\s*月\s*"
    r"(?P<end_day>\d{1,2})\s*日?"
)
_ISO_TRIP_RANGE_RE = re.compile(
    r"(?P<start_year>20\d{2})[-/.]"
    r"(?P<start_month>\d{1,2})[-/.]"
    r"(?P<start_day>\d{1,2})\s*"
    r"(?:—|–|~|～|至|到)+\s*"
    r"(?:(?P<end_year>20\d{2})[-/.])?"
    r"(?P<end_month>\d{1,2})[-/.]"
    r"(?P<end_day>\d{1,2})"
)
_FULL_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:年\s*|[-/.])"
    r"(?P<month>\d{1,2})\s*(?:月\s*|[-/.])"
    r"(?P<day>\d{1,2})\s*日?"
)
_PARTIAL_DATE_RE = re.compile(
    r"(?<![\d年])(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日"
)
_NEGATIVE_VISIT_MARKERS = (
    "不去",
    "不进入",
    "取消",
    "不安排",
    "仅路过",
    "只路过",
    "路过不进",
    "外围经过",
    "经过外围",
    "远观",
    "不游览",
    "不参观",
)
_POSITIVE_ACTIVITY_MARKERS = (
    "→",
    "->",
    "游览",
    "参观",
    "前往",
    "到达",
    "进入",
    "打卡",
    "观看",
    "上午",
    "下午",
    "傍晚",
    "重点安排",
)
```

- [ ] **Step 5: Implement deterministic single-document parsing**

Replace the old `VisitDateExtractor` protocol block only after Task 4 removes its callers. For this task, add the following new protocol and resolver immediately before the existing `VisitDateExtractor` so the current production flow still imports:

```python
class ItineraryResolver(Protocol):
    def resolve(
            self,
            documents: Sequence[object],
            attraction_names: Sequence[str]) -> Mapping[
                str,
                VisitDateResolution,
            ]:
        raise RuntimeError("protocol method")


class ReservationItineraryResolver:
    @staticmethod
    def _safe_date(year: int, month: int, day: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    @classmethod
    def _trip_range(cls, text: str) -> _TripRange | None:
        ranges = set()
        for pattern in (_CHINESE_TRIP_RANGE_RE, _ISO_TRIP_RANGE_RE):
            for match in pattern.finditer(text):
                start_year = int(match.group("start_year"))
                end_year = int(match.group("end_year") or start_year)
                start = cls._safe_date(
                    start_year,
                    int(match.group("start_month")),
                    int(match.group("start_day")),
                )
                end = cls._safe_date(
                    end_year,
                    int(match.group("end_month")),
                    int(match.group("end_day")),
                )
                if start is not None and end is not None and start <= end:
                    ranges.add((start, end))
        if len(ranges) != 1:
            return None
        start, end = next(iter(ranges))
        return _TripRange(start=start, end=end)

    @staticmethod
    def _is_trip_range_line(line: str) -> bool:
        return bool(
            _CHINESE_TRIP_RANGE_RE.search(line)
            or _ISO_TRIP_RANGE_RE.search(line)
        )

    @classmethod
    def _partial_date(
            cls,
            month: int,
            day: int,
            trip_range: _TripRange | None) -> date | None:
        if trip_range is None:
            return None
        candidates = {
            candidate
            for year in range(trip_range.start.year, trip_range.end.year + 1)
            if (candidate := cls._safe_date(year, month, day)) is not None
            and trip_range.start <= candidate <= trip_range.end
        }
        if len(candidates) != 1:
            return None
        return next(iter(candidates))

    @classmethod
    def _line_dates(
            cls,
            line: str,
            trip_range: _TripRange | None) -> tuple[date, ...]:
        if cls._is_trip_range_line(line):
            return ()

        positioned: list[tuple[int, date]] = []
        for match in _FULL_DATE_RE.finditer(line):
            parsed = cls._safe_date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            if parsed is not None:
                positioned.append((match.start(), parsed))
        for match in _PARTIAL_DATE_RE.finditer(line):
            parsed = cls._partial_date(
                int(match.group("month")),
                int(match.group("day")),
                trip_range,
            )
            if parsed is not None:
                positioned.append((match.start(), parsed))

        if not positioned or min(position for position, unused in positioned) > 24:
            return ()
        return tuple(sorted({value for unused, value in positioned}))

    @staticmethod
    def _merge_chunks(chunks: Sequence[str]) -> str:
        merged = ""
        for raw_chunk in chunks:
            chunk = str(raw_chunk or "").strip()
            if not chunk:
                continue
            if not merged:
                merged = chunk
                continue
            overlap = 0
            maximum = min(len(merged), len(chunk), 500)
            for size in range(maximum, 19, -1):
                if merged.endswith(chunk[:size]):
                    overlap = size
                    break
            merged += chunk[overlap:] if overlap else "\n" + chunk
        return merged

    @classmethod
    def _segments(
            cls,
            chunks: Sequence[str]) -> tuple[_DatedItinerarySegment, ...]:
        text = cls._merge_chunks(chunks)
        trip_range = cls._trip_range(text)
        segments = []
        current_dates: tuple[date, ...] = ()
        current_lines: list[str] = []

        for raw_line in text.splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line:
                continue
            dates = cls._line_dates(line, trip_range)
            if dates:
                if current_dates:
                    segments.append(_DatedItinerarySegment(
                        dates=current_dates,
                        lines=tuple(current_lines),
                    ))
                current_dates = dates
                current_lines = [line]
            elif current_dates:
                current_lines.append(line)

        if current_dates:
            segments.append(_DatedItinerarySegment(
                dates=current_dates,
                lines=tuple(current_lines),
            ))
        return tuple(segments)

    @staticmethod
    def _segment_match(
            segment: _DatedItinerarySegment,
            attraction_name: str) -> str:
        matching = [
            (index, line)
            for index, line in enumerate(segment.lines)
            if attraction_name in line
        ]
        if not matching:
            return ""
        if any(
                any(marker in line for marker in _NEGATIVE_VISIT_MARKERS)
                for unused, line in matching):
            return "negative"
        if any(
                index == 0
                or any(
                    marker in line
                    for marker in _POSITIVE_ACTIVITY_MARKERS
                )
                for index, line in matching):
            return "positive"
        return ""

    @classmethod
    def _resolve_document(
            cls,
            document: object,
            attraction_names: Sequence[str]) -> dict[
                str,
                VisitDateResolution,
            ]:
        segments = cls._segments(document.chunks)
        resolutions = {}
        for attraction_name in attraction_names:
            positive_dates = set()
            negative_seen = False
            for segment in segments:
                match = cls._segment_match(segment, attraction_name)
                if match == "positive":
                    positive_dates.update(segment.dates)
                elif match == "negative":
                    negative_seen = True

            dates = tuple(sorted(positive_dates))
            if len(dates) == 1:
                reason: ResolutionReason = "resolved"
            elif len(dates) > 1:
                reason = "ambiguous"
            elif negative_seen:
                reason = "not_scheduled"
            else:
                reason = "not_found"
            resolutions[attraction_name] = VisitDateResolution(dates, reason)
        return resolutions

    def resolve(
            self,
            documents: Sequence[object],
            attraction_names: Sequence[str]) -> Mapping[
                str,
                VisitDateResolution,
            ]:
        names = tuple(dict.fromkeys(
            str(name).strip()
            for name in attraction_names
            if str(name).strip()
        ))
        if not documents:
            return {
                name: VisitDateResolution((), "not_found")
                for name in names
            }
        return self._resolve_document(documents[0], names)
```

- [ ] **Step 6: Run the resolver tests and confirm GREEN**

Run:

```powershell
python -m unittest tests.test_reservation_service.ReservationItineraryResolverTests -v
```

Expected: all five resolver tests pass.

- [ ] **Step 7: Commit the single-document resolver**

```powershell
git add reservation_service.py tests/test_reservation_service.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "feat: parse itinerary visit dates deterministically"
```

### Task 3: Select One Best-Matching Itinerary Document

**Files:**
- Modify: `reservation_service.py`
- Test: `tests/test_reservation_service.py`

- [ ] **Step 1: Add failing multi-document selection tests**

Add these methods to `ReservationItineraryResolverTests`:

```python
def test_selects_highest_coverage_document_without_cross_filling(self):
    newer = StoredDocumentContent(
        document_id=2,
        filename="newer.md",
        chunks=(
            "2026-08-19｜前往翡翠湖。",
        ),
    )
    older = StoredDocumentContent(
        document_id=1,
        filename="older.md",
        chunks=(
            "2026-08-17｜游览青海湖。\n"
            "2026-08-20｜参观莫高窟。",
        ),
    )

    resolutions = ReservationItineraryResolver().resolve(
        (newer, older),
        ("青海湖", "莫高窟", "翡翠湖"),
    )

    self.assertEqual(resolutions["青海湖"].dates, (date(2026, 8, 17),))
    self.assertEqual(resolutions["莫高窟"].dates, (date(2026, 8, 20),))
    self.assertEqual(resolutions["翡翠湖"].reason, "not_found")

def test_equal_coverage_prefers_newest_document(self):
    newer = StoredDocumentContent(
        document_id=2,
        filename="newer.md",
        chunks=("2026-08-19｜前往翡翠湖。",),
    )
    older = StoredDocumentContent(
        document_id=1,
        filename="older.md",
        chunks=("2026-08-17｜游览青海湖。",),
    )

    resolutions = ReservationItineraryResolver().resolve(
        (newer, older),
        ("青海湖", "翡翠湖"),
    )

    self.assertEqual(resolutions["翡翠湖"].dates, (date(2026, 8, 19),))
    self.assertEqual(resolutions["青海湖"].reason, "not_found")

def test_all_zero_scores_select_no_document(self):
    document = StoredDocumentContent(
        document_id=1,
        filename="pass-by.md",
        chunks=(
            "2026-08-21｜嘉峪关外围经过，但不进入关城。",
        ),
    )

    resolution = ReservationItineraryResolver().resolve(
        (document,),
        ("嘉峪关",),
    )["嘉峪关"]

    self.assertEqual(resolution, VisitDateResolution((), "not_found"))
```

- [ ] **Step 2: Run the three tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_reservation_service.ReservationItineraryResolverTests.test_selects_highest_coverage_document_without_cross_filling tests.test_reservation_service.ReservationItineraryResolverTests.test_equal_coverage_prefers_newest_document tests.test_reservation_service.ReservationItineraryResolverTests.test_all_zero_scores_select_no_document -v
```

Expected: the highest-coverage and all-zero tests fail because the resolver currently always uses the first document.

- [ ] **Step 3: Replace first-document selection with score-based selection**

Replace `ReservationItineraryResolver.resolve` with:

```python
def resolve(
        self,
        documents: Sequence[object],
        attraction_names: Sequence[str]) -> Mapping[
            str,
            VisitDateResolution,
        ]:
    names = tuple(dict.fromkeys(
        str(name).strip()
        for name in attraction_names
        if str(name).strip()
    ))
    empty = {
        name: VisitDateResolution((), "not_found")
        for name in names
    }
    best_score = 0
    best_resolutions = None
    for document in documents:
        resolutions = self._resolve_document(document, names)
        score = sum(
            1
            for resolution in resolutions.values()
            if resolution.dates
        )
        if score > best_score:
            best_score = score
            best_resolutions = resolutions

    return best_resolutions if best_resolutions is not None else empty
```

The input from `MemoryStore.list_document_contents` is newest first. Updating only for a strictly larger score preserves the newest document on ties.

- [ ] **Step 4: Run all resolver tests and confirm GREEN**

Run:

```powershell
python -m unittest tests.test_reservation_service.ReservationItineraryResolverTests -v
```

Expected: all resolver tests pass, including the Qinghai-Gansu mapping.

- [ ] **Step 5: Commit document selection**

```powershell
git add reservation_service.py tests/test_reservation_service.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "feat: select one itinerary source per reservation draft"
```

### Task 4: Integrate the Resolver Into Reservation Drafts

**Files:**
- Modify: `reservation_service.py`
- Modify: `bot.py`
- Modify: `onebot_app.py`
- Modify: `tests/test_reservation_service.py`
- Modify: `tests/test_reservation_acceptance.py`

- [ ] **Step 1: Replace the fake date extractor with a fake itinerary resolver**

In `tests/test_reservation_service.py`, remove `DateResponseClient`, remove its now-unused `SimpleNamespace` import, and replace `FakeDateExtractor` with:

```python
class FakeItineraryResolver:
    def __init__(self, resolutions):
        self.resolutions = resolutions
        self.calls = []

    def resolve(self, documents, attraction_names):
        self.calls.append((tuple(documents), tuple(attraction_names)))
        return {
            name: self.resolutions.get(
                name,
                VisitDateResolution((), "not_found"),
            )
            for name in attraction_names
        }
```

Update every `ReservationService` constructor call that currently passes `FakeDateExtractor` so it instead uses the keyword form `itinerary_resolver=FakeItineraryResolver(resolutions)`. Use explicit records such as:

```python
FakeItineraryResolver({
    "青海湖": VisitDateResolution(
        (date(2026, 8, 16),),
        "resolved",
    ),
})
```

For the multiple-date case use:

```python
VisitDateResolution(
    (date(2026, 8, 20), date(2026, 8, 21)),
    "ambiguous",
)
```

Remove the two `LLMVisitDateExtractor` unit tests because that component is removed rather than retained as a fallback.

Update `test_no_reservation_item_skips_date_matching` to use and verify the new fake:

```python
resolver = FakeItineraryResolver({})
service = ReservationService(
    self.store,
    itinerary_resolver=resolver,
)
plan = service.create_draft(
    self.image,
    (self.item("黑独山", False, 0, "none"),),
)
self.assertEqual(plan.items[0].status, "ready")
self.assertEqual(plan.items[0].reminder_policy, "none")
self.assertEqual(resolver.calls, [])
```

- [ ] **Step 2: Add the not-scheduled draft formatting test**

Add this method to `ReservationDraftTests`:

```python
def test_not_scheduled_item_uses_explicit_manual_decision_status(self):
    resolver = FakeItineraryResolver({
        "嘉峪关": VisitDateResolution((), "not_scheduled"),
    })
    service = ReservationService(
        self.store,
        itinerary_resolver=resolver,
    )

    plan = service.create_draft(
        self.image,
        (self.item("嘉峪关", True, 1, "day"),),
    )

    self.assertEqual(plan.items[0].status, "not_scheduled")
    self.assertIn(
        "行程未安排该景点，需要手动决定",
        service.format_draft(plan),
    )
```

In `test_unique_document_date_becomes_ready`, replace the old evidence-length assertion with:

```python
self.assertEqual(len(resolver.calls), 1)
self.assertEqual(resolver.calls[0][1], ("青海湖",))
self.assertEqual(resolver.calls[0][0][0].filename, "行程.md")
```

- [ ] **Step 3: Run reservation draft tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_reservation_service.ReservationDraftTests -v
```

Expected: failures because `ReservationService` does not accept `itinerary_resolver`, does not call `list_document_contents`, and does not persist `not_scheduled`.

- [ ] **Step 4: Replace LLM date extraction in `ReservationService`**

Remove `import json`, `VisitDateExtractor`, and `LLMVisitDateExtractor` from `reservation_service.py`. Replace the constructor and `create_draft` with this structure:

```python
class ReservationService:
    def __init__(
            self,
            store: object,
            itinerary_resolver: ItineraryResolver | None = None):
        self.store = store
        self.itinerary_resolver = (
            itinerary_resolver or ReservationItineraryResolver()
        )

    def create_draft(
            self,
            image: object,
            extraction_items: Sequence[ReservationExtractionItem],
            now: datetime | None = None):
        required_names = tuple(dict.fromkeys(
            extraction.attraction_name
            for extraction in extraction_items
            if extraction.requires_reservation
        ))
        resolutions = {}
        if required_names:
            documents = self.store.list_document_contents(
                image.storage_scope_id
            )
            resolutions = self.itinerary_resolver.resolve(
                documents,
                required_names,
            )

        draft_items = []
        for extraction in extraction_items:
            if not extraction.requires_reservation:
                draft_items.append({
                    "extraction": extraction,
                    "visit_date": None,
                    "booking_date": None,
                    "date_candidates": (),
                    "custom_reminder_times": (),
                    "reminder_policy": "none",
                    "status": "ready",
                })
                continue

            resolution = resolutions.get(
                extraction.attraction_name,
                VisitDateResolution((), "not_found"),
            )
            candidates = resolution.dates
            visit_date = candidates[0] if len(candidates) == 1 else None
            booking_date = (
                calculate_booking_date(
                    visit_date,
                    extraction.advance_value,
                    extraction.advance_unit,
                )
                if visit_date is not None
                else None
            )
            status = "ready" if visit_date is not None else "needs_input"
            if resolution.reason == "not_scheduled":
                status = "not_scheduled"
            draft_items.append({
                "extraction": extraction,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": candidates,
                "custom_reminder_times": (),
                "reminder_policy": "default",
                "status": status,
            })

        return self.store.create_reservation_draft(
            image_id=image.image_id,
            platform=image.platform,
            group_id=image.group_id,
            creator_id=image.uploader_id,
            items=tuple(draft_items),
            now=now,
        )
```

In `format_draft`, immediately after the `requires_reservation` check, add:

```python
if item.status == "not_scheduled":
    lines.append("   游览日期：未确定")
    lines.append("   状态：行程未安排该景点，需要手动决定")
    continue
```

- [ ] **Step 5: Remove obsolete runtime wiring**

In `bot.py`, change:

```python
from reservation_service import LLMVisitDateExtractor, ReservationService
```

to:

```python
from reservation_service import ReservationService
```

Replace the reservation service construction with:

```python
self.reservation_service = ReservationService(self.memory_store)
```

In `onebot_app.py`, make the same import change and replace the construction with:

```python
reservation_service = ReservationService(store)
```

Do not alter `ImageVisionExtractor`; it remains responsible for multimodal image extraction.

- [ ] **Step 6: Update the acceptance fixture to one itinerary document**

In `tests/test_reservation_acceptance.py`, remove `DateMapExtractor`. Replace the loop that inserts one document per attraction with one combined document:

```python
itinerary_text = "\n".join(
    f"{visit_date.isoformat()}｜游览{name}。"
    for name, visit_date in sorted(visit_dates.items())
)
store.add_document(
    group_openid="group-a",
    uploader_openid="member-a",
    filename="完整行程.md",
    sha256="combined-itinerary",
    full_text=itinerary_text,
    chunks=[itinerary_text],
)
service = ReservationService(store)
```

This preserves the original reminder-count acceptance while enforcing the new one-document rule.

- [ ] **Step 7: Run reservation, acceptance, and import tests**

Run:

```powershell
python -m unittest tests.test_reservation_service tests.test_reservation_acceptance tests.test_bot tests.test_onebot_app -v
```

Expected: all selected tests pass; no LLM date extractor import remains.

- [ ] **Step 8: Commit service integration**

```powershell
git add reservation_service.py bot.py onebot_app.py tests/test_reservation_service.py tests/test_reservation_acceptance.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "fix: resolve reservation dates from one itinerary"
```

### Task 5: Extract and Store XLSX Itineraries

**Files:**
- Modify: `requirements.txt`
- Modify: `document_service.py`
- Modify: `tests/test_document_service.py`

- [ ] **Step 1: Add and install the workbook dependency**

Append this line to `requirements.txt`:

```text
openpyxl
```

Run:

```powershell
python -m pip install -r requirements.txt
```

Expected: `openpyxl` is installed or reported as already satisfied.

- [ ] **Step 2: Add a real workbook fixture helper**

Add these imports to `tests/test_document_service.py`:

```python
from datetime import date, datetime
from unittest.mock import Mock, patch

from openpyxl import Workbook
```

Replace the existing `patch` import with the combined `Mock, patch` import, then add:

```python
def make_xlsx_bytes():
    buffer = io.BytesIO()
    workbook = Workbook()
    itinerary = workbook.active
    itinerary.title = "每日行程"
    itinerary.append(["日期", "行程", "人数", "确认", "出发时间"])
    itinerary.append([
        date(2026, 8, 17),
        "西宁 → 青海湖 → 茶卡盐湖 → 都兰",
        4,
        True,
        datetime(2026, 8, 17, 7, 30),
    ])
    itinerary.merge_cells("A4:B4")
    itinerary["A4"] = "集合地点：西宁"

    hidden = workbook.create_sheet("隐藏备注")
    hidden.sheet_state = "hidden"
    hidden["A1"] = "不应进入知识库"
    workbook.create_sheet("空表")
    lodging = workbook.create_sheet("住宿")
    lodging.append(["日期", "住宿地"])
    lodging.append([date(2026, 8, 17), "都兰"])

    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()
```

- [ ] **Step 3: Add failing extraction and cached-value-mode tests**

Add these methods to `DocumentServiceTests`:

```python
def test_extracts_visible_xlsx_rows_with_normalized_dates(self):
    text = self.service._extract_text("plan.xlsx", make_xlsx_bytes())

    self.assertIn("[工作表：每日行程]", text)
    self.assertIn("日期 | 行程 | 人数 | 确认 | 出发时间", text)
    self.assertIn("2026-08-17", text)
    self.assertIn("2026-08-17 07:30", text)
    self.assertIn("西宁 → 青海湖 → 茶卡盐湖 → 都兰", text)
    self.assertIn("4 | TRUE", text)
    self.assertEqual(text.count("集合地点：西宁"), 1)
    self.assertIn("[工作表：住宿]", text)
    self.assertIn("2026-08-17 | 都兰", text)
    self.assertNotIn("隐藏备注", text)
    self.assertNotIn("不应进入知识库", text)
    self.assertNotIn("[工作表：空表]", text)

def test_xlsx_requests_saved_formula_results_without_recalculation(self):
    worksheet = SimpleNamespace(
        title="统计",
        sheet_state="visible",
        iter_rows=lambda values_only: iter([("总里程", 450)]),
    )
    workbook = SimpleNamespace(
        worksheets=[worksheet],
        close=Mock(),
    )
    with patch(
            "document_service.load_workbook",
            return_value=workbook) as load:
        text = self.service._extract_text("plan.xlsx", b"xlsx")

    self.assertEqual(text, "[工作表：统计]\n总里程 | 450")
    self.assertTrue(load.call_args.kwargs["read_only"])
    self.assertTrue(load.call_args.kwargs["data_only"])
    workbook.close.assert_called_once_with()

def test_corrupt_xlsx_is_rejected_without_partial_text(self):
    with self.assertRaisesRegex(ValueError, "Excel 文件.*无法读取"):
        self.service._extract_text("broken.xlsx", b"not-a-workbook")
```

- [ ] **Step 4: Add failing ingestion and legacy-format tests**

Add these methods to `DocumentServiceTests`:

```python
def test_ingests_xlsx_into_group_document_context(self):
    attachment = SimpleNamespace(
        filename="plan.xlsx",
        url="https://example.test/plan.xlsx",
        size=100,
    )
    with patch.object(
            self.service,
            "_download_attachment",
            return_value=make_xlsx_bytes()):
        result = self.service.ingest_attachments(
            "group-xlsx",
            "member",
            [attachment],
        )

    self.assertTrue(result.handled)
    self.assertIn("已保存旅行文档", result.reply)
    context = self.service.memory_store.build_document_context(
        "group-xlsx",
        "茶卡盐湖",
    )
    self.assertIn("茶卡盐湖", context)

def test_prepare_attachments_accepts_xlsx_without_persisting(self):
    attachment = SimpleNamespace(
        filename="plan.xlsx",
        url="https://example.test/plan.xlsx",
        size=100,
    )
    with patch.object(
            self.service,
            "_download_attachment",
            return_value=make_xlsx_bytes()):
        prepared = self.service.prepare_attachments([attachment])

    self.assertEqual(len(prepared), 1)
    self.assertEqual(prepared[0].filename, "plan.xlsx")
    self.assertIn("茶卡盐湖", prepared[0].full_text)
    self.assertEqual(
        self.service.memory_store.build_document_context(
            "group",
            "茶卡盐湖",
        ),
        "",
    )

def test_legacy_xls_requires_conversion_to_xlsx(self):
    attachment = SimpleNamespace(
        filename="old-plan.xls",
        url="https://example.test/old-plan.xls",
        size=100,
    )

    result = self.service.ingest_attachments(
        "group",
        "member",
        [attachment],
    )

    self.assertTrue(result.handled)
    self.assertIn("另存为 .xlsx", result.reply)
```

- [ ] **Step 5: Run XLSX tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_document_service.DocumentServiceTests.test_extracts_visible_xlsx_rows_with_normalized_dates tests.test_document_service.DocumentServiceTests.test_xlsx_requests_saved_formula_results_without_recalculation tests.test_document_service.DocumentServiceTests.test_corrupt_xlsx_is_rejected_without_partial_text tests.test_document_service.DocumentServiceTests.test_ingests_xlsx_into_group_document_context tests.test_document_service.DocumentServiceTests.test_prepare_attachments_accepts_xlsx_without_persisting tests.test_document_service.DocumentServiceTests.test_legacy_xls_requires_conversion_to_xlsx -v
```

Expected: failures because `.xlsx` is decoded as plain text or ignored and `.xls` has no dedicated reply.

- [ ] **Step 6: Implement workbook normalization**

Add these imports to `document_service.py`:

```python
from datetime import date, datetime
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
```

Change the supported extensions and add the legacy Excel extension:

```python
SUPPORTED_EXTENSIONS = {".docx", ".txt", ".md", ".xlsx"}
LEGACY_EXCEL_EXTENSION = ".xls"
```

Add these helpers immediately before `_extract_text`:

```python
@staticmethod
def _excel_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).strip()

@staticmethod
def _extract_xlsx_text(data: bytes) -> str:
    try:
        workbook = load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
        )
        try:
            parts = []
            for worksheet in workbook.worksheets:
                if worksheet.sheet_state != "visible":
                    continue
                rows = []
                for raw_row in worksheet.iter_rows(values_only=True):
                    values = [
                        DocumentService._excel_value(value)
                        for value in raw_row
                    ]
                    while values and not values[-1]:
                        values.pop()
                    if any(values):
                        rows.append(" | ".join(values))
                if rows:
                    parts.append(f"[工作表：{worksheet.title}]")
                    parts.extend(rows)
            return "\n".join(parts)
        finally:
            workbook.close()
    except (
            BadZipFile,
            InvalidFileException,
            KeyError,
            OSError,
            ValueError,
    ) as exc:
        raise ValueError("Excel 文件损坏、加密或无法读取") from exc
```

Update `_extract_text` so the format dispatch is:

```python
if extension == ".docx":
    document = Document(io.BytesIO(data))
    parts = [
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
elif extension == ".xlsx":
    text = DocumentService._extract_xlsx_text(data)
else:
    text = DocumentService._decode_text(data)
```

In `ingest_attachments`, collect `.xls` filenames separately and append:

```python
messages.append(
    f"暂不支持旧版 Excel 文件 {filename}，"
    "请另存为 .xlsx 后重新上传。"
)
```

Treat the presence of either legacy Word or legacy Excel filenames as `handled=True` even when no supported attachment exists.

- [ ] **Step 7: Run all document service tests and confirm GREEN**

Run:

```powershell
python -m unittest tests.test_document_service -v
```

Expected: all document tests pass, including existing Word extraction and summary behavior.

- [ ] **Step 8: Commit XLSX ingestion**

```powershell
git add requirements.txt document_service.py tests/test_document_service.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "feat: ingest xlsx itinerary documents"
```

### Task 6: Update Private Upload Guidance and User Documentation

**Files:**
- Modify: `upload_binding.py`
- Modify: `README.md`
- Modify: `tests/test_upload_binding.py`

- [ ] **Step 1: Add failing private-upload message tests**

In `test_issue_binding_returns_private_upload_instructions`, add:

```python
self.assertIn(".xlsx", reply)
```

In `test_private_code_redeems_target_group`, add:

```python
self.assertIn(".xlsx", result.reply)
```

Add this test to `UploadBindingServiceTests`:

```python
def test_legacy_xls_private_upload_requests_xlsx_conversion(self):
    self.documents.prepared = ()
    self.service.issue_binding("group-a", "member-a")
    self.service.handle_private_message(
        "private-user",
        "QG-ABC234",
        [],
    )

    result = self.handle_attachment(
        "private-event-old-xls",
        [SimpleNamespace(filename="old-plan.xls")],
    )

    self.assertIn("另存为 .xlsx", result.reply)
    self.assertIn("本次绑定已失效", result.reply)
```

- [ ] **Step 2: Run the message tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_upload_binding.UploadBindingServiceTests.test_issue_binding_returns_private_upload_instructions tests.test_upload_binding.UploadBindingServiceTests.test_private_code_redeems_target_group tests.test_upload_binding.UploadBindingServiceTests.test_legacy_xls_private_upload_requests_xlsx_conversion -v
```

Expected: `.xlsx` assertions fail and `.xls` receives the generic unsupported message.

- [ ] **Step 3: Update private upload strings and `.xls` branching**

Add this import to `upload_binding.py`:

```python
from pathlib import Path
```

Change all three supported-format prompts to use:

```python
".docx、.txt、.md 或 .xlsx 文件"
```

Replace the `if not prepared` reply construction with:

```python
if not prepared:
    legacy_excel = any(
        Path(str(getattr(item, "filename", "") or "")).suffix.lower()
        == ".xls"
        for item in attachments
    )
    if legacy_excel:
        reply = (
            "暂不支持旧版 Excel，请另存为 .xlsx 后重新上传。"
            "本次绑定已失效，请回到目标群重新申请。"
        )
    else:
        reply = (
            "该附件格式暂不支持。请发送 .docx、.txt、.md 或 .xlsx "
            "文件，本次绑定已失效，请回到目标群重新申请。"
        )
```

- [ ] **Step 4: Update README behavior and troubleshooting text**

Make these exact documentation changes:

- Change the feature list entry from `.docx/.txt/.md` to `.docx/.txt/.md/.xlsx`.
- Add `.xlsx` to the supported-format list under “上传长期旅行资料”.
- Change the legacy sentence to: `旧版 .doc 和 .xls 暂不支持，请分别转换为 .docx 和 .xlsx。`
- Change the troubleshooting upload line to `.docx/.txt/.md/.xlsx`.
- State that Excel imports all visible non-empty worksheets, cell values, dates, and saved formula results, while ignoring hidden sheets, charts, images, comments, and formatting.
- Change the reservation-date description to state that one best-matching itinerary document is selected for the whole draft and dates are not mixed across versions.

- [ ] **Step 5: Run upload and documentation-adjacent tests**

Run:

```powershell
python -m unittest tests.test_upload_binding tests.test_commands tests.test_bot_application -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit guidance updates**

```powershell
git add upload_binding.py README.md tests/test_upload_binding.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "docs: advertise xlsx itinerary uploads"
```

### Task 7: Add XLSX Reservation Acceptance and Run Full Regression

**Files:**
- Modify: `tests/test_reservation_acceptance.py`
- Verify: all production and test files changed in Tasks 1-6

- [ ] **Step 1: Add XLSX acceptance imports and helper**

At the top of `tests/test_reservation_acceptance.py`, add:

```python
import io
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

from document_service import DocumentService
```

Add this helper before `ReservationAcceptanceTests`:

```python
def make_acceptance_xlsx():
    buffer = io.BytesIO()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "每日行程"
    worksheet.append(["日期", "行程"])
    worksheet.append([
        date(2026, 8, 17),
        "西宁 → 青海湖 → 茶卡盐湖 → 都兰",
    ])
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()
```

- [ ] **Step 2: Add the end-to-end XLSX acceptance test**

Add this method to `ReservationAcceptanceTests`:

```python
def test_xlsx_upload_is_queryable_and_drives_reservation_dates(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = MemoryStore(Path(temp_dir) / "xlsx-acceptance.db")
        documents = DocumentService(store)
        attachment = SimpleNamespace(
            filename="青甘行程.xlsx",
            url="https://example.test/青甘行程.xlsx",
            size=100,
        )
        with patch.object(
                documents,
                "_download_attachment",
                return_value=make_acceptance_xlsx()):
            ingest = documents.ingest_attachments(
                "group-xlsx",
                "member-a",
                [attachment],
            )

        self.assertTrue(ingest.handled)
        self.assertIn(
            "茶卡盐湖",
            store.build_document_context("group-xlsx", "茶卡盐湖"),
        )

        image, unused = store.create_reservation_image(
            storage_scope_id="group-xlsx",
            platform="qq_official",
            group_id="group-xlsx",
            uploader_id="member-a",
            sha256="e" * 64,
            file_path="data/images/ee/xlsx.jpg",
            content_type="image/jpeg",
            byte_size=100,
            model_id="fake-model",
        )
        items = tuple(
            normalize_extraction_item({
                "attraction_name": name,
                "requires_reservation": True,
                "advance_value": 1,
                "advance_unit": "day",
                "confidence": 0.99,
            })
            for name in ("青海湖", "茶卡盐湖")
        )

        draft = ReservationService(store).create_draft(image, items)

        self.assertEqual(
            tuple(item.visit_date for item in draft.items),
            (date(2026, 8, 17), date(2026, 8, 17)),
        )
        self.assertEqual(
            tuple(item.booking_date for item in draft.items),
            (date(2026, 8, 16), date(2026, 8, 16)),
        )
```

- [ ] **Step 3: Run the acceptance module**

Run:

```powershell
python -m unittest tests.test_reservation_acceptance -v
```

Expected: the existing ten-item reminder acceptance and the new XLSX acceptance both pass.

- [ ] **Step 4: Run the complete test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all baseline 196 tests plus every new test pass with zero failures and zero errors.

- [ ] **Step 5: Run repository consistency checks**

Run:

```powershell
git diff --check
git status --short
rg -n "LLMVisitDateExtractor|date_extractor" . -g "*.py"
```

Expected:

- `git diff --check` prints nothing.
- `git status --short` lists only the intended Task 7 test change before commit.
- the ripgrep command returns no production or test Python references.

- [ ] **Step 6: Commit the end-to-end acceptance**

```powershell
git add tests/test_reservation_acceptance.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "test: cover xlsx reservation itinerary flow"
```

- [ ] **Step 7: Record final verification state**

Run:

```powershell
git status --short --branch
git log --oneline -8
```

Expected: a clean `feature/image-reservation-reminders` worktree containing the seven implementation commits after the documentation commits. Do not push or merge until the user explicitly chooses that action.
