from dataclasses import asdict, dataclass
import json


class ReadFailed(RuntimeError):
    def __init__(self, result):
        super().__init__(result.error_code or result.status)
        self.retryable = result.status == 'failed' and result.retryable
        self.retry_after_seconds = result.retry_after_seconds
        if self.retry_after_seconds is not None and self.retry_after_seconds > 86400:
            self.retryable = False
        if not self.retryable:
            self.retry_after_seconds = None


@dataclass(frozen=True)
class ToolResult:
    status: str
    action: str
    resource_id: str = ""
    state_version: int | None = None
    data: str = ""
    error_code: str = ""
    retryable: bool = False
    retry_after_seconds: int | None = None
    effect_status: str = 'none'
    operation_key: str = ''
    artifact_id: str = ''
    attempts: int = 1
    missing_fields: tuple[str, ...] = ()
    payload: dict | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_text(cls, action: str, text: str, resource_id: str = ""):
        failed = text.startswith("工具错误：")
        error_code, retryable, retry_after = '', False, None
        if failed:
            normalized = text.lower()
            if any(value in normalized for value in ('429', 'rate limit', '频率', '限流')):
                error_code, retryable, retry_after = 'rate_limit', True, 30
            elif any(value in normalized for value in ('超时', 'timeout', '网络', '连接', '暂时', '稍后', '繁忙', '5xx')):
                error_code, retryable, retry_after = 'temporary_upstream', True, 5
            elif any(value in normalized for value in ('权限', '不属于', '无权')):
                error_code = 'permission_denied'
            elif any(value in normalized for value in ('缺少', '参数', '格式')):
                error_code = 'invalid_argument'
            elif any(value in normalized for value in ('未配置', '未知工具', '不支持')):
                error_code = 'not_available'
            else:
                error_code = 'tool_error'
        return cls(
            status="failed" if failed else "completed", action=action,
            resource_id=resource_id, data=text,
            error_code=error_code, retryable=retryable, retry_after_seconds=retry_after,
        )


def validate_arguments(value, schema: dict, path: str = "参数") -> str:
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    expected = schema.get("type")
    if expected in types and type(value) is not types[expected]:
        return f"{path} 必须是 {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} 不在允许值中"
    if expected == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", ())):
            return f"{path} 缺少必要字段"
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            return f"{path} 包含未定义字段"
        for key, item in value.items():
            error = validate_arguments(item, properties.get(key, {}), f"{path}.{key}")
            if error:
                return error
    if expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            return f"{path} 数量超出范围"
        for item in value:
            error = validate_arguments(item, schema.get("items", {}), path)
            if error:
                return error
    if expected == "integer" and value < schema.get("minimum", value):
        return f"{path} 小于允许值"
    if expected == "integer" and value > schema.get("maximum", value):
        return f"{path} 大于允许值"
    return ""
