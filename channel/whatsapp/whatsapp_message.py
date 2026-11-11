"""Map a parsed WhatsApp ``InboundMessage`` onto cow's unified ``ChatMessage``."""

from bridge.context import ContextType
from channel.chat_message import ChatMessage
from channel.whatsapp.whatsapp_webhook import InboundMessage


class WhatsAppMessage(ChatMessage):
    def __init__(self, inbound: InboundMessage, ctype: ContextType, content: str, self_id: str = ""):
        super().__init__(inbound)
        self.msg_id = inbound.msg_id
        self.create_time = inbound.timestamp
        self.ctype = ctype
        self.content = content

        nickname = inbound.sender_name or inbound.sender_id
        self.from_user_id = inbound.sender_id
        self.from_user_nickname = nickname
        self.to_user_id = self_id or "whatsapp_business"
        self.to_user_nickname = self.to_user_id

        self.is_group = bool(inbound.group_id)
        if self.is_group:
            self.other_user_id = inbound.group_id
            self.other_user_nickname = inbound.group_id
            self.actual_user_id = inbound.sender_id
            self.actual_user_nickname = nickname
            # The channel applies the group trigger before building this message,
            # so a group message that gets this far is addressed to the bot.
            self.is_at = True
        else:
            self.other_user_id = inbound.sender_id
            self.other_user_nickname = nickname
