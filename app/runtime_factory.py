from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agents.travel_agent import TravelAgent
from app.bot_application import TravelBotApplication
from core.background_supervisor import BackgroundSupervisor
from core.chat_transport import MessageTransport, ReplyRenderer
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from services.document_service import DocumentService
from services.maintenance import MaintenanceService
from services.outbox_worker import OutboxWorker
from services.reminder_scheduler import ReminderScheduler
from services.reservation_draft_creator import ReservationDraftCreator
from services.reservation_service import ReservationService
from services.travel_service import TravelService
from services.upload_binding import UploadBindingService
from services.vision_service import ImageVisionExtractor, ReservationImageService
from tools.reservation_tools import AgentToolRouter


@dataclass(frozen=True)
class RuntimeComponents:
    store: MemoryStore
    travel_service: TravelService
    reservation_service: ReservationService
    reservation_draft_creator: ReservationDraftCreator
    tool_router: AgentToolRouter
    travel_agent: TravelAgent | None
    document_service: DocumentService
    upload_binding_service: UploadBindingService
    image_extractor: ImageVisionExtractor | None
    reservation_image_service: ReservationImageService
    outbox_worker: OutboxWorker
    reminder_scheduler: ReminderScheduler
    maintenance_service: MaintenanceService
    application: TravelBotApplication
    supervisor: BackgroundSupervisor


def build_runtime(
        settings: Settings,
        *,
        platform: str,
        transport: MessageTransport,
        reply_renderer: ReplyRenderer,
        group_allowed: Callable[[str], bool],
        store: MemoryStore | None = None) -> RuntimeComponents:
    memory_store = store or MemoryStore()
    travel_service = TravelService(settings)
    reservation_service = ReservationService(memory_store)
    image_extractor = (
        ImageVisionExtractor(
            model_id=settings.llm_model_id,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        if settings.llm_configured
        else None
    )
    reservation_image_service = ReservationImageService(
        memory_store,
        image_extractor,
        image_root=memory_store.database_path.parent / "images",
    )
    reservation_draft_creator = ReservationDraftCreator(
        reservation_image_service,
        reservation_service,
    )
    tool_router = AgentToolRouter(
        travel_service,
        reservation_service,
        reservation_draft_creator,
    )
    travel_agent = (
        TravelAgent(settings, tool_router.execute)
        if settings.llm_configured
        else None
    )
    document_service = DocumentService(
        memory_store,
        summarizer=(
            travel_agent.summarize_document if travel_agent else None
        ),
    )
    upload_service = UploadBindingService(
        memory_store,
        document_service,
        group_allowed=group_allowed,
    )
    outbox_worker = OutboxWorker(platform, memory_store, transport)
    reminder_scheduler = ReminderScheduler(
        platform=platform,
        store=memory_store,
        renderer=reply_renderer,
        group_allowed=group_allowed,
    )
    maintenance_service = MaintenanceService(
        memory_store,
        reservation_image_service.image_root,
    )
    application = TravelBotApplication(
        store=memory_store,
        travel_service=travel_service,
        travel_agent=travel_agent,
        document_service=document_service,
        upload_binding_service=upload_service,
        outbox_worker=outbox_worker,
        reply_renderer=reply_renderer,
        reminder_scheduler=reminder_scheduler,
        reservation_service=reservation_service,
        tool_router=tool_router,
        group_allowed=group_allowed,
    )
    return RuntimeComponents(
        store=memory_store,
        travel_service=travel_service,
        reservation_service=reservation_service,
        reservation_draft_creator=reservation_draft_creator,
        tool_router=tool_router,
        travel_agent=travel_agent,
        document_service=document_service,
        upload_binding_service=upload_service,
        image_extractor=image_extractor,
        reservation_image_service=reservation_image_service,
        outbox_worker=outbox_worker,
        reminder_scheduler=reminder_scheduler,
        maintenance_service=maintenance_service,
        application=application,
        supervisor=BackgroundSupervisor(),
    )
