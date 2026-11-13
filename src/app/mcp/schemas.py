"""Tool arguments derived from the reviewed operation's authoritative API schema."""

import json
from typing import Any

from .catalog import Operation


def argument_schema(operation: Operation, document: dict[str, Any]) -> dict[str, Any]:
    route = document["paths"][operation.path][operation.method.lower()]
    properties: dict[str, Any] = {}
    required = []
    for location in ("path", "query"):
        params = [p for p in route.get("parameters", []) if p["in"] == location and p["name"] != "ticket"]
        if params:
            mandatory = [p["name"] for p in params if p.get("required")]
            properties[location] = {
                "type": "object",
                "properties": {p["name"]: p["schema"] for p in params},
                "additionalProperties": False,
                "required": mandatory,
            }
            if mandatory:
                required.append(location)
    content = route.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        properties["body"] = content["application/json"]["schema"]
        if route["requestBody"].get("required"):
            required.append("body")
    if "multipart/form-data" in content or operation.path.startswith("/api/v1/import/"):
        properties["files"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string", "format": "uuid"},
            "description": "Handles from POST /api/v1/mcp/files using this app's MCP token.",
        }
        required.append("files")
        multipart = content.get("multipart/form-data", {}).get("schema", {})
        if "$ref" in multipart:
            multipart = document["components"]["schemas"][multipart["$ref"].rsplit("/", 1)[-1]]
        fields = {key: value for key, value in multipart.get("properties", {}).items() if key != "file"}
        mandatory = [key for key in multipart.get("required", []) if key in fields]
        properties["body"] = {
            "type": "object",
            "properties": fields,
            "required": mandatory,
            "additionalProperties": False,
            "description": "Non-file multipart form fields.",
        }
        if mandatory:
            required.append("body")
    if operation.method == "POST":
        properties["idempotency_key"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "Reuse the same key when retrying a create request.",
        }
    if operation.name == "read_dive_profile":
        properties["max_profile_points"] = {
            "type": "integer",
            "minimum": 2,
            "maximum": 5000,
            "default": 500,
            "description": "Maximum points per channel. Output reports full-series extrema and original counts.",
        }
    schema: dict[str, Any] = json.loads(
        json.dumps({"type": "object", "properties": properties, "required": required, "additionalProperties": False})
    )
    definitions = document.get("components", {}).get("schemas", {})
    selected: dict[str, Any] = {}

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                key = ref.rsplit("/", 1)[-1]
                value["$ref"] = "#/$defs/" + key
                if key not in selected:
                    selected[key] = json.loads(json.dumps(definitions[key]))
                    collect(selected[key])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(schema)
    if selected:
        schema["$defs"] = selected
    if operation.name in {"patch_user", "patch_preferences"}:
        allowed = (
            {"name", "username"}
            if operation.name == "patch_user"
            else {
                "gear_service_emails",
                "renewal_reminder_emails",
                "year_in_review_emails",
                "units",
                "dive_form_hidden_fields",
                "dive_form_preset_uuid",
            }
        )
        user = schema["$defs"]["UserUpdate"]
        user["properties"] = {key: value for key, value in user["properties"].items() if key in allowed}
        user["additionalProperties"] = False
    return schema
