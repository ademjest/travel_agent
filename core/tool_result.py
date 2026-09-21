from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class ToolResult:
    status: str
    action: str
    resource_id: str = ""
    state_version: int | None = None
    data: str = ""
    error_code: str = ""
    retryable: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_text(cls, action: str, text: str, resource_id: str = ""):
        failed = text.startswith("工具错误：")
        return cls(
            status="failed" if failed else "completed", action=action,
            resource_id=resource_id, data=text,
            error_code="tool_error" if failed else "", retryable=failed,
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
    return ""
