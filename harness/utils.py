# share_model.py

import json
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, create_model, Field

# Primitive JSON→Python type map
_type_map: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def resolve_type(
    prop: dict,
    defs: Dict[str, Any],
    models: Dict[str, type[BaseModel]],
    field_name: str,
) -> Any:
    """
    Recursively resolve a JSON-schema property into a Python type:
      - enum → dynamic Enum
      - primitives
      - arrays (of primitives or $ref)
      - $ref → nested model
      - anyOf → Optional or Union
    """
    # ── 1) enum → dynamic Enum class ────────────────────────────────────────
    if "enum" in prop:
        enum_name = f"{field_name[0].upper()}{field_name[1:]}Enum"
        members = {val: val for val in prop["enum"]}
        return Enum(enum_name, members)

    # ── 2) direct primitive or array ───────────────────────────────────────
    if "type" in prop:
        jt = prop["type"]
        if jt == "array":
            items = prop["items"]
            if "$ref" in items:
                ref = items["$ref"].split("/")[-1]
                return List[models[ref]]
            return List[_type_map[items["type"]]]
        return _type_map[jt]

    # ── 3) reference to a definition ───────────────────────────────────────
    if "$ref" in prop:
        ref = prop["$ref"].split("/")[-1]
        return models[ref]

    # ── 4) anyOf → nullable or union ──────────────────────────────────────
    if "anyOf" in prop:
        sub_types = []
        is_nullable = False
        for p in prop["anyOf"]:
            if p.get("type") == "null":
                is_nullable = True
                continue
            if "$ref" in p:
                ref = p["$ref"].split("/")[-1]
                sub_types.append(models[ref])
            else:
                sub_types.append(_type_map[p["type"]])
        base = sub_types[0] if len(sub_types) == 1 else Union[tuple(sub_types)]
        return Optional[base] if is_nullable else base

    raise ValueError(f"Unsupported schema entry for '{field_name}': {prop!r}")


def import_model(schema_json: str) -> type[BaseModel]:
    """
    Parse the JSON schema and dynamically build:
    1) all nested models from `$defs`
    2) the top-level model with enum, optional, nested lists, etc.
    """
    schema = json.loads(schema_json)
    defs = schema.get("$defs", {})

    # 1) Build nested models first
    models: Dict[str, type[BaseModel]] = {}
    for def_name, def_schema in defs.items():
        props = def_schema.get("properties", {})
        req = set(def_schema.get("required", []))
        fields: Dict[str, tuple[Any, Any]] = {}
        for fname, fprop in props.items():
            py_type = resolve_type(fprop, defs, models, fname)
            if "default" in fprop:
                default = fprop["default"]
            elif fname not in req:
                default = None
            else:
                default = ...
            fields[fname] = (py_type, default)
        models[def_name] = create_model(def_name, **fields)

    # 2) Build the top-level model
    top_props = schema["properties"]
    top_req = set(schema.get("required", []))
    top_fields: Dict[str, tuple[Any, Any]] = {}
    for name, prop in top_props.items():
        py_type = resolve_type(prop, defs, models, name)
        if "default" in prop:
            default = prop["default"]
        elif name not in top_req:
            default = None
        else:
            default = ...
        top_fields[name] = (py_type, default)

    top_name = schema.get("title", "DynamicModel")
    return create_model(top_name, **top_fields, __base__=BaseModel)


def export_model(model: type[BaseModel]) -> str:
    """Serialize the model’s JSON schema to a JSON string."""
    return json.dumps(model.model_json_schema(), separators=(",", ":"))


# # ─── DEMO ───────────────────────────────────────────────────────────────────────

# # ─── SENDER ────────────────────────────────────────────────────────────────────
# from typing import Literal

# class RecentView(BaseModel):
#     profile_url: str = Field(
#         description="Eg: https://.../in/swaroopa-dutta/"
#     )
#     name: str = Field(description="Eg: Swaroopa Dutta")
#     title: str = Field(description="Eg: Product Manager at LinkedIn")
#     viewed_at: str = Field(
#         description="estimate ISO-format date/time, e.g. 2025-05-21T10:00:00Z"
#     )

# class FetchLatestViewsResponse(BaseModel):
#     answer_status: Literal["found", "not_found"]
#     recent_views: List[RecentView] = Field(
#         description="The list of all the new views, upto a month old."
#     )


# # ─── RECEIVER ──────────────────────────────────────────────────────────────────

# if __name__ == "__main__":
#     # Sender → export schema
#     schema_json = export_model(FetchLatestViewsResponse)
#     print("── transmitted schema ──")
#     print(schema_json, "\n")

#     # Receiver → reconstruct
#     DynamicResp = import_model(schema_json)

#     # Test it!
#     sample = {
#         "answer_status": "found",
#         "recent_views": [
#             {
#                 "profile_url": "https://linkedin.com/in/test",
#                 "name": "Test User",
#                 "title": "Dev",
#                 "viewed_at": "2025-06-11T12:00:00Z",
#             }
#         ],
#     }
#     inst = DynamicResp(**sample)
#     print("── instance ──")
#     print(inst)
#     print("── as dict ──")
#     print(inst.dict())
