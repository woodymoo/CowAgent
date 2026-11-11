"""Agent tool: create and manage WhatsApp groups through the Cloud API Groups API.

Offered only while a WhatsApp channel is running (``is_available`` is checked
every turn, and channels start after tools load). Channel imports are lazy, so
loading the tool registry never pulls in the channel. The tool is a thin adapter:
validation and HTTP live in ``channel.whatsapp.whatsapp_groups``.
"""

import json

from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger

_GROUP_ACTIONS = {
    "info", "delete", "get_invite_link", "reset_invite_link", "list_join_requests",
}


def _running_group_service():
    """The group service of the running WhatsApp channel, or ``None``."""
    from common import channel_registry

    manager = channel_registry.get_channel_manager()
    if manager is None:
        return None
    services = [getattr(ch, "group_service", None) for _name, ch in manager.find_channels_by_type("whatsapp")]
    services = [s for s in services if s is not None]
    if len(services) > 1:
        logger.warning("[whatsapp_group] several WhatsApp channels running; using the first")
    return services[0] if services else None


class WhatsAppGroupTool(BaseTool):
    name: str = "whatsapp_group"
    description: str = (
        "Create and manage WhatsApp groups owned by this WhatsApp business number (Meta Cloud API "
        "Groups API). Limits: requires an Official Business Account; at most 8 participants; people "
        "cannot be added directly. Share the group's invite link (get_invite_link) and they join "
        "through it, or approve their join requests. After 'create', Meta delivers the group id and "
        "invite link asynchronously: if 'create' returns no id, call 'list' to find the new group. "
        "To message a group, reply in it or use the group id as the receiver with a 'group:' prefix."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "info", "update", "delete", "get_invite_link",
                         "reset_invite_link", "remove_participants", "list_join_requests",
                         "approve_join_requests", "reject_join_requests"],
                "description": "Operation to perform.",
            },
            "group_id": {"type": "string", "description": "Target group id (all actions except create/list)."},
            "subject": {"type": "string", "description": "Group name, max 128 chars (create, update)."},
            "description": {"type": "string", "description": "Group description, max 2048 chars (create, update)."},
            "join_approval_mode": {
                "type": "string", "enum": ["auto_approve", "approval_required"],
                "description": "create only: whether join requests need approval.",
            },
            "participants": {
                "type": "array", "items": {"type": "string"},
                "description": "remove_participants: phone numbers / WhatsApp ids (1-8).",
            },
            "join_request_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "approve/reject_join_requests: ids from list_join_requests.",
            },
            "limit": {"type": "integer", "description": "list: page size 1-1024 (default 25)."},
            "after": {"type": "string", "description": "list: paging cursor."},
        },
        "required": ["action"],
    }

    def is_available(self) -> bool:
        try:
            return _running_group_service() is not None
        except Exception as e:
            logger.debug(f"[whatsapp_group] availability check failed: {e}")
            return False

    def execute(self, args: dict) -> ToolResult:
        from channel.whatsapp.whatsapp_api import WhatsAppApiError

        action = (args or {}).get("action")
        try:
            service = _running_group_service()
        except Exception as e:
            return ToolResult.fail(f"WhatsApp channel lookup failed: {e}")
        if service is None:
            return ToolResult.fail("WhatsApp channel is not running; start the 'whatsapp' channel first.")
        try:
            result = self._dispatch(service, action, args)
        except WhatsAppApiError as e:
            logger.warning(f"[whatsapp_group] {action} failed: {e}")
            return ToolResult.fail(json.dumps({"error": e.to_dict()}, ensure_ascii=False))
        except (ValueError, TypeError) as e:
            return ToolResult.fail(f"Invalid arguments for '{action}': {e}")
        return ToolResult.success(json.dumps(result, ensure_ascii=False))

    @staticmethod
    def _dispatch(service, action, args):
        if action == "create":
            return service.create(args.get("subject"), description=args.get("description"),
                                  join_approval_mode=args.get("join_approval_mode"))
        if action == "list":
            kwargs = {"limit": args.get("limit") or 25}
            if args.get("after"):
                kwargs["after"] = args["after"]
            return service.list_groups(**kwargs)
        if action == "update":
            return service.update(args.get("group_id"), subject=args.get("subject"),
                                  description=args.get("description"))
        if action == "remove_participants":
            return service.remove_participants(args.get("group_id"), args.get("participants"))
        if action == "approve_join_requests":
            return service.approve_join_requests(args.get("group_id"), args.get("join_request_ids"))
        if action == "reject_join_requests":
            return service.reject_join_requests(args.get("group_id"), args.get("join_request_ids"))
        if action in _GROUP_ACTIONS:
            return getattr(service, action)(args.get("group_id"))
        raise ValueError(f"unknown action {action!r}")
