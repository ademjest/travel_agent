"""Offline source-boundary checks. Never imports the application or rewrites files."""
import ast
import json
from pathlib import Path

try:
    from .capture_upgrade_baseline import ROOT, source_paths
except ImportError:
    from capture_upgrade_baseline import ROOT, source_paths


RETIRED_PATHS = (
    'tools/trip/hotel_quotes.py',
    'tools/trip/hotel_catalog.py',
    'agents/clarification.py',
    'agents/context_builder.py',
    'agents/reminder_intent.py',
    'agents/research_search_planner.py',
    'agents/travel_decision.py',
    'agents/trip_planner.py',
    'core/attraction_answers.py',
    'core/attraction_facts.py',
    'core/attraction_projection.py',
    'core/attraction_rules.py',
    'core/conversation_summary.py',
    'core/place_recommendations.py',
    'core/planning_dates.py',
    'core/planning_evidence.py',
    'core/read_only_answer.py',
    'core/research_evidence.py',
    'core/rule_intent.py',
    'core/rule_semantics.py',
    'core/source_applicability.py',
    'core/summary_contract.py',
    'core/summary_semantics.py',
    'core/travel_answer.py',
    'core/trip_diff.py',
    'core/trip_updates.py',
    'core/wendao_answer.py',
    'core/wendao_blocks.py',
    'scripts/build_eval_cases.py',
    'scripts/evaluate_local_embedding.py',
    'scripts/evaluate_read_only.py',
    'scripts/evaluate_retrieval.py',
    'scripts/evaluate_summary_quality.py',
    'scripts/evaluate_tasks.py',
    'scripts/evaluate_token_memory.py',
    'scripts/evaluate_v3_experiments.py',
    'scripts/evaluate_v3_memory_contracts.py',
    'scripts/evaluate_v3_state_flows.py',
    'scripts/probe_official_rules.py',
    'scripts/smoke_booking_discovery.py',
    'scripts/smoke_inbox_media.py',
    'scripts/smoke_local.py',
    'scripts/smoke_local_embedding.py',
    'scripts/smoke_research_sources.py',
    'scripts/smoke_research_web.py',
    'scripts/smoke_scheduled_query.py',
    'scripts/smoke_semantic_compiler.py',
    'scripts/smoke_semantic_reminder.py',
    'scripts/smoke_summary_live.py',
    'scripts/smoke_summary_web.py',
    'scripts/smoke_task_conversation.py',
    'scripts/smoke_trip_accommodation.py',
    'scripts/smoke_trip_context.py',
    'scripts/smoke_trip_flow.py',
    'scripts/smoke_v3_web_copy.py',
    'scripts/smoke_web.py',
    'scripts/verify_route_places_live.py',
    'scripts/verify_v3_regression.py',
    'services/attraction_research.py',
    'services/booking_policy.py',
    'services/booking_reminder_service.py',
    'services/conversation_summary_service.py',
    'services/document_service.py',
    'services/image_context_service.py',
    'services/personal_reminder_service.py',
    'services/planning_service.py',
    'services/policy_watch_service.py',
    'services/preference_service.py',
    'services/read_only_service.py',
    'services/reminder_time.py',
    'services/research_context.py',
    'services/research_service.py',
    'services/reservation_draft_creator.py',
    'services/reservation_service.py',
    'services/scheduled_query_service.py',
    'services/semantic_task_service.py',
    'services/travel_answer_writer.py',
    'services/travel_service.py',
    'services/trip_service.py',
    'services/upload_binding.py',
    'services/vision_service.py',
    'services/wendao_research.py',
    'tools/agent_tools.py',
    'tools/reservation_tools.py',
)
RETIRED_MODULES = frozenset(path[:-3].replace("/", ".") for path in RETIRED_PATHS)


def check(root=ROOT):
    paths=source_paths(root)
    names={p.relative_to(root).as_posix() for p in paths}
    required={'frontend/vite.config.ts','frontend/tsconfig.json','frontend/index.html',
              'frontend/package.json','frontend/package-lock.json','.github/workflows/tests.yml',
              'deploy/napcat/Dockerfile','deploy/napcat/compose.yml','start-web.cmd',
              'data/official_sources.json','data/attraction_aliases.json'}
    errors=[f'Missing release input: {name}' for name in sorted(required-names)]
    errors.extend('Retired source is still present: '+name for name in RETIRED_PATHS if (root/name).exists())
    business_inputs = {'tools/search.py','tools/travel_query.py','tools/materials.py','tools/preferences.py',
        'tools/hotel_prices/tool.py','tools/hotel_prices/quotes.py','core/hotel_contract.py','core/budget_contract.py',
        'tools/reservation/tool.py','tools/reminders/tool.py','tools/research/tool.py','tools/trip/tool.py',
        'app/request_router.py','app/conversation_context.py','infrastructure/schema.py',
        'evaluation/support.py','evaluation/runner.py','evaluation/scenarios.py','scripts/run_evaluations.py'}
    errors.extend('Missing implementation owner: '+name for name in sorted(business_inputs-names))
    for path in paths:
        if path.suffix != '.py':
            continue
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
        except SyntaxError as error:
            errors.append(str(error))
            continue
        for node in ast.walk(tree):
            modules = ([node.module] if isinstance(node,ast.ImportFrom) and node.module else
                       [name.name for name in node.names] if isinstance(node,ast.Import) else [])
            for module in modules:
                if module in RETIRED_MODULES:
                    errors.append(f'{relative}:{node.lineno}: retired import {module}')
                if module.startswith('scripts.archive'):
                    errors.append(f'{relative}:{node.lineno}: active code imports archive')
                if relative.startswith('tools/') and module.split('.')[0] in {'app','agents'}:
                    errors.append(f'{relative}:{node.lineno}: tool imports application/Agent')
                if relative.startswith('tools/hotel_prices/') and module.startswith('tools.trip'):
                    errors.append(f'{relative}:{node.lineno}: independent hotel tool imports itinerary implementation')
                if relative.startswith('infrastructure/') and (relative.endswith('_repository.py') or relative.endswith('memory_store.py')) and module.split('.')[0] in {'tools','app','agents','services'}:
                    errors.append(f'{relative}:{node.lineno}: repository imports business implementation')
            if relative.startswith(('adapters/','agents/','app/','core/','infrastructure/','services/','tools/')) and isinstance(node,ast.Call):
                if isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name):
                    if node.func.value.id=='ToolResult' and node.func.attr=='from_text':
                        errors.append(f'{relative}:{node.lineno}: production status inferred from prose')
                    if node.func.value.id=='inspect' and node.func.attr=='signature' and relative.startswith(('agents/','app/','tools/')):
                        errors.append(f'{relative}:{node.lineno}: tool execution signature probing')
            if relative=='infrastructure/memory_store.py' and isinstance(node,ast.Constant) and isinstance(node.value,str):
                import re
                if re.match(r'\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b',node.value,re.I):
                    errors.append(f'{relative}:{node.lineno}: domain SQL in store composition')
    return {'passed':not errors,'source_files':len(paths),'errors':errors}


if __name__=='__main__':
    result=check()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
