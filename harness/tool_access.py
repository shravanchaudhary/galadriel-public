"""Actor-based tool visibility policy, independent of agent state."""

from __future__ import annotations


UNTRUSTED_READ_ONLY_TOOLS = {
    "read_file", "memory", "recall", "palace_search", "palace_taxonomy", "palace_kg_query",
    "palace_kg_timeline", "palace_diary_read", "google_search",
    "fetch_url_data", "db_get", "db_query", "run_shell", "wait",
}


def is_untrusted_organization_slack(context: dict | None) -> bool:
    context = context or {}
    return (
        context.get("source") == "slack"
        and context.get("replika_type") == "organization"
        and not context.get("trusted", False)
    )


def tools_for_request(tools: list[dict], context: dict | None) -> list[dict]:
    """Filter solely from actor trust; experiential state has no authority."""
    if not is_untrusted_organization_slack(context):
        return tools
    filtered = [
        {key: value for key, value in tool.items() if key != "cache_control"}
        for tool in tools
        if tool.get("name") in UNTRUSTED_READ_ONLY_TOOLS
    ]
    if filtered:
        filtered[-1] = {
            **filtered[-1], "cache_control": {"type": "ephemeral"},
        }
    return filtered
