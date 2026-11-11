"""whatsapp_group agent tool: offered only while a WhatsApp channel is running,
dispatches each action to that channel's group service, and hands Meta's errors
back to the Agent verbatim."""
import json
from types import SimpleNamespace

import pytest

from agent.tools.whatsapp_group.whatsapp_group import WhatsAppGroupTool
from channel.whatsapp.whatsapp_api import WhatsAppApiError


class FakeGroups:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return {"ok": name}
        return method


class FakeManager:
    def __init__(self, channels):
        self.channels = channels

    def find_channels_by_type(self, channel_type):
        assert channel_type == "whatsapp"
        return [(f"whatsapp{i}", ch) for i, ch in enumerate(self.channels)]


@pytest.fixture
def groups():
    return FakeGroups()


@pytest.fixture
def tool(monkeypatch, groups):
    monkeypatch.setattr("common.channel_registry.get_channel_manager",
                        lambda: FakeManager([SimpleNamespace(group_service=groups)]))
    return WhatsAppGroupTool()


def test_schema_lists_all_actions():
    actions = set(WhatsAppGroupTool.params["properties"]["action"]["enum"])
    assert actions == {"create", "list", "info", "update", "delete", "get_invite_link",
                       "reset_invite_link", "remove_participants", "list_join_requests",
                       "approve_join_requests", "reject_join_requests"}
    assert WhatsAppGroupTool.params["required"] == ["action"]
    assert "instance_id" not in WhatsAppGroupTool.params["properties"]


def test_construction_does_no_io(monkeypatch):
    def boom():
        raise AssertionError("constructor must not look up channels")

    monkeypatch.setattr("common.channel_registry.get_channel_manager", boom)
    WhatsAppGroupTool()


def test_available_only_with_running_channel(monkeypatch, groups):
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: None)
    assert WhatsAppGroupTool().is_available() is False
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: FakeManager([]))
    assert WhatsAppGroupTool().is_available() is False
    not_started = SimpleNamespace(group_service=None)
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: FakeManager([not_started]))
    assert WhatsAppGroupTool().is_available() is False
    started = SimpleNamespace(group_service=groups)
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: FakeManager([started]))
    assert WhatsAppGroupTool().is_available() is True


def test_is_available_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("registry broken")

    monkeypatch.setattr("common.channel_registry.get_channel_manager", boom)
    assert WhatsAppGroupTool().is_available() is False


@pytest.mark.parametrize("args,expected", [
    ({"action": "create", "subject": "Team", "description": "d", "join_approval_mode": "auto_approve"},
     ("create", ("Team",), {"description": "d", "join_approval_mode": "auto_approve"})),
    ({"action": "list", "limit": 5, "after": "C"}, ("list_groups", (), {"limit": 5, "after": "C"})),
    ({"action": "info", "group_id": "G"}, ("info", ("G",), {})),
    ({"action": "update", "group_id": "G", "subject": "S"},
     ("update", ("G",), {"subject": "S", "description": None})),
    ({"action": "delete", "group_id": "G"}, ("delete", ("G",), {})),
    ({"action": "get_invite_link", "group_id": "G"}, ("get_invite_link", ("G",), {})),
    ({"action": "reset_invite_link", "group_id": "G"}, ("reset_invite_link", ("G",), {})),
    ({"action": "remove_participants", "group_id": "G", "participants": ["1"]},
     ("remove_participants", ("G", ["1"]), {})),
    ({"action": "list_join_requests", "group_id": "G"}, ("list_join_requests", ("G",), {})),
    ({"action": "approve_join_requests", "group_id": "G", "join_request_ids": ["r"]},
     ("approve_join_requests", ("G", ["r"]), {})),
    ({"action": "reject_join_requests", "group_id": "G", "join_request_ids": ["r"]},
     ("reject_join_requests", ("G", ["r"]), {})),
])
def test_dispatch(tool, groups, args, expected):
    result = tool.execute(args)
    assert result.status == "success", result.result
    assert groups.calls == [expected]
    assert json.loads(result.result) == {"ok": expected[0]}


def test_unknown_action_fails(tool):
    assert tool.execute({"action": "add_participant"}).status == "error"


def test_validation_error_is_reported(tool, groups):
    groups.error = ValueError("subject must not be empty")
    result = tool.execute({"action": "create", "subject": ""})
    assert result.status == "error"
    assert "subject must not be empty" in result.result


def test_meta_error_is_returned_verbatim(tool, groups):
    groups.error = WhatsAppApiError(400, code=131215, title="Not eligible",
                                    message="Groups API requires an Official Business Account")
    result = tool.execute({"action": "create", "subject": "Team"})
    assert result.status == "error"
    body = json.loads(result.result)
    assert body["error"]["code"] == 131215
    assert "Official Business Account" in body["error"]["message"]


def test_execute_without_running_channel_fails_cleanly(monkeypatch):
    monkeypatch.setattr("common.channel_registry.get_channel_manager", lambda: None)
    result = WhatsAppGroupTool().execute({"action": "list"})
    assert result.status == "error"
    assert "not running" in result.result
