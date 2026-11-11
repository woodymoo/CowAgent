"""WhatsAppMessage maps a parsed InboundMessage onto the unified ChatMessage fields."""
from bridge.context import ContextType
from channel.whatsapp.whatsapp_message import WhatsAppMessage
from channel.whatsapp.whatsapp_webhook import InboundMessage


def test_one_to_one_fields():
    inbound = InboundMessage(msg_id="wamid.1", sender_id="1555", msg_type="text", timestamp=7,
                             sender_name="Alice", text="hi")
    m = WhatsAppMessage(inbound, ContextType.TEXT, "hi", self_id="PNID")
    assert (m.msg_id, m.create_time, m.ctype, m.content) == ("wamid.1", 7, ContextType.TEXT, "hi")
    assert (m.from_user_id, m.from_user_nickname) == ("1555", "Alice")
    assert (m.other_user_id, m.other_user_nickname) == ("1555", "Alice")
    assert m.to_user_id == "PNID"
    assert not m.is_group
    assert m.actual_user_id is None


def test_group_fields():
    inbound = InboundMessage(msg_id="wamid.2", sender_id="1555", msg_type="text",
                             sender_name="", group_id="GRP")
    m = WhatsAppMessage(inbound, ContextType.TEXT, "yo", self_id="PNID")
    assert m.is_group and m.is_at
    assert m.other_user_id == "GRP"
    assert m.actual_user_id == "1555"
    assert m.actual_user_nickname == "1555"  # falls back to id when no profile name
    assert m.from_user_nickname == "1555"
    assert m._rawmsg is inbound
