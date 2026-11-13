"""Tool arguments derived from the reviewed operation's authoritative API schema."""

import json
from typing import Any

from ..authorization.operations import Operation


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
    if "multipart/form-data" in content:
        multipart = content["multipart/form-data"]["schema"]
        if "$ref" in multipart:
            multipart = document["components"]["schemas"][multipart["$ref"].rsplit("/", 1)[-1]]
        file_schema = multipart["properties"]["file"]
        properties["files"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": min(file_schema.get("maxItems", 20), 20) if file_schema.get("type") == "array" else 1,
            "items": {"type": "string", "format": "uuid"},
            "description": "Handles from POST /api/v1/mcp/files using this app's MCP token.",
        }
        required.append("files")
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
    idempotency = next(
        (p for p in route.get("parameters", []) if p["in"] == "header" and p["name"].lower() == "idempotency-key"),
        None,
    )
    if idempotency is not None:
        header = idempotency["schema"]
        if "anyOf" in header:
            header = next(branch for branch in header["anyOf"] if branch.get("type") == "string")
        properties["idempotency_key"] = {
            **{key: value for key, value in header.items() if key != "default"},
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
    if operation.account_view is not None and operation.method == "PATCH":
        allowed = operation.account_view.write_fields
        user = schema["$defs"]["UserUpdate"]
        user["properties"] = {key: value for key, value in user["properties"].items() if key in allowed}
        user["additionalProperties"] = False
    return schema
