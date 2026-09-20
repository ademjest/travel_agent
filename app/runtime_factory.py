from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable
from openai import OpenAI

from agents.travel_agent import TravelAgent
from agents.reminder_intent import ReminderIntentParser
from agents.trip_planner import TripPlanner
from agents.intent_compiler import IntentCompiler
from app.bot_application import TravelBotApplication
from core.background_supervisor import BackgroundSupervisor
from core.chat_transport import MessageTransport, ReplyRenderer
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from services.document_service import DocumentService
from services.maintenance import MaintenanceService
from services.outbox_worker import OutboxWorker
from services.personal_reminder_service import PersonalReminderService
from services.booking_reminder_service import BookingReminderService
from services.trip_service import TripService
from services.image_context_service import ImageContextService
from services.scheduled_query_service import ScheduledQueryService
from services.policy_watch_service import PolicyWatchService
from services.booking_policy import BookingPolicyResolver
from services.semantic_task_service import SemanticTaskService
from services.preference_service import PreferenceService
from services.research_service import ResearchService
from infrastructure.search_client import SearchClient
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
    model_client = (ModelGateway(memory_store, OpenAI(api_key=settings.llm_api_key,
        base_url=settings.llm_base_url, timeout=90, max_retries=1)) if settings.llm_configured else None)
    travel_service = TravelService(settings)
    reservation_service = ReservationService(memory_store)
    image_extractor = (
        ImageVisionExtractor(
            model_id=settings.llm_model_id,
            client=model_client,
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
        TravelAgent(settings, tool_router.execute_result, client=model_client)
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
    outbox_worker = OutboxWorker(platform, memory_store, transport, group_allowed=group_allowed)
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
        personal_reminder_service=PersonalReminderService(memory_store, intent_parser=(
            ReminderIntentParser(travel_agent.client, travel_agent.model) if travel_agent else None)),
        booking_reminder_service=BookingReminderService(memory_store,
            client=travel_agent.client if travel_agent else None, model=travel_agent.model if travel_agent else ''),
        trip_service=TripService(memory_store, TripPlanner(
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else '', travel_service.amap)),
        image_context_service=ImageContextService(memory_store, reservation_image_service._download,
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else ''),
        scheduled_query_service=ScheduledQueryService(memory_store, travel_service,
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else ''),
        policy_watch_service=PolicyWatchService(memory_store, BookingPolicyResolver(),
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else ''),
    )
    application.preference_service = PreferenceService(memory_store.preferences)
    application.research_service = ResearchService(memory_store,
        SearchClient(os.getenv('SEARCH_API_KEY', ''), os.getenv('SEARCH_BASE_URL', 'https://api.tavily.com')),
        model_client, settings.llm_model_id, application.trip_service)
    if model_client is not None:
        application.semantic_task_service = SemanticTaskService(
            memory_store,
            IntentCompiler(model_client, settings.llm_model_id),
            trip_service=application.trip_service,
            reminder_service=application.personal_reminder_service,
            mode=os.getenv('TRAVEL_SEMANTIC_MODE', 'execute'),
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
