from amap_client import (
    AmapClient,
    AmapError,
    CurrentWeather,
    RouteSummary,
    WeatherForecast,
)
from commands import HELP_TEXT, parse_command
from settings import Settings


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


def _format_traffic(route: RouteSummary) -> str:
    known_distances = {
        status: distance
        for status, distance in route.traffic_distances.items()
        if status != "未知" and distance > 0
    }
    total = sum(known_distances.values())
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

        traffic_summary = [f"车辆拥堵风险：{level}"]
        for status in ("畅通", "缓行", "拥堵", "严重拥堵"):
            distance = known_distances.get(status, 0)
            if distance:
                traffic_summary.append(
                    f"{status}：{_format_distance(distance)}"
                )

    return "\n".join([
        "【高德实时路线与路况】",
        f"起点：{route.origin.address}",
        f"终点：{route.destination.address}",
        f"总距离：{_format_distance(route.distance_meters)}",
        f"当前预计耗时：{_format_duration(route.duration_seconds)}",
        *traffic_summary,
        "说明：路况会变化，请以出发前再次查询结果为准。",
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
