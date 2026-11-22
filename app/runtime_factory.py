from __future__ import annotations

from dataclasses import dataclass, replace
import os
from typing import Callable
from infrastructure.model_client import create_model_client as OpenAI

from agents.travel_agent import TravelAgent
from tools.trip.planner import TripPlanner
from agents.intent_compiler import IntentCompiler
from app.conversation_context import ContextBuilder
from app.bot_application import TravelBotApplication
from core.background_supervisor import BackgroundSupervisor
from core.chat_transport import MessageTransport, ReplyRenderer
from core.settings import Settings
from infrastructure.memory_store import MemoryStore
from infrastructure.model_gateway import ModelGateway
from infrastructure.skill_manager import get_skill_manager
from tools.materials import MaterialsTool
from services.maintenance import MaintenanceService
from services.outbox_worker import OutboxWorker
from tools.trip.tool import TripTool
from tools.trip.presentation import TripAnswerWriter
from tools.reminders.policy import BookingPolicyResolver
from services.official_sources import OfficialSources
from app.request_router import RequestRouter
from tools.preferences import PreferencesTool
from tools.research.tool import ResearchTool
from tools.trip.tool import PlanningArtifacts
from app.conversation_context import ConversationSummaryService
from infrastructure.search_client import SearchClient
from services.reminder_scheduler import ReminderScheduler
from tools.travel_query import TravelQueryTool
from adapters.upload_binding import UploadBindingService
from tools.reservation.tool import ReservationTool, ImageVisionExtractor, ReservationImageService
from tools.registry import AgentToolRouter
from tools.search import SearchTool
from tools.reminders.tool import RemindersTool


@dataclass(frozen=True)
class RuntimeComponents:
    store: MemoryStore
    travel_service: TravelQueryTool
    reservation_service: ReservationTool
    reservation_draft_creator: ReservationTool
    tool_router: AgentToolRouter
    travel_agent: TravelAgent | None
    document_service: MaterialsTool
    upload_binding_service: UploadBindingService
    image_extractor: ImageVisionExtractor | None
    reservation_image_service: ReservationImageService
    outbox_worker: OutboxWorker
    reminder_scheduler: ReminderScheduler
    maintenance_service: MaintenanceService
    application: TravelBotApplication
    supervisor: BackgroundSupervisor
    rail_client: object | None = None

    @property
    def tool_registry(self):
        return self.tool_router.registry


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
    from infrastructure.mcp_client import RailMCPConfig, RemoteMCPClient
    from infrastructure.rail_client import RailClient, validate_rail_tools
    try:
        rail_config = RailMCPConfig.parse(settings.rail_mcp_config)
    except ValueError:
        rail_config = RailMCPConfig()  # Optional configuration cannot prevent historical reads or Web startup.
    rail_mcp = RemoteMCPClient(rail_config, validate_tools=validate_rail_tools) if rail_config.enabled else None
    rail_client = RailClient(rail_mcp) if rail_mcp else None
    skill_manager = get_skill_manager()
    skill_manager.snapshot()  # Validate required assets even when no model is configured.
    official_sources = OfficialSources()
    booking_resolver = BookingPolicyResolver(registry=official_sources)
    from infrastructure.local_embedding import configure_local_index
    configure_local_index(memory_store)
    model_client = None
    model_id = settings.llm_model_id
    if settings.llm_configured:
        configuration=settings.model_configuration
        connection,profile=configuration.select('unspecified',force_default=True)
        model_id=profile.model_id
        model_client=ModelGateway(memory_store,client if client is not None else OpenAI(api_key=connection.api_key,
            base_url=connection.base_url,timeout=90,max_retries=0),default_model=model_id,
            skill_manager=skill_manager,configuration=configuration,client_factory=OpenAI)
        settings=replace(settings,llm_model_id=model_id)
    travel_service = TravelQueryTool(settings, store=memory_store)
    travel_service.rail = rail_client
    if amap_client is not None:
        travel_service.amap=amap_client
    if travel_service.manages_read_retries:
        travel_service.amap.diagnostics = memory_store.diagnostics.record
        travel_service.amap.checkpoint_store = memory_store
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
    reservation_service = ReservationTool(memory_store, image_service=reservation_image_service)
    reservation_draft_creator = reservation_service
    from infrastructure.web_reader import read_page
    configured_reader = reader if reader is not None else (
        lambda url, *, url_allowed=None: read_page(url, diagnostics=memory_store.diagnostics.record, url_allowed=url_allowed))
    public_search = search_client if search_client is not None else SearchClient(os.getenv('SEARCH_API_KEY', ''), os.getenv('SEARCH_BASE_URL', 'https://api.tavily.com'))
    website_search = SearchTool(memory_store,public_search,reader=configured_reader,registry=official_sources,
        enabled=os.getenv('RESEARCH_OFFICIAL_LINKS_ENABLED','true').lower()=='true')
    tool_router = AgentToolRouter(travel_service, reservation_service, memory_store.tool_artifacts,
                                 delivery_repository=memory_store.delivery,search_tool=website_search)
    travel_service.tool_executor = tool_router.execute_result
    travel_agent = (
        TravelAgent(settings, tool_router.execute_result, client=model_client,
                    catalog_resolver=tool_router.freeze_catalog)
        if settings.llm_configured
        else None
    )
    document_service = MaterialsTool(memory_store, client=model_client, model=settings.llm_model_id)
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
    reminders_tool = RemindersTool(memory_store, travel_service, resolver=booking_resolver,
        client=model_client, model=settings.llm_model_id, clock=clock, tool_executor=tool_router.execute_result)
    from infrastructure.wendao_client import WendaoClient
    from tools.hotel_prices.tool import HotelQuotes
    research_tool = ResearchTool(memory_store,
        public_search,
        model_client, settings.llm_model_id,
        reader=configured_reader, amap=travel_service.amap, official_sources=official_sources,
        website_search=website_search,
        wendao=WendaoClient(os.getenv('WENDAO_API_KEY', ''), os.getenv('WENDAO_ENABLED', 'false').lower() == 'true',
                           diagnostics=memory_store.diagnostics.record))
    travel_service.hotel_prices=HotelQuotes(memory_store,research_tool.wendao,model_client,settings.llm_model_id,
        amap=travel_service.amap,clock=clock,enabled=os.getenv('HOTEL_QUOTES_ENABLED','true').lower()=='true')
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
        read_only_service=travel_service,
        research_service=research_tool,
        group_allowed=group_allowed,
        reminders_tool=reminders_tool,
        personal_reminder_service=reminders_tool,
        booking_reminder_service=reminders_tool,
        trip_service=TripTool(memory_store, TripPlanner(
            travel_agent.client if travel_agent else None, travel_agent.model if travel_agent else '', travel_service.amap),
            research_reader=research_tool,rail_executor=tool_router.execute_result,
            answer_writer=TripAnswerWriter(memory_store,model_client,settings.llm_model_id) if model_client else None,
            hotel_quotes=travel_service.hotel_prices),
        image_context_service=document_service,
        scheduled_query_service=reminders_tool,
        policy_watch_service=reminders_tool,
        context_builder=ContextBuilder(memory_store, ConversationSummaryService(
            memory_store.conversation_summaries, model_client, settings.llm_model_id,
            diagnostics=memory_store.diagnostics.summary), diagnostics=memory_store.diagnostics.context),
    )
    application.preference_service = PreferencesTool(memory_store.preferences)
    application.skill_manager = skill_manager
    application.planning_service = PlanningArtifacts(memory_store.planning)
    if isinstance(application.research_service.search,SearchClient):
        application.research_service.search.diagnostics=memory_store.diagnostics.record
    if clock is not None:
        for service in (reminders_tool, application.trip_service, reminder_scheduler, outbox_worker, maintenance_service):
            service.clock=clock
    if model_client is not None:
        application.semantic_task_service = RequestRouter(
            memory_store,
            IntentCompiler(model_client, settings.llm_model_id, diagnostics=memory_store.diagnostics),
            trip_service=application.trip_service,
            preferences_tool=application.preference_service,
            planning_tool=application.planning_service,
            reservation_tool=reservation_service,
            reminder_service=reminders_tool,
            research_service=application.research_service,
            travel_service=travel_service,
            tool_executor=tool_router.execute_result,
            read_only_service=application.read_only_service,
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
        supervisor=BackgroundSupervisor(resources=(rail_mcp,) if rail_mcp else ()),
        rail_client=rail_client,
    )
