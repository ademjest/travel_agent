# Reservation Draft Auto-Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically refresh unresolved reservation drafts from the latest stored itinerary before listing or confirming them, while adding an explicit refresh command and a static QQ keyboard button.

**Architecture:** Keep reservation mutations in the deterministic command/service path. `ReservationService` resolves only unresolved draft items, `MemoryStore` applies all item-state changes atomically, and existing list/confirm commands invoke refresh lazily. The QQ keyboard only prefills the explicit command; the LLM receives guidance but no reservation side-effect tools.

**Tech Stack:** Python 3.11, SQLite, `unittest`, QQ Bot Markdown keyboard, existing `ReservationItineraryResolver`

---

## File Map

- Modify `commands.py`: parse explicit refresh and invalid confirmation guidance commands.
- Modify `qq_ui.py`: expose the static refresh button and command documentation.
- Modify `memory_store.py`: atomically update unresolved reservation item resolution fields.
- Modify `reservation_service.py`: refresh drafts and hook refresh into list/confirm command paths.
- Modify `travel_agent.py`: state the deterministic reservation-command boundary.
- Modify `README.md`: document automatic and explicit refresh behavior.
- Modify `tests/test_commands.py`: command parser regression tests.
- Modify `tests/test_qq_ui.py`: static keyboard button regression test.
- Modify `tests/test_reservation_store.py`: transaction, ownership, and overwrite protection tests.
- Modify `tests/test_reservation_service.py`: service refresh, list, confirm, and guidance tests.
- Modify `tests/test_reservation_acceptance.py`: reproduce image-first, Excel-later workflow.
- Modify `tests/test_travel_agent.py`: prompt boundary regression test.

### Task 1: Explicit Commands And Static QQ Button

**Files:**
- Modify: `commands.py:29-157`
- Modify: `qq_ui.py:6-105`
- Test: `tests/test_commands.py`
- Test: `tests/test_qq_ui.py`
- Test: `tests/test_bot_application.py`

- [ ] **Step 1: Write failing parser tests**

Add to `CommandTests`:

```python
def test_reservation_refresh_command(self):
    command = parse_command("刷新预约 R-20260723-001")

    self.assertEqual(command.name, "reservation_refresh")
    self.assertEqual(command.args, ("R-20260723-001",))

def test_invalid_natural_confirmation_gets_deterministic_guidance(self):
    command = parse_command("确认创建预约提醒")

    self.assertEqual(command.name, "reservation_confirm_help")
    self.assertEqual(command.args, ())
```

- [ ] **Step 2: Write the failing static-button test**

Extend `QQGroupUiTests.test_keyboard_uses_official_group_command_actions`:

```python
self.assertIn("查看预约提醒", commands)
self.assertIn("刷新预约 ", commands)
```

Extend `test_help_and_menu_use_markdown_with_keyboard`:

```python
self.assertIn("刷新预约 R-20260722-001", payload["markdown"]["content"])
```

- [ ] **Step 3: Write the failing deterministic-routing test**

Add to `TravelBotApplicationTests`:

```python
async def test_invalid_confirmation_phrase_uses_reservation_service_not_llm(self):
    event = self.group_event(
        "reservation-confirm-help",
        "确认创建预约提醒",
    )

    await self.application.handle(event)

    self.assertEqual(len(self.reservation_service.commands), 1)
    self.assertEqual(
        self.reservation_service.commands[0][0].name,
        "reservation_confirm_help",
    )
    self.assertEqual(self.travel_agent.calls, [])
```

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
conda run -n agent python -m unittest tests.test_commands tests.test_qq_ui tests.test_bot_application -v
```

Expected: failures because `刷新预约` and the confirmation-help phrase parse as `unknown`, the latter reaches the LLM, and the keyboard lacks `刷新预约 `.

- [ ] **Step 5: Implement the command parser**

In `commands.py`, add:

```python
REFRESH_PLAN_RE = re.compile(rf"^刷新预约\s+({PLAN_CODE})$")
```

In `parse_command`, immediately after `reservation_list` handling, add:

```python
if command == "确认创建预约提醒":
    return Command(name="reservation_confirm_help")

match = REFRESH_PLAN_RE.fullmatch(command)
if match:
    return Command(name="reservation_refresh", args=match.groups())
```

- [ ] **Step 6: Add the static QQ button and help text**

Change the final row in `BUTTON_ROWS` to:

```python
(
    ("reservations", "⏰ 预约提醒", "查看预约提醒", 0),
    ("reservation-refresh", "🔄 刷新预约", "刷新预约 ", 1),
),
```

Add this line under the reservation command examples in `build_help_markdown`:

```text
- 刷新：`刷新预约 R-20260722-001`
```

- [ ] **Step 7: Run the focused tests and verify GREEN**

Run:

```powershell
conda run -n agent python -m unittest tests.test_commands tests.test_qq_ui tests.test_bot_application -v
```

Expected: all command and QQ UI tests pass.

- [ ] **Step 8: Commit Task 1**

```powershell
git add commands.py qq_ui.py tests/test_commands.py tests/test_qq_ui.py tests/test_bot_application.py
git commit -m "feat: add reservation refresh command"
```

### Task 2: Atomic Draft Resolution Updates

**Files:**
- Modify: `memory_store.py:1390-1436`
- Test: `tests/test_reservation_store.py`

- [ ] **Step 1: Write the failing atomic-update test**

Add a helper to `ReservationStoreTests`:

```python
def create_refreshable_plan(self):
    image, unused = self.store.create_reservation_image(
        storage_scope_id="group-a",
        platform="qq_official",
        group_id="group-a",
        uploader_id="member-a",
        sha256="f" * 64,
        file_path="data/images/ff/image.jpg",
        content_type="image/jpeg",
        byte_size=10,
        model_id="vision-model",
    )
    extraction = ReservationExtractionItem(
        attraction_name="青海湖",
        price_text="",
        opening_hours="",
        requires_reservation=True,
        advance_value=1,
        advance_unit="day",
        booking_channel="",
        source_text="青海湖提前一天预约",
        confidence=0.99,
    )
    return self.store.create_reservation_draft(
        image_id=image.image_id,
        platform="qq_official",
        group_id="group-a",
        creator_id="member-a",
        items=({
            "extraction": extraction,
            "visit_date": None,
            "booking_date": None,
            "date_candidates": (),
            "custom_reminder_times": (),
            "reminder_policy": "default",
            "status": "needs_input",
        },),
    )
```

Add the test:

```python
def test_refresh_draft_items_updates_only_owned_unresolved_items(self):
    plan = self.create_refreshable_plan()

    changed = self.store.refresh_reservation_draft_items(
        platform="qq_official",
        group_id="group-a",
        creator_id="member-a",
        plan_code=plan.plan_code,
        updates=({
            "item_index": 1,
            "visit_date": date(2026, 8, 17),
            "booking_date": date(2026, 8, 16),
            "date_candidates": (date(2026, 8, 17),),
            "status": "ready",
        },),
    )

    self.assertEqual(changed, 1)
    loaded = self.store.get_reservation_plan(
        "qq_official", "group-a", plan.plan_code
    )
    self.assertEqual(loaded.items[0].visit_date, date(2026, 8, 17))
    self.assertEqual(loaded.items[0].booking_date, date(2026, 8, 16))
    self.assertEqual(loaded.items[0].status, "ready")

    wrong_owner = self.store.refresh_reservation_draft_items(
        "qq_official",
        "group-a",
        "member-b",
        plan.plan_code,
        ({
            "item_index": 1,
            "visit_date": date(2026, 8, 18),
            "booking_date": date(2026, 8, 17),
            "date_candidates": (date(2026, 8, 18),),
            "status": "ready",
        },),
    )
    self.assertEqual(wrong_owner, 0)
```

- [ ] **Step 2: Write the failing overwrite-protection test**

```python
def test_refresh_draft_items_does_not_overwrite_ready_item(self):
    plan = self.create_refreshable_plan()
    self.store.update_reservation_draft_item_date(
        "qq_official",
        "group-a",
        "member-a",
        plan.plan_code,
        1,
        date(2026, 8, 20),
        date(2026, 8, 19),
    )

    changed = self.store.refresh_reservation_draft_items(
        "qq_official",
        "group-a",
        "member-a",
        plan.plan_code,
        ({
            "item_index": 1,
            "visit_date": date(2026, 8, 17),
            "booking_date": date(2026, 8, 16),
            "date_candidates": (date(2026, 8, 17),),
            "status": "ready",
        },),
    )

    self.assertEqual(changed, 0)
    loaded = self.store.get_reservation_plan(
        "qq_official", "group-a", plan.plan_code
    )
    self.assertEqual(loaded.items[0].visit_date, date(2026, 8, 20))
```

- [ ] **Step 3: Run the store tests and verify RED**

Run:

```powershell
conda run -n agent python -m unittest tests.test_reservation_store -v
```

Expected: errors because `MemoryStore.refresh_reservation_draft_items` does not exist.

- [ ] **Step 4: Implement the atomic store method**

Add to `MemoryStore` after `update_reservation_draft_item_date`:

```python
def refresh_reservation_draft_items(
        self,
        platform: str,
        group_id: str,
        creator_id: str,
        plan_code: str,
        updates: tuple[dict[str, object], ...],
        now: datetime | None = None) -> int:
    updated_at = now or datetime.now(timezone.utc)
    changed = 0
    with self._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = connection.execute(
            """
            SELECT id FROM reservation_plans
            WHERE platform = ?
              AND group_id = ?
              AND creator_id = ?
              AND plan_code = ?
              AND status = 'draft'
            """,
            (platform, group_id, creator_id, plan_code),
        ).fetchone()
        if plan is None:
            return 0
        for update in updates:
            visit_date = update["visit_date"]
            booking_date = update["booking_date"]
            cursor = connection.execute(
                """
                UPDATE reservation_items
                SET visit_date = ?,
                    booking_date = ?,
                    date_candidates_json = ?,
                    status = ?,
                    updated_at = ?
                WHERE plan_id = ?
                  AND item_index = ?
                  AND requires_reservation = 1
                  AND visit_date IS NULL
                  AND status IN ('needs_input', 'not_scheduled')
                """,
                (
                    visit_date.isoformat() if visit_date else None,
                    booking_date.isoformat() if booking_date else None,
                    json.dumps([
                        value.isoformat()
                        for value in update["date_candidates"]
                    ]),
                    update["status"],
                    updated_at.isoformat(),
                    int(plan["id"]),
                    int(update["item_index"]),
                ),
            )
            changed += cursor.rowcount
    return changed
```

- [ ] **Step 5: Run the store tests and verify GREEN**

Run:

```powershell
conda run -n agent python -m unittest tests.test_reservation_store -v
```

Expected: all reservation store tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add memory_store.py tests/test_reservation_store.py
git commit -m "feat: refresh unresolved reservation items atomically"
```

### Task 3: Lazy Refresh In Reservation Service

**Files:**
- Modify: `reservation_service.py:1-1018`
- Test: `tests/test_reservation_service.py`
- Test: `tests/test_reservation_acceptance.py`

- [ ] **Step 1: Write failing service-refresh tests**

Add to `ReservationDraftTests`:

```python
def test_refresh_plan_resolves_unresolved_items_from_latest_documents(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 17),), "resolved"
        )
    }

    result = service.refresh_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(result.updated_count, 1)
    self.assertEqual(result.plan.items[0].visit_date, date(2026, 8, 17))
    self.assertEqual(result.plan.items[0].booking_date, date(2026, 8, 16))

def test_refresh_plan_preserves_manual_ready_date(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    service.complete_item_date(
        "qq_official",
        "group-a",
        "member-a",
        plan.plan_code,
        1,
        date(2026, 8, 20),
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 17),), "resolved"
        )
    }

    result = service.refresh_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(result.updated_count, 0)
    self.assertEqual(result.plan.items[0].visit_date, date(2026, 8, 20))

def test_refresh_plan_keeps_ambiguous_candidates_for_manual_choice(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("莫高窟", True, 1, "month"),),
    )
    resolver.resolutions = {
        "莫高窟": VisitDateResolution(
            (date(2026, 8, 20), date(2026, 8, 21)),
            "ambiguous",
        )
    }

    result = service.refresh_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(result.plan.items[0].status, "needs_input")
    self.assertEqual(
        result.plan.items[0].date_candidates,
        (date(2026, 8, 20), date(2026, 8, 21)),
    )

def test_refresh_plan_promotes_newly_scheduled_item_to_ready(self):
    resolver = FakeItineraryResolver({
        "嘉峪关": VisitDateResolution((), "not_scheduled")
    })
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("嘉峪关", True, 1, "day"),),
    )
    resolver.resolutions = {
        "嘉峪关": VisitDateResolution(
            (date(2026, 8, 21),), "resolved"
        )
    }

    result = service.refresh_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(result.updated_count, 1)
    self.assertEqual(result.plan.items[0].status, "ready")
    self.assertEqual(result.plan.items[0].visit_date, date(2026, 8, 21))

def test_refresh_plan_rejects_another_creator(self):
    service = ReservationService(
        self.store,
        itinerary_resolver=FakeItineraryResolver({}),
    )
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )

    with self.assertRaisesRegex(PermissionError, "创建者"):
        service.refresh_plan(
            "qq_official", "group-a", "member-b", plan.plan_code
        )
```

- [ ] **Step 2: Write the failing image-first, Excel-later acceptance test**

Add to `ReservationReminderAcceptanceTests`:

```python
def test_existing_draft_refreshes_after_later_xlsx_upload(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = MemoryStore(Path(temp_dir) / "late-xlsx.db")
        image, unused = store.create_reservation_image(
            storage_scope_id="group-late",
            platform="qq_official",
            group_id="group-late",
            uploader_id="member-a",
            sha256="9" * 64,
            file_path="data/images/99/late.jpg",
            content_type="image/jpeg",
            byte_size=100,
            model_id="fake-model",
        )
        item = normalize_extraction_item({
            "attraction_name": "青海湖",
            "requires_reservation": True,
            "advance_value": 1,
            "advance_unit": "day",
            "confidence": 0.99,
        })
        service = ReservationService(store)
        draft = service.create_draft(image, (item,))
        self.assertEqual(draft.items[0].status, "needs_input")

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
            documents.ingest_attachments(
                "group-late", "member-a", [attachment]
            )

        listed = service.list_plans(
            "qq_official", "group-late", "member-a"
        )
        self.assertEqual(listed[0].items[0].visit_date, date(2026, 8, 17))
        self.assertEqual(listed[0].items[0].booking_date, date(2026, 8, 16))
```

- [ ] **Step 3: Write failing list and confirmation tests**

Add these imports to `tests/test_reservation_service.py`:

```python
from types import SimpleNamespace

from commands import parse_command
```

Add to `ReservationManagementTests`:

```python
def test_list_plans_refreshes_draft_before_formatting(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 17),), "resolved"
        )
    }

    plans = service.list_plans("qq_official", "group-a", "member-a")

    self.assertEqual(plans[0].plan_code, plan.plan_code)
    self.assertEqual(plans[0].items[0].status, "ready")

def test_confirmation_refreshes_draft_before_creating_reminders(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 17),), "resolved"
        )
    }

    confirmed = service.confirm_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(confirmed.status, "confirmed")
    reminders = self.store.list_reservation_reminders(
        "qq_official", "group-a", "member-a"
    )
    self.assertEqual(len(reminders), 2)

def test_refresh_plan_does_not_change_confirmed_plan(self):
    resolver = FakeItineraryResolver({
        "青海湖": VisitDateResolution(
            (date(2026, 8, 16),), "resolved"
        )
    })
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    confirmed = service.confirm_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 18),), "resolved"
        )
    }

    result = service.refresh_plan(
        "qq_official", "group-a", "member-a", plan.plan_code
    )

    self.assertEqual(result.updated_count, 0)
    self.assertEqual(result.plan.status, "confirmed")
    self.assertEqual(
        result.plan.items[0].visit_date,
        confirmed.items[0].visit_date,
    )

def test_explicit_refresh_command_returns_updated_draft(self):
    resolver = FakeItineraryResolver({})
    service = ReservationService(self.store, itinerary_resolver=resolver)
    plan = service.create_draft(
        self.image,
        (self.item("青海湖", True, 1, "day"),),
    )
    resolver.resolutions = {
        "青海湖": VisitDateResolution(
            (date(2026, 8, 17),), "resolved"
        )
    }
    event = SimpleNamespace(
        platform="qq_official",
        scope_id="group-a",
        sender_id="member-a",
    )

    reply = service.handle_command(
        parse_command(f"刷新预约 {plan.plan_code}"),
        event,
    )

    self.assertIn("刷新 1 个项目", reply)
    self.assertIn("2026-08-17", reply)

def test_confirmation_help_command_returns_exact_syntax(self):
    service = ReservationService(
        self.store,
        itinerary_resolver=FakeItineraryResolver({}),
    )
    event = SimpleNamespace(
        platform="qq_official",
        scope_id="group-a",
        sender_id="member-a",
    )

    reply = service.handle_command(
        parse_command("确认创建预约提醒"),
        event,
    )

    self.assertIn("查看预约提醒", reply)
    self.assertIn("确认预约 R-YYYYMMDD-NNN", reply)
```

- [ ] **Step 4: Run the service tests and verify RED**

Run:

```powershell
conda run -n agent python -m unittest tests.test_reservation_service tests.test_reservation_acceptance -v
```

Expected: failures because `refresh_plan` and automatic refresh hooks do not exist.

- [ ] **Step 5: Implement the refresh result and service method**

Import storage scoping:

```python
from chat_transport import storage_scope_id
```

Add:

```python
@dataclass(frozen=True)
class ReservationRefreshResult:
    plan: object
    updated_count: int
```

Add to `ReservationService`:

```python
def refresh_plan(
        self,
        platform: str,
        group_id: str,
        creator_id: str,
        plan_code: str) -> ReservationRefreshResult:
    plan = self.store.get_reservation_plan(platform, group_id, plan_code)
    if plan is None:
        raise ValueError("预约计划不存在")
    if plan.creator_id != creator_id:
        raise PermissionError("只有创建者可以刷新预约计划")
    if plan.status != "draft":
        return ReservationRefreshResult(plan, 0)

    items = tuple(
        item
        for item in plan.items
        if item.requires_reservation
        and item.visit_date is None
        and item.status in {"needs_input", "not_scheduled"}
    )
    if not items:
        return ReservationRefreshResult(plan, 0)

    documents = self.store.list_document_contents(
        storage_scope_id(platform, group_id)
    )
    resolutions = self.itinerary_resolver.resolve(
        documents,
        tuple(item.attraction_name for item in items),
    )
    updates = []
    for item in items:
        resolution = resolutions.get(
            item.attraction_name,
            VisitDateResolution((), "not_found"),
        )
        visit_date = (
            resolution.dates[0]
            if len(resolution.dates) == 1
            else None
        )
        booking_date = (
            calculate_booking_date(
                visit_date,
                item.advance_value,
                item.advance_unit,
            )
            if visit_date is not None
            else None
        )
        status = "ready" if visit_date is not None else "needs_input"
        if resolution.reason == "not_scheduled":
            status = "not_scheduled"
        desired = (
            visit_date,
            booking_date,
            resolution.dates,
            status,
        )
        current = (
            item.visit_date,
            item.booking_date,
            item.date_candidates,
            item.status,
        )
        if desired != current:
            updates.append({
                "item_index": item.item_index,
                "visit_date": visit_date,
                "booking_date": booking_date,
                "date_candidates": resolution.dates,
                "status": status,
            })

    changed = self.store.refresh_reservation_draft_items(
        platform,
        group_id,
        creator_id,
        plan_code,
        tuple(updates),
    )
    refreshed = self.store.get_reservation_plan(
        platform, group_id, plan_code
    )
    if refreshed is None:
        raise RuntimeError("reservation draft disappeared during refresh")
    return ReservationRefreshResult(refreshed, changed)
```

- [ ] **Step 6: Hook refresh into list and confirm**

In `list_plans`, refresh only drafts before returning:

```python
refreshed = []
for plan in plans:
    if plan.status == "draft":
        plan = self.refresh_plan(
            platform, group_id, creator_id, plan.plan_code
        ).plan
    refreshed.append(plan)
return tuple(refreshed)
```

At the start of `confirm_plan`, replace the direct plan lookup with:

```python
plan = self.refresh_plan(
    platform, group_id, creator_id, plan_code
).plan
```

Keep the existing confirmation and idempotency logic unchanged after that line.

- [ ] **Step 7: Implement explicit refresh and guidance command replies**

In `handle_command`, add before `reservation_confirm`:

```python
if name == "reservation_refresh":
    result = self.refresh_plan(
        platform, group_id, creator_id, args[0]
    )
    if result.plan.status != "draft":
        raise ValueError("只有未确认的预约草稿可以刷新")
    if result.updated_count:
        prefix = f"已按最新行程刷新 {result.updated_count} 个项目。\n"
    else:
        prefix = "未找到可自动补齐的新日期。\n"
    return prefix + self.format_draft(result.plan)
if name == "reservation_confirm_help":
    return (
        "请先发送“查看预约提醒”获取计划编号，"
        "再发送“确认预约 R-YYYYMMDD-NNN”。"
    )
```

- [ ] **Step 8: Run service, acceptance, and command tests and verify GREEN**

Run:

```powershell
conda run -n agent python -m unittest tests.test_reservation_service tests.test_reservation_acceptance tests.test_commands -v
```

Expected: all tests pass, including repeated-confirmation idempotency.

- [ ] **Step 9: Commit Task 3**

```powershell
git add reservation_service.py tests/test_reservation_service.py tests/test_reservation_acceptance.py
git commit -m "feat: refresh reservation drafts from latest itinerary"
```

### Task 4: Acceptance Flow, LLM Boundary, And Documentation

**Files:**
- Modify: `travel_agent.py:122-140`
- Modify: `README.md:251-283`
- Modify: `tests/test_travel_agent.py`

- [ ] **Step 1: Write the failing LLM-boundary test**

Add to `TravelAgentTests`:

```python
def test_system_prompt_keeps_reservation_writes_in_fixed_commands(self):
    client = FakeClient([
        completion(assistant_message(content="请使用固定预约命令。"))
    ])
    agent = TravelAgent(
        self.settings,
        lambda name, arguments: "not used",
        client=client,
    )

    agent.run("根据 Excel 创建预约提醒")

    system_prompt = client.completions.requests[0]["messages"][0]["content"]
    self.assertIn("查看预约提醒", system_prompt)
    self.assertIn("刷新预约 R-", system_prompt)
    self.assertIn("确认预约 R-", system_prompt)
    self.assertIn("不得声称已经创建或修改预约提醒", system_prompt)
```

- [ ] **Step 2: Run the boundary test and verify RED**

Run:

```powershell
conda run -n agent python -m unittest tests.test_travel_agent.TravelAgentTests.test_system_prompt_keeps_reservation_writes_in_fixed_commands -v
```

Expected: failure because the system prompt does not yet document deterministic reservation commands.

- [ ] **Step 3: Add the LLM reservation boundary**

Add a rule to `_system_prompt` after the existing no-hidden-chain rule:

```text
10. 预约计划和提醒的查看、刷新、确认、修改与取消只能使用确定性命令。不得声称已经创建或修改预约提醒，也不得编造命令。需要操作时指导用户使用“查看预约提醒”“刷新预约 R-计划编号”或“确认预约 R-计划编号”。
```

- [ ] **Step 4: Update README usage documentation**

In the reservation reminder section, add:

```text
刷新预约 R-20260722-001
```

Document these rules:

- `查看预约提醒` automatically refreshes unresolved drafts from the latest stored itinerary.
- `确认预约` performs the same refresh before validation.
- The static “刷新预约” button prefills `刷新预约 ` and requires a plan code.
- Refresh never overwrites manually completed dates or confirmed plans.
- For the image-first, Excel-later flow, users do not need to resend the image.

- [ ] **Step 5: Run the focused boundary tests and verify GREEN**

Run:

```powershell
conda run -n agent python -m unittest tests.test_bot_application tests.test_travel_agent -v
```

Expected: all focused tests pass.

- [ ] **Step 6: Run the complete test suite and compile check**

Run:

```powershell
conda run -n agent python -m unittest discover -s tests -v
conda run -n agent python -m compileall -q .
git diff --check
```

Expected: all tests pass, compile check exits `0`, and `git diff --check` reports no errors.

- [ ] **Step 7: Commit Task 4**

```powershell
git add travel_agent.py README.md tests/test_travel_agent.py
git commit -m "fix: keep reservation writes in deterministic commands"
```

## Final Verification

- [ ] Confirm `git status -sb` is clean.
- [ ] Confirm `git log -5 --oneline` contains the four task commits after the design and plan commits.
- [ ] Confirm the old screenshot flow works without re-uploading the reservation image:

```text
查看预约提醒
刷新预约 R-20260723-001
确认预约 R-20260723-001
```

- [ ] Do not push or merge until the user chooses the integration path.
