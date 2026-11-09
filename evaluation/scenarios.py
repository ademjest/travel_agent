"""Explicit scenario catalog. Entries describe complete recipes, never import them eagerly."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    module: str
    modes: tuple[str, ...]
    description: str


SCENARIOS = {
    'conversation_workflow': Scenario('evaluation.cases.conversation_workflow', ('offline','real_model_fixed_tools','live_provider'),
        'Blank creation, 24 existing journeys and 20 post-delivery audit slices; bound draft actions and contracts through isolated Web persistence; bounded real-model fixed-source samples.'),
    'model_routing': Scenario('evaluation.cases.model_routing', ('offline','live_provider'),
        'U6 connection/profile isolation, migration and fixed-input capability probes; no user conversation data.'),
    'intercity_planning': Scenario('evaluation.cases.intercity_planning', ('offline','live_provider'),
        'U4/U5 transport windows, snapshot confirmation and workflow lifecycle in an isolated Web app; bounded providers.'),
    'rail_query': Scenario('evaluation.cases.rail_query', ('offline','live_provider'),
        'U3 unified read entry, missing fields, scope and recovery; bounded live NL/model and rail acceptance.'),
    'rail_mcp': Scenario('evaluation.cases.rail_mcp', ('offline', 'live_provider'),
        'U2 fake/Remote contract and single-leg sample gate; independently capped MCP operations, no model calls.'),
    'official_intent_smoke': Scenario('evaluation.cases.official_intent_smoke', ('real_model_fixed_tools',),
        'Three bounded real intent compiler calls for standalone websites, without provider or production writes.'),
    'official_search_diagnostics': Scenario('evaluation.cases.official_search_diagnostics', ('offline','live_provider'),
        'Ten Tavily queries, twenty unique pages, fifty physical HTTP requests; capture and replay without model calls.'),
    'official_navigation': Scenario('evaluation.cases.official_navigation', ('offline',),
        'U1 navigation discovery, fact-state isolation, source limits and checkpoint reuse with fixed public snapshots.'),
    'evaluate_tasks': Scenario('evaluation.cases.evaluate_tasks', ('offline', 'real_model_fixed_tools'),
        'Task state and side-effect contracts against fixed external snapshots.'),
    'evaluate_read_only': Scenario('evaluation.cases.evaluate_read_only', ('offline',),
        'Per-operation fact coverage and grounded-answer contracts.'),
    'evaluate_summary_quality': Scenario('evaluation.cases.evaluate_summary_quality', ('offline',),
        'Summary publication, source evidence, freshness and fault cases.'),
    'smoke_summary_live': Scenario('evaluation.cases.smoke_summary_live', ('real_model_fixed_tools',),
        'Explicit bounded real-model summary smoke in temporary data.'),
}

ALIASES = {'tasks': 'evaluate_tasks', 'read_only': 'evaluate_read_only', 'summary': 'evaluate_summary_quality'}

SCENARIOS['tool_catalog'] = Scenario('evaluation.cases.tool_catalog', ('offline', 'real_model_fixed_tools'),
    'Paired legacy/compact tool selection; synthetic data, no business execution, persisted real-token budget.')

SCENARIOS['build_eval_cases'] = Scenario('evaluation.cases.build_eval_cases', ('offline',), 'Build versioned task scenarios. Split is fixed before any live-model evaluation.')

SCENARIOS['evaluate_local_embedding'] = Scenario('evaluation.cases.evaluate_local_embedding', ('offline',), 'Isolated lexical/BGE retrieval comparison. No chat models or production data.')

SCENARIOS['evaluate_retrieval'] = Scenario('evaluation.cases.evaluate_retrieval', ('offline',), 'Compare FTS/keyword retrieval with the optional local hybrid index.')

SCENARIOS['evaluate_token_memory'] = Scenario('evaluation.cases.evaluate_token_memory', ('offline',), 'Export a privacy-safe report using a strictly read-only SQLite connection.')

SCENARIOS['evaluate_v3_experiments'] = Scenario('evaluation.cases.evaluate_v3_experiments', ('offline', 'real_model_fixed_tools'), 'Bounded intent-model probe. Offline validates the plan; --live requires complete limits.')

SCENARIOS['evaluate_v3_memory_contracts'] = Scenario('evaluation.cases.evaluate_v3_memory_contracts', ('offline',), '50 offline summary boundary fixtures, plus cache metric contracts; no quality/cost claims.')

SCENARIOS['evaluate_v3_state_flows'] = Scenario('evaluation.cases.evaluate_v3_state_flows', ('offline',), 'Forty distinct production-assembly state scenarios complement the 120 legacy cases.')

SCENARIOS['probe_official_rules'] = Scenario('evaluation.cases.probe_official_rules', ('live_provider',), 'Read only reviewed public policy pages; no models, Tavily or Amap calls.')

SCENARIOS['smoke_booking_discovery'] = Scenario('evaluation.cases.smoke_booking_discovery', ('live_provider',), 'Live official policy and model-led discovery; temporary storage, no QQ messages.')

SCENARIOS['smoke_inbox_media'] = Scenario('evaluation.cases.smoke_inbox_media', ('live_provider',), 'Queued real-model weather/image flow. Uses synthetic pixels, temporary DB and no QQ sends.')

SCENARIOS['smoke_local'] = Scenario('evaluation.cases.smoke_local', ('offline', 'live_provider'), 'Opt-in local integration checks. Never sends a QQ message or uses production storage.')

SCENARIOS['smoke_local_embedding'] = Scenario('evaluation.cases.smoke_local_embedding', ('offline',), 'Exercise real local encoding and runtime wiring against an isolated database.')

SCENARIOS['smoke_research_sources'] = Scenario('evaluation.cases.smoke_research_sources', ('live_provider',), 'Live search-policy checks with temporary state and no QQ delivery.')

SCENARIOS['smoke_research_web'] = Scenario('evaluation.cases.smoke_research_web', ('live_provider',), 'Actual model/public-page check with temporary Web data and recording-only delivery.')

SCENARIOS['smoke_scheduled_query'] = Scenario('evaluation.cases.smoke_scheduled_query', ('live_provider',), 'Wait one real minute, then query the real data source. Temporary DB and no QQ sends.')

SCENARIOS['smoke_semantic_compiler'] = Scenario('evaluation.cases.smoke_semantic_compiler', ('real_model_fixed_tools',), 'Live semantic compiler smoke check; parses only and never performs a business write.')

SCENARIOS['smoke_semantic_reminder'] = Scenario('evaluation.cases.smoke_semantic_reminder', ('live_provider',), 'Exercise real semantic reminder continuation through Web API with disposable data.')

SCENARIOS['smoke_summary_web'] = Scenario('evaluation.cases.smoke_summary_web', ('real_model_fixed_tools',), 'Bounded live summary/Web acceptance using synthetic historical fixtures and an isolated database.')

SCENARIOS['smoke_task_conversation'] = Scenario('evaluation.cases.smoke_task_conversation', ('live_provider',), 'Real model/tools through the OneBot application chain, using temporary data and no QQ sends.')

SCENARIOS['smoke_trip_accommodation'] = Scenario('evaluation.cases.smoke_trip_accommodation', ('live_provider',), 'Verify a mixed itinerary/hotel/budget request with real APIs and temporary Web data.')

SCENARIOS['smoke_trip_context'] = Scenario('evaluation.cases.smoke_trip_context', ('live_provider',), 'Real model/AMap verification; all business data is isolated in a temporary Web store.')

SCENARIOS['smoke_trip_flow'] = Scenario('evaluation.cases.smoke_trip_flow', ('live_provider',), 'Real model and POI data; full OneBot trip flow with temporary DB and no QQ sends.')

SCENARIOS['smoke_v3_web_copy'] = Scenario('evaluation.cases.smoke_v3_web_copy', ('offline',), 'Web health smoke against a migrated COPY, with all workers and external services off.')

SCENARIOS['smoke_web'] = Scenario('evaluation.cases.smoke_web', ('live_provider',), 'Real Web API/model/tool smoke test; disposable database, no QQ transport.')

SCENARIOS['verify_route_places_live'] = Scenario('evaluation.cases.verify_route_places_live', ('live_provider',), 'Explicit, bounded Amap integration check; never calls a language model or production DB.')

SCENARIOS['verify_v3_regression'] = Scenario('evaluation.cases.verify_v3_regression', ('offline',), 'Save regression logs and evidence with tool/version metadata.')
