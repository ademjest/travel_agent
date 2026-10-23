from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable
from openai import OpenAI

from agents.travel_agent import TravelAgent
from agents.reminder_intent import ReminderIntentParser
from agents.trip_planner import TripPlanner
from agents.intent_compiler import IntentCompiler
from agents.context_builder import ContextBuilder
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
from services.official_sources import OfficialSources
from services.semantic_task_service import SemanticTaskService
from services.preference_service import PreferenceService
from services.research_service import ResearchService
from services.planning_service import PlanningService
from services.conversation_summary_service import ConversationSummaryService
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
        store: MemoryStore | None = None,
        client=None, amap_client=None, search_client=None, reader=None, clock=None,
        semantic_mode=None) -> RuntimeComponents:
    memory_store = store or MemoryStore()
    official_sources = OfficialSources()
    booking_resolver = BookingPolicyResolver(registry=official_sources)
    from infrastructure.local_embedding import configure_local_index
    configure_local_index(memory_store)
    model_client = (ModelGateway(memory_store, client if client is not None else OpenAI(api_key=settings.llm_api_key,
        base_url=settings.llm_base_url, timeout=90, max_retries=0)) if settings.llm_configured else None)
    travel_service = TravelService(settings)
    if amap_client is not None:
        travel_service.amap=amap_client
    if travel_service.manages_read_retries:
        travel_service.amap.diagnostics = memory_store.diagnostics.record
        travel_service.amap.checkpoint_store = memory_store
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
        memory_store.tool_artifacts,
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
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else '',
            tool_executor=tool_router.execute_result),
        policy_watch_service=PolicyWatchService(memory_store, booking_resolver,
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else ''),
        context_builder=ContextBuilder(memory_store, ConversationSummaryService(
            memory_store.conversation_summaries, model_client, settings.llm_model_id,
            diagnostics=memory_store.diagnostics.summary), diagnostics=memory_store.diagnostics.context),
    )
    application.preference_service = PreferenceService(memory_store.preferences)
    application.planning_service = PlanningService(memory_store.planning)
    from infrastructure.wendao_client import WendaoClient
    application.research_service = ResearchService(memory_store,
        search_client if search_client is not None else SearchClient(os.getenv('SEARCH_API_KEY', ''), os.getenv('SEARCH_BASE_URL', 'https://api.tavily.com')),
        model_client, settings.llm_model_id, application.trip_service,
        amap=travel_service.amap, official_sources=official_sources,
        wendao=WendaoClient(os.getenv('WENDAO_API_KEY', ''), os.getenv('WENDAO_ENABLED', 'false').lower() == 'true',
                           diagnostics=memory_store.diagnostics.record))
    application.trip_service.research_service = application.research_service
    application.booking_reminder_service.resolver = booking_resolver
    if reader is not None:
        application.research_service.reader=reader
    else:
        from infrastructure.web_reader import read_page
        application.research_service.reader=lambda url, **kwargs: read_page(url,diagnostics=memory_store.diagnostics.record, **kwargs)
    if isinstance(application.research_service.search,SearchClient):
        application.research_service.search.diagnostics=memory_store.diagnostics.record
    if clock is not None:
        for service in (application.personal_reminder_service,application.booking_reminder_service,
                        application.trip_service,application.scheduled_query_service,application.policy_watch_service,
                        reminder_scheduler,outbox_worker,maintenance_service):
            service.clock=clock
    if model_client is not None:
        application.semantic_task_service = SemanticTaskService(
            memory_store,
            IntentCompiler(model_client, settings.llm_model_id, diagnostics=memory_store.diagnostics),
            trip_service=application.trip_service,
            reminder_service=application.personal_reminder_service,
            research_service=application.research_service,
            travel_service=travel_service,
            tool_executor=tool_router.execute_result,
            mode=semantic_mode if semantic_mode is not None else os.getenv('TRAVEL_SEMANTIC_MODE', 'execute'),
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
