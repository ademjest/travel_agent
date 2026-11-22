"""Travel queries: preparation, execution, checkpoints and factual answers."""
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from core.hotel_contract import merge_hotel_state, HOTEL_STATE_FIELDS, stay_text
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import asyncio
import time

from core.execution_outcome import TravelDecision
from core.tool_result import TOOL_BY_INTENT
from core.commands import missing_weather_location, is_weather_clarification
from core.chat_transport import ChatEvent
from core.commands import HELP_TEXT, parse_command
from core.event_idempotency import event_operation_key
from core.execution_outcome import ExecutionOutcome, TaskContext, TravelReadRequest
from core.execution_scope import ensure_execution_active
from core.intent_ir import IntentIR, IntentOperation, READ_OPERATION_FIELDS, parse_intent_ir, to_jsonable
from core.intent_ir import COMMAND_SCHEMA_VERSION as RULE_VERSION
from core.settings import Settings
from core.tasks import TaskRecord, TaskUpdate, READ_TASK_TYPES
from core.tool_execution import retry_read
from core.tool_result import AgentToolContext, ToolResult
from infrastructure.amap_client import (
    AmapClient, AmapError, CurrentWeather, RouteSummary, TrafficSegment,
    WeatherForecast, PlaceResolutionError, amap_failure,
)
from core.tool_result import TOOLS_BY_NAME, TRAVEL_TOOL_NAMES, TRAIN_TICKETS_TOOL, validate_arguments
from core.tool_result import DEFAULT_CATALOG
from core.time_input import parse_time_window, parse_reminder_time, BEIJING
from infrastructure.mcp_client import MCPUnavailable


RELIABLE_TRAFFIC_COVERAGE_RATIO = 0.7
MAX_CONGESTED_SEGMENTS_TO_SHOW = 6
UNKNOWN_TRAFFIC_STATUSES = {"", "未知", "未知路况", "无数据"}


def read_coverage_domains(text, *, hotel_query=False):
    """Detect potentially omitted read facts; never select or authorize an operation."""
    domains=set()
    if re.search(r'房价|酒店.{0,10}(?:价格|多少钱)|住宿.{0,10}报价',text):domains.add('hotel')
    if re.search(r'高铁|动车|列车|车票|火车',text):
        domains.add('rail')
    if re.search(r'机票|航班',text):
        domains.add('air')
    if re.search(r'公交|地铁|公共交通',text):
        domains.add('transit')
    elif re.search(r'步行|走路',text):
        domains.add('walking')
    elif re.search(r'路况|拥堵|堵车|车流|通行|适合自驾|自驾风险|出行风险',text):
        domains.add('traffic')
    elif re.search(r'路线|怎么走|距离|驾车|开车|耗时',text):
        domains.add('route')
    weather=bool(re.search(r'天气|气温|下雨|降雨|下雪|大风|带伞|撑伞|淋雨',text))
    future=bool(re.search(r'天气预报|预报|明天|后天|未来|今天下午|今天晚上|今晚',text))
    if future and (weather or '预报' in text):
        domains.add('forecast')
    if re.search(r'现在天气|当前天气|实时天气',text) or weather and not future:
        domains.add('weather')
    if (not re.search(r'文档|文件|资料|行程单|计划书|住宿安排|表格|xlsx|excel|docx|预约|提醒|门票',text)
            and not domains & {'route','traffic','walking','transit'}
            and re.search(r'餐馆|餐厅|博物馆|景点推荐|推荐景点|吃什么|有什么好玩'+('' if hotel_query else r'|酒店|住哪里'),text)
            and (not domains & {'weather','forecast'} or re.search(r'推荐|找酒店|找餐馆',text))):
        if 'hotel' not in domains:domains.add('places')
    return domains


def _format_distance(meters: int) -> str:
    if meters >= 1000:
        return f"{meters / 1000:.1f} 公里"
    return f"{meters} 米"


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{max(1, minutes)} 分钟"


def _format_weather(weather: CurrentWeather) -> str:
    return "\n".join([
        f"【当前天气】{weather.location.address}",
        f"天气：{weather.weather}",
        f"温度：{weather.temperature}℃",
        f"湿度：{weather.humidity}%",
        f"风向风力：{weather.wind_direction}风 {weather.wind_power}级",
        f"发布时间：{weather.report_time}",
    ])


def _format_forecast(forecast: WeatherForecast) -> str:
    lines = [
        f"【天气预报】{forecast.location.address}",
        f"发布时间：{forecast.report_time}",
    ]
    for day in forecast.days:
        lines.append(
            f"{day.date}：{day.day_weather}/{day.night_weather}，"
            f"{day.night_temperature}~{day.day_temperature}℃，"
            f"{day.day_wind}风 {day.day_power}级"
        )
    return "\n".join(lines)


def _format_route(route: RouteSummary) -> str:
    return "\n".join([
        "【高德驾车路线】",
        f"起点：{route.origin.address}",
        f"终点：{route.destination.address}",
        f"原始请求：{route.origin.query} → {route.destination.query}",
        f"距离：{_format_distance(route.distance_meters)}",
        f"预计耗时：{_format_duration(route.duration_seconds)}",
        f"预计收费：{route.tolls_yuan} 元",
        f"红绿灯：{route.traffic_lights} 个",
        "说明：预计耗时会随高德实时路况变化。",
        *([f"定位依据：起点POI {route.origin.poi_id or '行政地点'}；终点POI {route.destination.poi_id or '行政地点'}。入口开放及车辆准入请另行核对。"] if route.origin.poi_id or route.destination.poi_id else []),
    ])


def _format_mileage(meters: int) -> str:
    if meters >= 1000:
        return f"{meters / 1000:.1f} 公里"
    return f"{meters} 米"


def _format_traffic_segment(
        segment: TrafficSegment,
        index: int) -> str:
    road_description = segment.road_name.strip() or "未命名道路"
    instruction = segment.instruction.strip()
    if instruction and instruction not in road_description:
        road_description += f"（{instruction}）"

    details = (
        f"约沿路线 {_format_mileage(segment.route_start_meters)}"
        f"—{_format_mileage(segment.route_end_meters)}，"
        f"长度 {_format_distance(segment.distance_meters)}"
    )
    if segment.start_coordinates and segment.end_coordinates:
        details += (
            f"；坐标 {segment.start_coordinates}"
            f" → {segment.end_coordinates}"
        )
    return f"{index}. {segment.status}：{road_description}（{details}）"


def _merge_adjacent_congested_segments(
        segments: list[TrafficSegment]) -> list[TrafficSegment]:
    merged: list[TrafficSegment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].status == segment.status
            and merged[-1].road_name == segment.road_name
            and merged[-1].route_end_meters == segment.route_start_meters
        ):
            previous = merged[-1]
            merged[-1] = TrafficSegment(
                status=previous.status,
                road_name=previous.road_name,
                instruction=previous.instruction or segment.instruction,
                distance_meters=(
                    previous.distance_meters + segment.distance_meters
                ),
                route_start_meters=previous.route_start_meters,
                route_end_meters=segment.route_end_meters,
                start_coordinates=(
                    previous.start_coordinates or segment.start_coordinates
                ),
                end_coordinates=(
                    segment.end_coordinates or previous.end_coordinates
                ),
            )
        else:
            merged.append(segment)
    return merged


def _format_traffic(route: RouteSummary) -> str:
    known_distances = {
        status: distance
        for status, distance in route.traffic_distances.items()
        if status not in UNKNOWN_TRAFFIC_STATUSES and distance > 0
    }
    total = sum(known_distances.values())
    if route.distance_meters > 0:
        coverage_ratio = min(total / route.distance_meters, 1.0)
        coverage_text = (
            f"路况数据覆盖率：{coverage_ratio:.0%}（"
            f"{_format_distance(min(total, route.distance_meters))} / "
            f"{_format_distance(route.distance_meters)}）"
        )
    else:
        coverage_ratio = None
        coverage_text = "路况数据覆盖率：无法计算（路线总距离未知）"

    congested = sum(
        distance
        for status, distance in known_distances.items()
        if status in {"拥堵", "严重拥堵"}
    )
    slow = known_distances.get("缓行", 0)

    if total == 0:
        traffic_summary = [
            "高德暂未返回分段路况，当前仅能提供交通感知预计耗时。"
        ]
    else:
        congested_ratio = congested / total
        slow_ratio = slow / total
        if congested_ratio >= 0.1:
            level = "高"
        elif congested_ratio > 0 or slow_ratio >= 0.2:
            level = "中"
        else:
            level = "低"

        coverage_is_reliable = (
            coverage_ratio is not None
            and coverage_ratio >= RELIABLE_TRAFFIC_COVERAGE_RATIO
        )
        if level == "低" and not coverage_is_reliable:
            risk_text = (
                "车辆拥堵风险：数据覆盖不足，暂不评级"
                "（路况覆盖不足，已覆盖路段未发现明显拥堵）"
            )
        elif not coverage_is_reliable:
            risk_text = (
                f"车辆拥堵风险：{level}"
                "（路况覆盖不足，实际风险可能更高）"
            )
        else:
            risk_text = f"车辆拥堵风险：{level}"

        traffic_summary = [risk_text]
        for status in ("畅通", "缓行", "拥堵", "严重拥堵"):
            distance = known_distances.get(status, 0)
            if distance:
                traffic_summary.append(
                    f"{status}：{_format_distance(distance)}"
                )

    congested_segments = _merge_adjacent_congested_segments([
        segment
        for segment in route.traffic_segments
        if segment.status in {"拥堵", "严重拥堵"}
        and segment.distance_meters > 0
    ])
    if congested_segments:
        traffic_summary.append("具体拥堵路段：")
        traffic_summary.extend(
            _format_traffic_segment(segment, index)
            for index, segment in enumerate(
                congested_segments[:MAX_CONGESTED_SEGMENTS_TO_SHOW],
                start=1,
            )
        )
        omitted_count = (
            len(congested_segments) - MAX_CONGESTED_SEGMENTS_TO_SHOW
        )
        if omitted_count > 0:
            traffic_summary.append(
                f"另有 {omitted_count} 个拥堵路段未展开，请以高德导航为准。"
            )

    return "\n".join([
        "【高德实时路线与路况】",
        f"起点：{route.origin.address}",
        f"终点：{route.destination.address}",
        f"原始请求：{route.origin.query} → {route.destination.query}",
        f"总距离：{_format_distance(route.distance_meters)}",
        f"当前预计耗时：{_format_duration(route.duration_seconds)}",
        coverage_text,
        *traffic_summary,
        "说明：路况会变化，请以出发前再次查询结果为准。",
        (
            "安全提示：高德 TMC 反映交通速度和拥堵，不代表封路、施工、"
            "落石、积雪等道路危险；分段里程是按返回距离累计的近似位置。"
            "青甘自驾还需核对交警、交通运输和景区公告。"
        ),
    ])


ANSWER_VERSION = 'readonly-answer-v2'
QUANTITY = re.compile(r'(-?\d+(?:\.\d+)?)\s*(℃|°C|°F|摄氏度|华氏度|公里|千米|分钟|小时|米|秒|元|%|％|km|min)')
ASSERTION = re.compile(r'\d|天气|气温|温度|湿度|风力|晴|雨|雪|拥堵|畅通|距离|耗时|分钟|小时|公里|元|费用|收费|票价|免费|起点|终点|班次|发车|封路|开放|关闭|保证|安全|肯定|一定|绝对|取消|预约|保存|提醒|已完成|已查询|已经')


@dataclass(frozen=True)
class ReadAnswer:
    reply: str
    status: str
    reasons: tuple[str, ...]
    source_hashes: tuple[str, ...]

    @property
    def facts_hash(self):
        return hashlib.sha256(''.join(self.source_hashes).encode()).hexdigest()


def quantities(text):
    units={'°C':'℃','摄氏度':'℃','华氏度':'°F','千米':'公里','km':'公里','min':'分钟','％':'%'}
    return {(Decimal(number),units.get(unit,unit)) for number,unit in QUANTITY.findall(text)}


def _number(value):
    if isinstance(value,bool):
        return None
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _known(value):
    return isinstance(value,str) and value.strip() not in {'','未知','--'}


def read_fact_state(receipt):
    """Validate domain fields; only old text-only receipts use compatibility matching."""
    if receipt.action not in TRAVEL_TOOL_NAMES:
        return ''
    if receipt.status!='completed':
        return 'unavailable'
    if not receipt.data.strip():
        return 'missing'
    payload=receipt.payload
    if payload is not None:
        if not isinstance(payload,dict):
            return 'invalid'
        if receipt.action=='get_current_weather':
            if not {'weather','temperature','report_time'}<=payload.keys():
                return 'invalid'
            if not _known(payload['weather']) or not _known(payload['report_time']) or payload['temperature'] in (None,'','未知','--'):
                return 'unknown'
            return 'known' if _number(payload['temperature']) is not None else 'invalid'
        if receipt.action=='get_weather_forecast':
            days=payload.get('days')
            if not isinstance(days,(tuple,list)):
                return 'invalid'
            if not days:
                return 'unknown'
            for day in days:
                if not isinstance(day,dict):
                    return 'invalid'
                try:
                    date.fromisoformat(day.get('date',''))
                except (ValueError,TypeError):
                    return 'invalid'
                if any(_number(day.get(field)) is None for field in ('day_temperature','night_temperature')):
                    return 'unknown'
            return 'known'
        if receipt.action=='search_travel_places':
            places=payload.get('places')
            return ('known' if places else 'empty') if isinstance(places,list) else 'invalid'
        if receipt.action==TRAIN_TICKETS_TOOL:
            if not isinstance(payload.get('trains'),list) or not payload.get('request') or not payload.get('retrieved_at'):
                return 'invalid'
            if payload.get('status') in {'incomplete','not_on_sale'}:
                return 'known'
            if any(not isinstance(row,dict) or not all(row.get(k) for k in
                       ('train_code','origin_station','destination_station','departure_at','arrival_at'))
                   or _number(row.get('duration_minutes')) is None or not isinstance(row.get('seats'),list)
                   for row in payload['trains']):
                return 'invalid'
            return 'known' if payload['trains'] else 'empty' if payload.get('status')=='empty' else 'invalid'
        if receipt.action=='query_hotel_prices':
            return ('known' if payload['offers'] else 'empty') if isinstance(payload.get('offers'),list) else 'invalid'
        distances=[_number(payload.get(field)) for field in ('distance_meters','duration_seconds')]
        return 'known' if (payload.get('origin') and payload.get('destination')
                           and all(v is not None and v>=0 for v in distances)) else 'invalid'
    if receipt.action in {'get_current_weather','get_weather_forecast'}:
        return 'known' if re.search(r'晴|雨|雪|阴|多云|-?\d+(?:\.\d+)?\s*℃',receipt.data) else 'missing'
    if receipt.action=='search_travel_places':
        if re.search(r'未找到|无匹配|没有找到',receipt.data):
            return 'empty'
        return 'known' if 'POI' in receipt.data else 'missing'
    return 'known' if re.search(r'\d+(?:\.\d+)?\s*(?:公里|千米|米|分钟|小时)|起点[：:]|终点[：:]|路况.{0,8}(?:无|未知)|覆盖',receipt.data) else 'missing'


def has_facts(receipt):
    return read_fact_state(receipt) in {'known','empty'}


def project_read_answer(candidate, receipts, *, max_chars=6500):
    """The guarantee is consistency with owned tool receipts, not external truth.

    Free advice can survive only as non-factual suggestions. This deliberately
    does not try to prove arbitrary paraphrases with a bag of matching numbers.
    """
    reads=[r for r in receipts if r.action in TRAVEL_TOOL_NAMES]
    completed=[r for r in reads if r.status=='completed' and has_facts(r)]
    facts=list(dict.fromkeys(r.data.strip() for r in completed))
    hashes=tuple(hashlib.sha256(text.encode()).hexdigest() for text in facts)
    if not facts:
        return ReadAnswer('本次没有取得可用的查询事实，请补充信息或稍后重试。','unverified',('no_facts',),hashes)
    evidence='\n\n'.join(facts)
    reasons=[]
    if quantities(candidate)-quantities(evidence):
        reasons.append('unsupported_quantity')
    if re.search(r'(?:道路|路线|出行).{0,8}(?:绝对|一定|肯定)安全|(?:保证|确定).{0,8}(?:安全|不堵|不封路|准点)',candidate):
        reasons.append('unsupported_guarantee')
    advice=[]
    for line in re.split(r'[。！？\n]+',candidate):
        line=line.strip(' -•*\t')
        if (re.match(r'^(?:建议|不妨|可以考虑|可考虑|请随身带伞|出行建议[：:])',line)
                and len(line)<=200 and not ASSERTION.search(line) and line not in evidence):
            advice.append(line+'。')
    limitations=[]
    actions={r.action for r in completed}
    if 'get_current_weather' in actions:
        limitations.append('当前天气为行政区级参考，不代表具体地点的微气候。')
    if 'get_weather_forecast' in actions:
        limitations.append('天气预报为日级、行政区级参考，不能据此确定某个小区在具体时刻是否下雨；未返回的日期或时段尚未核实。')
    if actions & {'get_driving_route','get_route_traffic','get_walking_route','get_transit_route'}:
        limitations.append('路线与耗时反映查询时取得的方案，不能保证未来班次、道路开放或通行安全。')
    text='\n\n'.join([*facts,*dict.fromkeys(advice),*limitations])
    failed=[r for r in reads if r.status!='completed']
    if failed:
        text+='\n\n尚未完成：'+'；'.join(r.data.removeprefix('工具错误：') for r in failed)
        reasons.append('incomplete_tools')
    missing=[r for r in reads if r.status=='completed' and not has_facts(r)]
    if missing:
        labels={'get_current_weather':'当前天气','get_weather_forecast':'天气预报','search_travel_places':'地点查询'}
        text+='\n\n尚未完成：'+'；'.join(labels.get(r.action,'路线查询')+'未取得可用事实' for r in missing)+'。'
        reasons.append('missing_facts')
    if len(text)>max_chars:
        # Whole conditions stay in owned tool artifacts; never truncate a value
        # away from its role, timestamp or applicability restriction.
        links=list(dict.fromkeys(r.artifact_id for r in reads if r.artifact_id))
        text='本次取得多项查询资料，完整条件超出单条答复范围。请按地点或查询项目分别查看。'
        if links:
            text+='\n完整工具结果：'+'、'.join(links)+'。'
        reasons.append('answer_budget')
    return ReadAnswer(text,'degraded' if reasons else 'grounded',tuple(reasons),hashes)


VERSION = 'readonly-routing-v3-hotel-state'
COMMANDS = frozenset({'weather','forecast','route','traffic'})


class ReadCoverageError(ValueError):
    """Coverage hints may reject an incomplete IR, never grant new tools."""


def rail_parameters(arguments, reference):
    """Normalize calendar/time on the server; missing dates never become today's tickets."""
    values=dict(arguments)
    missing=tuple(key for key in ('origin','destination','travel_date') if not values.get(key))
    if missing:
        return None,missing
    parsed=parse_reminder_time(values['travel_date'],reference)
    if parsed.error or not parsed.day:
        return None,('travel_date',)
    values['travel_date']=parsed.day
    for key in ('origin','destination'):
        values[key]=values[key].strip()
        values.setdefault(key+'_scope','station' if values[key].endswith('站') else 'city')
    values.setdefault('train_preference','high_speed')
    values.setdefault('limit',6)
    window=values.get('departure_window','')
    try:
        values['departure_window']=parse_time_window(window) if window else (0,24)
    except ValueError:
        return None,('departure_window',)
    return values,()


class TravelQueryTool:
    def __init__(self, settings: Settings | None = None, *, store=None, backend=None, tool_executor=None):
        self.store, self.backend, self.tool_executor = store, backend, tool_executor
        self.enabled = settings is not None or backend is not None or tool_executor is not None
        self.llm_configured = settings.llm_configured if settings else False
        self.amap = AmapClient(settings.amap_api_key) if settings and settings.amap_api_key else None
        self.rail = None
        self.hotel_prices = None

    @property
    def manages_read_retries(self):
        return self.backend.manages_read_retries if self.backend is not None else isinstance(self.amap, AmapClient)

    def execute_result(self, name, arguments, context=None):
        if name=='query_hotel_prices':
            return self.hotel_prices.execute_result(name,arguments,context) if self.hotel_prices else ToolResult('failed',name,error_code='not_available',data='酒店查价服务暂不可用。')
        if name==TRAIN_TICKETS_TOOL:
            return self.query_train_tickets(**arguments,context=context)
        if self.backend is not None:
            return self.backend.execute_result(name, arguments)
        if not isinstance(arguments, dict) or any(not isinstance(value, str) for value in arguments.values()):
            return ToolResult('failed', name, data='工具错误：参数必须为文本字段。', error_code='invalid_argument')
        if not self.amap:
            return ToolResult('failed', name, data='工具错误：尚未配置 AMAP_API_KEY。', error_code='not_available')
        try:
            if name == 'search_travel_places':
                city, keywords = arguments.get('city', '').strip(), arguments.get('keywords', '').strip()
                if not city or not keywords:
                    return ToolResult('failed', name, data='工具错误：请提供城市和要找的场所。', error_code='invalid_argument')
                places = self.amap.search_places(city, keywords)
                lines = [f'【高德地点查询】{city} · {keywords}']
                if not places:
                    lines.append('未找到匹配地点，请缩小区域或更换名称。')
                for index, place in enumerate(places, 1):
                    lines.append(f"{index}. {place['name']}｜{place.get('address', '地址未提供')}｜{place.get('type', '')}｜POI ID: {place.get('id', '')}")
                lines.extend((f'查询时间：{datetime.now(timezone.utc).isoformat()}',
                              '地点资料不代表营业中、实时房价、余票或库存；出发前请核对官方信息。'))
                return ToolResult('completed', name, data='\n'.join(lines), payload={'places': places})
            if name in {'get_walking_route', 'get_transit_route'}:
                origin, destination = arguments.get('origin', '').strip(), arguments.get('destination', '').strip()
                if not origin or not destination:
                    return ToolResult('failed', name, data='工具错误：缺少 origin 或 destination。', error_code='invalid_argument')
                mode = 'walking' if name == 'get_walking_route' else 'transit'
                route = self.amap.non_driving_route(origin, destination, mode=mode, city=arguments.get('city', ''))
                text = '\n'.join((f"【高德{'步行' if mode == 'walking' else '公共交通'}方案】",
                    f"{route['origin']} → {route['destination']}",
                    f"约 {_format_distance(route['distance_meters'])}，预计 {_format_duration(route['duration_seconds'])}",
                    *route['instructions'], '方案与班次可能变化，请结合出发时间再次核对。'))
                return ToolResult('completed', name, data=text, payload=route)
            if name in {'get_current_weather', 'get_weather_forecast'}:
                location = arguments.get('location', '').strip()
                if not location:
                    return ToolResult('failed', name, data='工具错误：缺少 location。', error_code='invalid_argument')
                if name == 'get_current_weather':
                    weather = self.amap.current_weather(location)
                    return ToolResult('completed', name, data=_format_weather(weather), payload=asdict(weather))
                forecast = self.amap.weather_forecast(location)
                return ToolResult('completed', name, data=_format_forecast(forecast), payload=asdict(forecast))
            if name in {'get_driving_route', 'get_route_traffic'}:
                origin, destination = arguments.get('origin', '').strip(), arguments.get('destination', '').strip()
                if not origin or not destination:
                    return ToolResult('failed', name, data='工具错误：缺少 origin 或 destination。', error_code='invalid_argument')
                route = self.amap.driving_route(origin, destination)
                return ToolResult('completed', name, data=_format_route(route) if name == 'get_driving_route'
                                  else _format_traffic(route), payload={'origin': asdict(route.origin),
                    'destination': asdict(route.destination), 'distance_meters': route.distance_meters,
                    'duration_seconds': route.duration_seconds, 'tolls_yuan': route.tolls_yuan})
        except PlaceResolutionError as exc:
            return ToolResult('needs_input', name, data=str(exc), error_code=exc.code,
                              missing_fields=(exc.field,) if exc.field else (),payload={'place_resolution':exc.resolution})
        except AmapError as exc:
            return amap_failure(name, exc)
        return ToolResult('failed', name, data=f'工具错误：未知工具 {name}。', error_code='not_available')

    def query_train_tickets(self, origin='', destination='', travel_date='', *, train_preference=None,
                            departure_window='', origin_scope=None, destination_scope=None, limit=6,
                            seat_class=None,seat_requirement=None,price_preference=None,context=None):
        """One business entry for model queries and later Trip dependencies, using owned receipts."""
        name=TRAIN_TICKETS_TOOL
        raw={'origin':origin,'destination':destination,'travel_date':travel_date,'limit':limit}
        raw.update({key:value for key,value in {'train_preference':train_preference,'departure_window':departure_window,
                    'origin_scope':origin_scope,'destination_scope':destination_scope,'seat_class':seat_class,
                    'seat_requirement':seat_requirement,'price_preference':price_preference}.items() if value is not None and value!=''})
        error=validate_arguments(raw,TOOLS_BY_NAME[name]['function']['parameters'])
        if error and all(raw.get(field) for field in ('origin','destination','travel_date')):
            return ToolResult('failed',name,data='铁路查询参数无效。',error_code='invalid_argument')
        reference=(context.execution_data or {}).get('reference_time') if context else None
        moment=datetime.fromisoformat(reference) if reference else (context.event.occurred_at if context and context.event else None) or datetime.now(timezone.utc)
        parameters,missing=rail_parameters(raw,moment)
        if missing:
            labels={'origin':'出发城市或车站','destination':'到达城市或车站','travel_date':'明确的乘车日期','departure_window':'明确的出发时段（如上午、18:00至22:00）'}
            return ToolResult('needs_input',name,data='请补充'+'、'.join(labels[k] for k in missing)+'。',missing_fields=missing)
        if self.rail is None:
            return ToolResult('failed',name,data='铁路数据源尚未启用，未查询车票。',error_code='not_available')
        retrieval={}
        def prepare_call(tool,arguments,attempt):
            bound=self.store is not None and context is not None and self.store.delivery.get_event_status(context.event_id) is not None
            if not bound:
                return None,None
            identity=hashlib.sha256(self.rail.mcp.config.endpoint.encode()).hexdigest()+self.rail.mcp.status().get('schema_sha256','')
            operation='rail-rpc-v1:'+identity+tool+(f':retry-{attempt}' if attempt else '')
            key=event_operation_key(context.event_id,operation,arguments)
            saved=self.store.delivery.get_event_tool_result(key)
            if saved:
                return key,json.loads(saved)
            with self.store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT attempts FROM event_read_attempts WHERE operation_key=?',(key,)).fetchone()
                total=db.execute('SELECT coalesce(sum(attempts),0) FROM event_read_attempts WHERE event_id=?',(context.event_id,)).fetchone()[0]
                if row:
                    raise MCPUnavailable('unconfirmed_submission')
                if total>=12:
                    raise MCPUnavailable('attempt_limit')
                db.execute('INSERT INTO event_read_attempts VALUES (?,?,1)',(key,context.event_id))
            return key,None
        def release_unsubmitted(key):
            with self.store._connect() as db:
                db.execute('DELETE FROM event_read_attempts WHERE operation_key=?',(key,))
        async def checkpoint(tool,arguments,*,attempt=0):
            ensure_execution_active()
            from mcp.types import CallToolResult
            await self.rail.mcp.ensure_ready()
            key,saved=await asyncio.to_thread(prepare_call,tool,arguments,attempt)
            if saved:
                retrieval[tool]=saved['retrieved_at']
                return CallToolResult.model_validate(saved['result'])
            try:
                result=await self.rail.mcp.call(tool,arguments)
            except MCPUnavailable as error:
                if key and error.submitted is False:
                    await asyncio.to_thread(release_unsubmitted,key)
                raise
            ensure_execution_active()
            retrieval[tool]=datetime.now(timezone.utc).isoformat()
            if key:
                await asyncio.to_thread(self.store.delivery.save_event_tool_result,key,context.event_id,'rail:'+tool,arguments,
                    json.dumps({'result':result.model_dump(mode='json'),'retrieved_at':retrieval[tool]},ensure_ascii=False))
            if result.isError and self.store and getattr(self.store,'diagnostics',None):
                await asyncio.to_thread(self.store.diagnostics.record,'tool',{'event_key':context.event_id if context else '',
                    'status':'remote_error','name':tool,'error_code':'provider_error','attempts':attempt+1})
            return result
        metrics={'logical_count':1,'mcp_submissions':0,'http_requests':None}
        def submitted():metrics['mcp_submissions']+=1
        from infrastructure.mcp_client import MCP_SUBMISSION_OBSERVER
        observer=MCP_SUBMISSION_OBSERVER.set(submitted)
        started=time.monotonic()
        if self.store:
            self.store.diagnostics.record('tool',{'event_key':context.event_id if context else '',
                'name':'rail_query','status':'started','provider_alias':'rail-remote',**metrics})
        try:
            data=self.rail.query_sync(**{k:v for k,v in parameters.items() if k not in {'seat_class','seat_requirement','price_preference'}},call=checkpoint)
            if data.get('all_trains') is not None:
                from core.rail_contract import seat_matches
                matches=[row for row in data['all_trains'] if seat_matches(row,parameters)]
                data['trains']=matches[:parameters['limit']]
                if not matches and data['status']=='ok':
                    data['status']='incomplete' if data.get('candidate_truncated') else 'empty'
        except MCPUnavailable as error:
            data={'status':'error','error_code':error.code,'trains':[]}
        finally:
            MCP_SUBMISSION_OBSERVER.reset(observer)
        if self.store:
            self.store.diagnostics.record('tool',{'event_key':context.event_id if context else '',
                'name':'rail_query','status':'completed' if data['status'] not in {'error','invalid_request'} else 'failed',
                'candidate_count':len(data.get('all_trains',data.get('trains',[]))),
                'elapsed_seconds':round(time.monotonic()-started,3),**metrics})
        data['query_metrics']=metrics
        data.update(request={**parameters,'departure_window':list(parameters['departure_window'])},retrieved_at=retrieval.get('get-tickets') or data.get('retrieved_at') or datetime.now(timezone.utc).isoformat(),
                    provider='12306-mcp',defaults={'train_preference':{'value':parameters['train_preference'],
                    'source':'default' if train_preference is None else 'request'}})
        if data['status'] in {'error','invalid_request'}:
            code=data.get('error_code','invalid_argument')
            if code=='location_unresolved':
                field=data.get('error_field') or 'origin'
                return ToolResult('needs_input',name,data='请确认'+('出发地' if field=='origin' else '到达地')+'的城市或车站全称及查询范围。',
                                  error_code=code,missing_fields=(field,),payload=data)
            detail={'unconfirmed_submission':'上次铁路请求已提交但未取得保存的确认结果，本次没有重复提交；请重新发起查询。',
                    'attempt_limit':'本轮查询已达到提交额度，剩余项目尚未执行；请缩小范围重新查询。',
                    'expired':'铁路专属连接已过期，请更新服务配置后重启。',
                    'schema_mismatch':'铁路服务工具合同已变化，本次未查询，请重新验证连接。',
                    'provider_error':'铁路远端工具处理失败，有限重试后仍未取得车票资料；请稍后重新查询。',
                    'invalid_response':'铁路服务返回的数据格式异常，无法可靠解析车票资料。',
                    'connection_lost':'铁路连接已断开，本次未取得确认结果；未因断线重发，后续新查询会尝试恢复连接。',
                    'protocol_error':'铁路会话发生协议错误，本次未取得确认结果；未因协议错误重发，后续新查询会尝试恢复连接。',
                    'connection_limit':'铁路连接尝试已达到限制，请稍后重新查询；限额测试需检查剩余连接额度。',
                    'unauthorized':'铁路服务认证失败，请检查服务配置。',
                    'forbidden':'铁路服务拒绝访问，请检查部署与账户权限。',
                    'call_limit':'本次铁路工具调用额度已用完，未继续提交查询。',
                    'timeout':'铁路查询超时，未取得确认结果；未因超时重发，请稍后重新查询。'}.get(code,
                    '铁路查询未取得有效结果，请检查连接或稍后重新查询。')
            return ToolResult('failed',name,data=detail,error_code=code,payload=data)
        labels={'high_speed':'高铁优先，数量不足时列出动车备选','high_speed_only':'仅高铁（G字头）','d_train':'仅动车（D字头）','conventional':'普通列车','all':'全部车次类别'}
        window=parameters['departure_window']
        window_text='–'.join(f'{int(value*60)//60:02d}:{int(value*60)%60:02d}' for value in window)
        lines=[f"{origin} → {destination}｜{parameters['travel_date']}（北京时间）",
               f"范围：{'具体站' if parameters['origin_scope']=='station' else '城市'} → {'具体站' if parameters['destination_scope']=='station' else '城市'}；出发 {window_text}；{labels[parameters['train_preference']]}。"]
        if train_preference is None:
            lines.append('未指定车次偏好，本次默认高铁优先。')
        full=data.get('all_trains',data['trains'])
        if full:
            count=len({(train['train_code'],train['departure_at'][:10]) for train in full})
            times=sorted(train['departure_at'][11:16] for train in full)
            lines.append(f"本次取得{count}组车次、{len(full)}个乘车区间，出发时间{times[0]}–{times[-1]}；以下为前{len(data['trains'])}条区间摘要。")
        seats={'available':'有票','sold_out':'无票','waitlist':'候补','unknown':'余票未知'}
        for train in data['trains']:
            category='动车备选 · ' if parameters['train_preference']=='high_speed' and train['train_code'].startswith('D') else ''
            lines.append(f"- {category}{train['train_code']}｜{train['origin_station']} {train['departure_at'][:16].replace('T',' ')} → {train['destination_station']} {train['arrival_at'][:16].replace('T',' ')}｜{_format_duration(train['duration_minutes']*60)}")
            values=[f"{seat['name'] or '席别未提供'}：{('¥'+str(seat['price_cny'])) if seat['price_cny'] is not None else '票价未知'}，{seats[seat['availability']]}" for seat in train['seats']]
            lines.append('  '+('；'.join(values) if values else '票价与余票未提供。'))
        lines.append('')
        if data['status']=='empty':
            lines.append('本次查询范围内未返回匹配车次。')
        elif data['status']=='not_on_sale':
            lines.append('供应商明确返回尚未起售，当前没有可查询的车票结果。')
        if data.get('candidate_truncated') or data['status']=='incomplete':
            lines.append('供应商候选达到本次上限，结果不完整；不能据此认定无车。')
        if data.get('city_coverage')=='provider_city_code_unverified':
            lines.append('城市结果按供应商城市代码查询，覆盖完整性未独立验证；以列出的实际车站为准。')
        queried_at=datetime.fromisoformat(data['retrieved_at']).astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        lines.extend((f"数据时间：{queried_at}（北京时间）",'车次列表受筛选与数量上限影响；余票和价格随时变化，请在 12306 购票前复核。'))
        return ToolResult('completed',name,data='\n'.join(lines),payload=data)

    def execute_tool(self, name: str, arguments: dict[str, str]) -> str:
        """Text adapter for command callers; execution status originates in execute_result."""
        result = self.execute_result(name, arguments)
        if result.status != 'completed' and not result.data.startswith('工具错误：'):
            return '工具错误：' + result.data
        return result.data

    def handle(self, content: str) -> str:
        command = parse_command(content)

        if command.error:
            return command.error
        if command.name == "ping":
            return "pong"
        if command.name == "help":
            return HELP_TEXT
        if command.name == "status":
            amap_status = "已配置" if self.amap else "未配置 AMAP_API_KEY"
            llm_status = "已配置" if self.llm_configured else "未完整配置"
            rail_state=self.rail.mcp.status()['state'] if self.rail else 'disabled'
            rail_status={'ready':'就绪','connecting':'连接中','disabled':'未启用','expired':'已过期'}.get(rail_state,'暂不可用')
            return (
                f"Bot 已在线。高德数据源：{amap_status}；"
                f"LLM Agent：{llm_status}；铁路数据源：{rail_status}。"
            )
        if command.name == "unknown":
            return "暂未识别该指令。发送“帮助”查看当前可用指令。"
        if not self.amap:
            return "尚未配置 AMAP_API_KEY，请先在 .env 中添加高德 Web 服务 Key。"

        if command.name in {'weather','forecast','route','traffic'}:
            return self.command(content,command)

        return "暂未识别该指令。发送“帮助”查看当前可用指令。"

    def execute_scoped_result(self,name,arguments,context):
        store = self.store
        max_attempts = 1 if name in {TRAIN_TICKETS_TOOL,'query_hotel_prices'} or self.manages_read_retries else 3
        def invoke():
            from infrastructure.place_resolution import PLACE_CONTEXT
            token=PLACE_CONTEXT.set((context.execution_data or {}).get('place_resolution') if context else None)
            try:
                if name==TRAIN_TICKETS_TOOL:return self.query_train_tickets(**arguments,context=context)
                if name=='query_hotel_prices':return self.execute_result(name,arguments,context)
                return self.execute_result(name,arguments)
            finally:
                PLACE_CONTEXT.reset(token)
        if store is None or context is None or store.delivery.get_event_status(context.event_id) is None:
            return retry_read(invoke, max_attempts=max_attempts)
        from infrastructure.place_resolution import PLACE_RESOLUTION_VERSION
        version=':rail-v1' if name==TRAIN_TICKETS_TOOL else ':'+PLACE_RESOLUTION_VERSION if name in {'get_driving_route','get_route_traffic','get_walking_route','get_transit_route'} else ''
        key = event_operation_key(context.event_id,'read:'+name+version,arguments)
        cached = store.delivery.get_event_tool_result(key)
        if cached is not None:
            if name==TRAIN_TICKETS_TOOL:
                store.diagnostics.record('tool',{'event_key':context.event_id,'name':'rail_query','status':'checkpoint_reused',
                    'logical_count':0,'mcp_submissions':0,'http_requests':None})
            return ToolResult(**json.loads(cached))
        def operation():
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                attempts = db.execute('SELECT attempts FROM event_read_attempts WHERE operation_key=?',(key,)).fetchone()
                total = db.execute('SELECT coalesce(sum(attempts),0) FROM event_read_attempts WHERE event_id=?',(context.event_id,)).fetchone()[0]
                if (attempts and attempts['attempts']>=3) or total>=12:
                    return ToolResult('failed',name,data='本次查询重试额度已用完，请稍后重新发起。',error_code='attempt_limit')
                db.execute('''INSERT INTO event_read_attempts VALUES (?,?,1)
                    ON CONFLICT(operation_key) DO UPDATE SET attempts=attempts+1''',(key,context.event_id))
            return invoke()
        result=retry_read(operation, max_attempts=max_attempts)
        if result.status=='completed':
            store.delivery.save_event_tool_result(key,context.event_id,name,arguments,result.to_json())
        return result


    def record(self,event,status,**details):
        if getattr(self.store,'diagnostics',None):
            self.store.diagnostics.record('routing',{'event_key':event.event_key,'status':status,
                'route_owner':VERSION,'validator_version':ANSWER_VERSION,**details})

    @staticmethod
    def command_ir(text,command=None):
        command=command or parse_command(text)
        if command.name not in COMMANDS or command.error:
            return None
        fields=TOOLS_BY_NAME[TOOL_BY_INTENT[command.name][0]]['function']['parameters']['required']
        return IntentIR(1,'read',(IntentOperation(command.name,'query',dict(zip(fields,command.args))),),source_spans=(text,))

    def _key(self,event,source):
        return ('rule-plan:' if source=='rule' else 'read-plan:')+hashlib.sha256(event.event_key.encode()).hexdigest()

    @staticmethod
    def from_context(context):
        if context.task is None:
            return None
        if context.task.task_type in {'hotel','read_batch'}:return None
        domains=context.slots.get('intents',[context.task.task_type])
        if context.task.task_type in {'weather','forecast'} and any(word in context.input_text for word in ('明天','后天')):
            domains=['forecast']
        queries=tuple(IntentOperation(domain,'search' if domain=='places' else 'query',
            {field:context.slots[field] for field in READ_OPERATION_FIELDS[domain] if context.slots.get(field)})
            for domain in domains if domain in READ_OPERATION_FIELDS)
        return TravelReadRequest(queries,context,source='continuation',explain=True) if queries else None

    def resume(self,event,claim):
        if self.store is None:
            return None
        frozen=self.store.delivery.get_event_tool_result(self._key(event,'semantic'))
        return self.prepare(event,claim) if frozen else None

    def prepare(self,event,claim,ir=None,context=None,*,source='semantic',mode='execute'):
        if mode=='shadow' or not self.enabled:
            return None
        if self.store is not None and self.store.delivery.get_event_status(event.event_key) is not None:
            self.store.delivery.freeze_tool_catalog(event.event_key,DEFAULT_CATALOG)
        context=context or TaskContext(None,event.content,(event.occurred_at or datetime.now(timezone.utc)).isoformat(),{},event.content)
        key=self._key(event,source)
        cached=self.store.delivery.get_event_tool_result(key) if self.store is not None else None
        if cached and source!='rule':
            snapshot=json.loads(cached)
            if snapshot.get('version')!=VERSION:
                return '只读处理版本已变化，已保存结果仍保留；请重新发起查询。'
            state=snapshot['context']
            task=TaskRecord(**state['task']) if state.get('task') else None
            context=TaskContext(task,state['message'],state['reference_time'],state['slots'],state['input_text'])
            ir=parse_intent_ir(snapshot['ir'],context.message)
            source=snapshot['source']
        if ir is None:
            return None
        if ir.action in {'read','clarify'} and ir.operations and all(q.domain in READ_OPERATION_FIELDS for q in ir.operations):
            if any(q.operation!=('search' if q.domain=='places' else 'query') for q in ir.operations):
                return None
        request=TravelReadRequest.from_ir(ir,context)
        if request is None:
            if context.slots.get('queries'):
                return '该引用包含多个铁路方向，请明确要修改的原方向及起终点；尚未提交车票查询。'
            return '本次只读查询的字段或范围未通过校验，尚未调用工具。'
        if source=='semantic' and not cached:
            residual=context.input_text or event.content
            from core.hotel_contract import leaves
            for value in sorted({v for q in request.queries for _,v in leaves(q.values) if isinstance(v,str)},key=len,reverse=True):
                residual=residual.replace(value,' ')
            hints=read_coverage_domains(residual,hotel_query=any(q.domain=='hotel' for q in request.queries))
            actual={q.domain for q in request.queries}
            if actual & {'weather','forecast'}:
                hints-= {'weather','forecast'}
            if 'traffic' in actual:
                hints.discard('route')
            if 'rail' in actual and not re.search(r'驾车|开车|自驾',event.content):
                hints.discard('route')
            if hints-actual:
                self.record(event,'read_coverage_mismatch',reason='missing_domains')
                raise ReadCoverageError('完整请求中的只读事项未全部进入IntentIR。')
        explain=source!='rule' and not any(q.domain=='hotel' for q in request.queries) and not all(q.domain=='rail' for q in request.queries) and (len(request.queries)>1 or bool(re.search(r'带伞|撑伞|淋雨|风险|安全|比较|分析',event.content)))
        request=replace(request,source=source,explain=explain)
        self.record(event,'read_selected',decision_source=source,candidate_count=len(request.queries),
                     state_version=context.task.version if context.task else None)
        if mode=='preview':
            return '已识别实时旅行查询；当前仅解析预览，尚未调用数据源。'
        if not cached and claim is not None and self.store is not None:
            snapshot={'version':RULE_VERSION if source=='rule' else VERSION,'ir':to_jsonable(ir),'source':source}
            if source!='rule':
                snapshot['context']={'task':asdict(context.task) if context.task else None,'message':context.message,
                    'reference_time':context.reference_time,'slots':context.slots,'input_text':context.input_text}
            self.store.delivery.save_event_tool_result(key,event.event_key,'read_plan',{},json.dumps(snapshot,ensure_ascii=False))
        missing=[field for query in request.queries for field in TOOLS_BY_NAME[TOOL_BY_INTENT[query.domain][0]]['function']['parameters'].get('required',[])
                 if not query.values.get(field)]
        if request.explain:
            return request
        return self.execute(event,claim,request,key=key)

    def _invoke(self, tool, arguments, event, reference_time='',place_resolution=None,slots=None):
        arguments={k:v for k,v in arguments.items() if k in TOOLS_BY_NAME[tool]['function']['parameters']['properties']}
        context = AgentToolContext(event.platform, event.scope_id, event.sender_id, event.event_key,event=event,
                                  allowed_actions=frozenset({tool}),execution_data={'reference_time':reference_time,
                                      'place_resolution':place_resolution,**{k:v for k,v in (slots or {}).items()
                                          if k in {'hotel_request','hotel_targets','hotel_candidates','hotel_date_proposal','hotel_expired','hotel_evidence','hotel_remaining_targets','hotel_batch_continuation','hotel_previous_status','field_provenance','source_context'}|set(HOTEL_STATE_FIELDS.values())|{'hotel_selection_ordinal','hotel_invalid_selection'}}},
                                  catalog_version=self.store.delivery.freeze_tool_catalog(event.event_key,DEFAULT_CATALOG)
                                  if self.store is not None and self.store.delivery.get_event_status(event.event_key) is not None else DEFAULT_CATALOG)
        executor = self.tool_executor or self.execute_scoped_result
        return executor(tool, arguments, context)

    def execute(self,event,claim,request,*,key=None):
        if not self.enabled:
            return '查询服务暂不可用，本次未执行查询。'
        key=key or self._key(event,request.source)
        context=request.context
        operations=[]; receipts=[]; missing=[]
        for index,query in enumerate(request.queries):
            ensure_execution_active()
            tool=TOOL_BY_INTENT[query.domain][0]
            fields=TOOLS_BY_NAME[tool]['function']['parameters'].get('required',[])
            needed=list(dict.fromkeys([field for field in fields if not query.values.get(field)]+
                        list(request.missing_fields[index] if request.missing_fields else ())))
            operation_key=key+':'+str(index)
            cached=self.store.delivery.get_event_tool_result(operation_key) if self.store is not None else None
            if needed:
                labels={'location':'地点（城市）','origin':'起点','destination':'终点','city':'城市','keywords':'场所类型','travel_date':'明确的乘车日期','departure_window':'明确的出发时段'}
                result=ToolResult('needs_input',tool,data='请补充'+'、'.join(labels.get(f,f) for f in needed)+'。',missing_fields=tuple(needed))
            elif cached:
                result=ToolResult(**json.loads(cached))
            else:
                result=self._invoke(tool,query.values,event,context.reference_time,context.slots.get('place_resolution'),context.slots)
            if not isinstance(result,ToolResult) or result.action!=tool:
                raise TypeError('Read tools must return their own ToolResult.')
            ensure_execution_active()
            if not cached and claim is not None and self.store is not None:
                self.store.delivery.save_event_tool_result(operation_key,event.event_key,tool,query.values,result.to_json())
            receipts.append(result); missing.extend(result.missing_fields)
            fact_state=read_fact_state(result)
            status='failed' if result.status=='completed' and fact_state not in {'known','empty'} else result.status
            operations.append({'operation_key':operation_key,'domain':query.domain,'arguments':query.values,
                               'status':status,'execution_status':result.status,'fact_state':fact_state})
        if len(request.queries)==1:
            query=request.queries[0]; result=receipts[0]; task=context.task
            slots={**{field:value for field,value in context.slots.items() if field not in READ_OPERATION_FIELDS[query.domain]},
                   **query.values,'intents':[query.domain],'reference_time':context.reference_time}
            slots.pop('missing_fields',None)
            if query.domain=='transit':
                slots.pop('city_source',None)
            if result.payload and result.payload.get('place_resolution'):
                slots['place_resolution']=result.payload['place_resolution']
            elif result.status=='completed':
                slots.pop('place_resolution',None)
            slots['field_sources']={**slots.get('field_sources',{}),**{field:event.event_key for field,value in query.values.items() if isinstance(value,str) and value in event.content}}
            if query.domain=='hotel' and result.payload:
                slots=merge_hotel_state(slots,result.payload)
                if result.status=='completed':slots.pop('hotel_previous_status',None)
            for field in READ_OPERATION_FIELDS[query.domain]-query.values.keys():
                slots['field_sources'].pop(field,None)
            if query.domain=='transit' and result.status=='completed' and result.payload and result.payload.get('city'):
                slots['city']=result.payload['city']
                slots['city_source']=result.payload['city_source']
                slots['field_sources']['city']=event.event_key
                self.record(event,'read_city_resolved',city_source=result.payload['city_source'],adopted_fields=['city'])
            status=operations[0]['status'] if result.status in {'completed','needs_input'} else 'failed'
            answer=project_read_answer('',receipts) if result.status=='completed' else None
            reply=answer.reply if answer else result.data.removeprefix('工具错误：')
            if answer and (answer.status=='unverified' or 'answer_budget' in answer.reasons):status='failed'
            update=TaskUpdate(query.domain,'collecting' if status=='needs_input' else status,
                task.initial_request if task else event.content,slots,tuple(missing),task.task_id if task else '',task.version if task else None)
            if query.domain=='hotel' and (result.payload or {}).get('historical'):update=None
            if context.slots.get('_read_batch') and not (result.payload or {}).get('historical'):
                batch=context.slots['_read_batch'];i=batch['index']
                rows=deepcopy(batch['operations']);rows[i]=operations[0]
                saved=[ToolResult(**r) for r in batch['receipts']];saved[i]=result
                remaining=tuple(dict.fromkeys([f for r in saved for f in r.missing_fields]+
                    (['continue_query'] if any(row['domain']=='hotel' and row['status']=='failed' for row in rows) else [])))
                reply=project_read_answer('',saved).reply if any(r.status=='completed' for r in saved) else result.data
                status='needs_input' if remaining else 'completed' if all(r.status=='completed' for r in saved) else 'failed'
                update=TaskUpdate('read_batch','collecting' if remaining else status,task.initial_request,
                    {'operations':rows,'receipts':[asdict(r) for r in saved],'reference_time':context.reference_time,
                     'hotel_states':{**batch.get('hotel_states',{}),str(i):merge_hotel_state(context.slots,result.payload or {})}},remaining,task.task_id,task.version)
        else:
            status='completed' if all(row['status']=='completed' for row in operations) else 'failed'
            labels={'completed':'已完成','needs_input':'待补充','failed':'未完成'}
            answer=project_read_answer('',receipts)
            reply=answer.reply
            if answer.status=='unverified' or 'answer_budget' in answer.reasons:
                status='failed'
            else:
                summaries=[]
                for i,row in enumerate(operations,1):
                    args=row['arguments']
                    description=('、'.join(t.get('name','') for t in args.get('targets',[]))+' '+stay_text(args.get('stay',{}))) if row['domain']=='hotel' else json.dumps(args,ensure_ascii=False)
                    summaries.append(f"{i}. {row['domain']} · {description}：{labels.get(row['status'],'未完成')}")
                reply+='\n\n查询状态：\n'+'\n'.join(summaries)
            rail_batch=all(query.domain=='rail' for query in request.queries)
            slots={'operations':operations,'receipts':[asdict(r) for r in receipts],
                   'hotel_states':{str(i):merge_hotel_state({},r.payload or {}) for i,(q,r) in enumerate(zip(request.queries,receipts)) if q.domain=='hotel'},'rule_version':RULE_VERSION,
                   'reference_time':context.reference_time}
            if rail_batch:
                slots.update(queries=[query.values for query in request.queries],reference_time=context.reference_time)
            if any(row['domain']=='hotel' and row['status']=='failed' for row in operations):missing.append('continue_query')
            owner=context.task if context.task and context.task.task_type!='hotel' else None
            update=TaskUpdate('rail' if rail_batch else 'read_batch','collecting' if missing else status,
                event.content,slots,tuple(dict.fromkeys(missing)),owner.task_id if owner else '',owner.version if owner else None)
        self.record(event,'read_completed' if status=='completed' else 'read_incomplete',effect_status='none',
            execution_status=status,answer_status=answer.status if answer else 'not_checked',
            facts_hash=answer.facts_hash if answer else '',fact_count=len(answer.source_hashes) if answer else 0)
        return ExecutionOutcome(status,reply,update,tuple(receipts)).persist(self.store,event,claim)

    def command(self,content,command=None):
        event=ChatEvent('onebot','group','','','',content)
        # Compatibility callers have no user/event identity. They must not use
        # a runtime's scoped checkpoints or cache a current query indefinitely.
        owner=TravelQueryTool(backend=self)
        return owner.prepare(event,None,self.command_ir(content,command),source='command')


def query_argument_error(name,arguments,read_request):
    if read_request is None:
        return ''
    queries=[query for query in read_request.queries if name in TOOL_BY_INTENT[query.domain]]
    fields=TOOLS_BY_NAME[name]['function']['parameters']['properties'] if name in TOOLS_BY_NAME else {}
    if queries and not any(all(arguments.get(field)==value for field,value in query.values.items() if field in fields) for query in queries):
        return '查询参数与本轮已提供的字段不一致，请使用已校验的地点、城市等参数。'
    if name==TRAIN_TICKETS_TOOL and queries:
        for query in queries:
            defaults={'origin_scope':'station' if query.values.get('origin','').endswith('站') else 'city',
                      'destination_scope':'station' if query.values.get('destination','').endswith('站') else 'city',
                      'train_preference':'high_speed','limit':6,'departure_window':''}
            if all(value==query.values.get(field,defaults.get(field)) for field,value in arguments.items()):
                return ''
        return '铁路筛选条件与本轮已校验的请求不一致。'
    if name in TRAVEL_TOOL_NAMES and any(isinstance(value,str) and value
            and value not in read_request.context.message and value!=read_request.context.slots.get(field)
            for field,value in arguments.items()):
        return '查询参数无法对应本轮原话或已绑定任务，不得猜测地点。'
    return ''



def finish_read_turn(result, decision, task, read_request, resolved_message, reference_time):
    hotel=next((r for r in result.receipts if r.action=='query_hotel_prices'),None)
    if hotel and hotel.payload:
        payload=hotel.payload
        answer=project_read_answer(result.reply,result.receipts) if hotel.status=='completed' else None
        if payload.get('historical'):return replace(result,reply=answer.reply if answer else hotel.data,task_update=None)
        slots=merge_hotel_state(task.slots if task else {},payload)
        slots.update(intents=['hotel'],reference_time=reference_time)
        update=TaskUpdate('hotel','collecting' if hotel.status=='needs_input' else hotel.status,
            resolved_message,slots,hotel.missing_fields,task.task_id if task else '',task.version if task else None)
        return replace(result,reply=answer.reply if answer else hotel.data,task_update=update,
            status=hotel.status,missing_slots=hotel.missing_fields)
    pure_read = set(decision.intents) <= READ_TASK_TYPES
    if not pure_read and result.status == 'completed' and any(r.action in TRAVEL_TOOL_NAMES for r in result.receipts):
        answer = project_read_answer('', result.receipts)
        committed = any(r.effect_status == 'committed' for r in result.receipts)
        other_facts = [r.data for r in result.receipts if r.action not in TRAVEL_TOOL_NAMES
                       and (not committed or r.effect_status == 'committed')]
        incomplete = answer.status == 'unverified' or set(answer.reasons) & {'answer_budget', 'missing_facts', 'incomplete_tools'}
        return replace(result, reply='\n\n'.join(dict.fromkeys([*other_facts, answer.reply])),
                       status='partial' if incomplete else result.status, answer_status=answer.status,
                       answer_reasons=answer.reasons, answer_facts_hash=answer.facts_hash)
    if pure_read:
        if result.status in {'completed','partial'}:
            answer=project_read_answer(result.reply,result.receipts)
            status=result.status if answer.status!='unverified' else 'failed'
            if set(answer.reasons)&{'answer_budget','missing_facts','incomplete_tools'}:
                status='partial'
            result=replace(result,reply=answer.reply,status=status,answer_status=answer.status,answer_reasons=answer.reasons,answer_facts_hash=answer.facts_hash)
        missing = result.missing_slots if result.status == 'needs_input' else ()
        if not missing and result.status == 'needs_input' and decision.intent in {'weather', 'forecast'}:
            missing = ('location',)
        slots = dict(task.slots) if task else {}
        if read_request is not None:
            for query in read_request.queries:
                slots.update(query.values)
        slots['intents'] = list(decision.intents)
        if reference_time:
            slots['reference_time'] = reference_time
        for trace in result.traces:
            for field in ('location', 'city', 'origin', 'destination', 'keywords'):
                if trace.arguments.get(field):
                    slots[field] = trace.arguments[field]
        update = TaskUpdate(
            task_type=decision.intent,
            status="collecting" if missing else ('failed' if result.status == 'partial' else result.status),
            initial_request=resolved_message,
            slots=slots, missing_slots=missing,
            task_id=task.task_id if task else "",
            expected_version=task.version if task else None,
        )
        return replace(result, task_update=update)
    return result


def early_read_clarification(message, decision, request):
    if request is None and set(decision.intents) <= {'weather', 'forecast'} and missing_weather_location(message):
        return '请告诉我需要查询哪个城市的天气？', ()
    if decision.needs_clarification:
        missing = ('origin', 'destination') if decision.intent in {'route', 'traffic', 'transit', 'walking'} else ()
        return decision.clarification_reply or '请告诉我驾车起点和终点。', missing
    return None


def unexecuted_read_clarification(decision, executed, reply):
    return not executed and set(decision.intents) <= {'weather', 'forecast'} and is_weather_clarification(reply)


def read_turn_policy(read_request, decision, reference_time):
    if read_request is not None:
        if any(query.domain not in READ_TASK_TYPES for query in read_request.queries):
            raise ValueError('只读查询不能开放写操作。')
        reference_time=read_request.context.reference_time
        intents=tuple(dict.fromkeys(query.domain for query in read_request.queries))
        decision=TravelDecision(intent=intents[0],intents=intents,
            allowed_tools=tuple(dict.fromkeys(tool for intent in intents for tool in TOOL_BY_INTENT[intent])),
            required_tool_groups=tuple(TOOL_BY_INTENT[intent] for intent in intents),
            require_live_data=True,needs_clarification=False,response_detail='brief' if set(intents)<={'weather','forecast'} else 'normal')
    return decision, reference_time
