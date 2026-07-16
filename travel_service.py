from amap_client import (
    AmapClient,
    AmapError,
    CurrentWeather,
    RouteSummary,
    TrafficSegment,
    WeatherForecast,
)
from commands import HELP_TEXT, parse_command
from settings import Settings


RELIABLE_TRAFFIC_COVERAGE_RATIO = 0.7
MAX_CONGESTED_SEGMENTS_TO_SHOW = 6
UNKNOWN_TRAFFIC_STATUSES = {"", "未知", "未知路况", "无数据"}


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
        f"距离：{_format_distance(route.distance_meters)}",
        f"预计耗时：{_format_duration(route.duration_seconds)}",
        f"预计收费：{route.tolls_yuan} 元",
        f"红绿灯：{route.traffic_lights} 个",
        "说明：预计耗时会随高德实时路况变化。",
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


class TravelService:
    def __init__(self, settings: Settings):
        self.llm_configured = settings.llm_configured
        self.amap = (
            AmapClient(settings.amap_api_key)
            if settings.amap_api_key
            else None
        )

    def execute_tool(self, name: str, arguments: dict[str, str]) -> str:
        if not self.amap:
            return "工具错误：尚未配置 AMAP_API_KEY。"

        try:
            if name == "get_current_weather":
                location = arguments.get("location", "").strip()
                if not location:
                    return "工具错误：缺少 location。"
                return _format_weather(self.amap.current_weather(location))

            if name == "get_weather_forecast":
                location = arguments.get("location", "").strip()
                if not location:
                    return "工具错误：缺少 location。"
                return _format_forecast(self.amap.weather_forecast(location))

            if name in {"get_driving_route", "get_route_traffic"}:
                origin = arguments.get("origin", "").strip()
                destination = arguments.get("destination", "").strip()
                if not origin or not destination:
                    return "工具错误：缺少 origin 或 destination。"
                route = self.amap.driving_route(origin, destination)
                if name == "get_driving_route":
                    return _format_route(route)
                return _format_traffic(route)
        except AmapError as exc:
            return f"工具错误：{exc}"

        return f"工具错误：未知工具 {name}。"

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
            return (
                f"Bot 已在线。高德数据源：{amap_status}；"
                f"LLM Agent：{llm_status}。"
            )
        if command.name == "unknown":
            return "暂未识别该指令。发送“帮助”查看当前可用指令。"
        if not self.amap:
            return "尚未配置 AMAP_API_KEY，请先在 .env 中添加高德 Web 服务 Key。"

        if command.name == "weather":
            return self.execute_tool(
                "get_current_weather",
                {"location": command.args[0]},
            ).removeprefix("工具错误：")
        if command.name == "forecast":
            return self.execute_tool(
                "get_weather_forecast",
                {"location": command.args[0]},
            ).removeprefix("工具错误：")
        if command.name == "route":
            return self.execute_tool(
                "get_driving_route",
                {"origin": command.args[0], "destination": command.args[1]},
            ).removeprefix("工具错误：")
        if command.name == "traffic":
            return self.execute_tool(
                "get_route_traffic",
                {"origin": command.args[0], "destination": command.args[1]},
            ).removeprefix("工具错误：")

        return "暂未识别该指令。发送“帮助”查看当前可用指令。"
