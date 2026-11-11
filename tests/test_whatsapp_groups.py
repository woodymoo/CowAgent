"""WhatsAppGroupService: one method per Groups API endpoint, with arguments
validated before any call reaches Meta."""
import pytest

from channel.whatsapp.whatsapp_groups import WhatsAppGroupService


class FakeClient:
    phone_number_id = "PNID"

    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {"ok": True}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def svc(client):
    return WhatsAppGroupService(client)


def test_create(svc, client):
    svc.create("  Team  ", description="d", join_approval_mode="approval_required")
    method, path, kw = client.calls[0]
    assert (method, path) == ("POST", "PNID/groups")
    assert kw["json"] == {"messaging_product": "whatsapp", "subject": "Team", "description": "d",
                          "join_approval_mode": "approval_required"}


def test_create_minimal_omits_optional_fields(svc, client):
    svc.create("Team")
    assert client.calls[0][2]["json"] == {"messaging_product": "whatsapp", "subject": "Team"}


@pytest.mark.parametrize("subject", ["", "   ", "x" * 129, None])
def test_create_rejects_bad_subject(svc, client, subject):
    with pytest.raises(ValueError):
        svc.create(subject)
    assert client.calls == []


def test_create_rejects_long_description_and_bad_mode(svc):
    with pytest.raises(ValueError):
        svc.create("ok", description="x" * 2049)
    with pytest.raises(ValueError):
        svc.create("ok", join_approval_mode="open")


def test_list_with_paging(svc, client):
    svc.list_groups(limit=10, after="CUR")
    method, path, kw = client.calls[0]
    assert (method, path) == ("GET", "PNID/groups")
    assert kw["params"] == {"limit": 10, "after": "CUR"}


def test_list_rejects_out_of_range_limit(svc):
    with pytest.raises(ValueError):
        svc.list_groups(limit=0)
    with pytest.raises(ValueError):
        svc.list_groups(limit=1025)


def test_info_requests_all_fields(svc, client):
    svc.info("G1")
    method, path, kw = client.calls[0]
    assert (method, path) == ("GET", "G1")
    assert set(kw["params"]["fields"].split(",")) >= {"subject", "participants", "join_approval_mode"}


def test_update_requires_something_to_change(svc, client):
    with pytest.raises(ValueError):
        svc.update("G1")
    svc.update("G1", subject="New")
    assert client.calls[-1] == ("POST", "G1", {"json": {"messaging_product": "whatsapp", "subject": "New"}})


def test_update_rejects_empty_subject(svc):
    with pytest.raises(ValueError):
        svc.update("G1", subject="  ")


def test_delete(svc, client):
    client.response = {"id": "G1", "subject": "Team"}
    svc.delete("G1")
    assert client.calls[0][:2] == ("GET", "G1")      # confirms it is a group first
    assert client.calls[1][:2] == ("DELETE", "G1")


@pytest.mark.parametrize("op", ["delete", "remove_participants"])
def test_destructive_ops_refuse_non_group_nodes(svc, client, op):
    """A user could talk the agent into DELETE /<waba-id>; only real groups qualify."""
    client.response = {"id": "123", "name": "not a group"}
    args = ("123",) if op == "delete" else ("123", ["1555"])
    with pytest.raises(ValueError):
        getattr(svc, op)(*args)
    assert [c[0] for c in client.calls] == ["GET"]


def test_invite_link_get_and_reset(svc, client):
    svc.get_invite_link("G1")
    svc.reset_invite_link("G1")
    assert client.calls[0][:2] == ("GET", "G1/invite_link")
    assert client.calls[1] == ("POST", "G1/invite_link", {"json": {"messaging_product": "whatsapp"}})


def test_remove_participants(svc, client):
    client.response = {"id": "G1", "subject": "Team"}
    svc.remove_participants("G1", ["15551234567", "+15557654321"])
    method, path, kw = client.calls[1]
    assert (method, path) == ("DELETE", "G1/participants")
    assert kw["json"]["participants"] == [{"user": "15551234567"}, {"user": "15557654321"}]


@pytest.mark.parametrize("users", [[], ["1"] * 9, "15551234567", [""]])
def test_remove_participants_validates(svc, users):
    with pytest.raises(ValueError):
        svc.remove_participants("G1", users)


def test_join_requests(svc, client):
    svc.list_join_requests("G1")
    svc.approve_join_requests("G1", ["r1", "r2"])
    svc.reject_join_requests("G1", ["r3"])
    assert client.calls[0][:2] == ("GET", "G1/join_requests")
    assert client.calls[1] == ("POST", "G1/join_requests",
                               {"json": {"messaging_product": "whatsapp", "join_requests": ["r1", "r2"]}})
    assert client.calls[2][:2] == ("DELETE", "G1/join_requests")


def test_join_requests_require_ids(svc):
    with pytest.raises(ValueError):
        svc.approve_join_requests("G1", [])


@pytest.mark.parametrize("group_id", ["", None, "../me", "G 1", "me", ".", "PNID"])
def test_group_id_is_validated(svc, client, group_id):
    with pytest.raises(ValueError):
        svc.info(group_id)
    assert client.calls == []
