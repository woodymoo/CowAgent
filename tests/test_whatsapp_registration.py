"""The whatsapp channel type is wired into every registration point, and stays
out of multi-instance (its webhook port is global config, not a credential)."""
from common import const
from config import available_setting


def test_const():
    assert const.WHATSAPP == "whatsapp"


def test_factory_builds_singleton_channel():
    from channel.channel_factory import create_channel
    from channel.whatsapp.whatsapp_channel import WhatsAppChannel

    ch = create_channel("whatsapp")
    assert isinstance(ch, WhatsAppChannel.__wrapped__)
    assert ch.channel_type == "whatsapp"
    assert create_channel("whatsapp") is ch


def test_not_multi_instance_ready():
    from channel.channel_instances import MULTI_INSTANCE_READY

    assert const.WHATSAPP not in MULTI_INSTANCE_READY


def test_config_defaults():
    for key, default in {
        "whatsapp_access_token": "", "whatsapp_phone_number_id": "", "whatsapp_app_secret": "",
        "whatsapp_verify_token": "", "whatsapp_port": 9893, "whatsapp_api_version": "v21.0",
        "whatsapp_group_trigger": "all", "whatsapp_rate_limit_mps": 20,
        "whatsapp_pair_interval_seconds": 6, "whatsapp_pair_burst": 5, "whatsapp_max_wait_seconds": 120,
    }.items():
        assert available_setting[key] == default, key


def test_web_console_channel_definition():
    from channel.web.api.channels import ChannelsHandler

    defn = ChannelsHandler.CHANNEL_DEFS["whatsapp"]
    keys = {f["key"]: f["type"] for f in defn["fields"]}
    assert keys == {"whatsapp_phone_number_id": "text", "whatsapp_access_token": "secret",
                    "whatsapp_app_secret": "secret", "whatsapp_verify_token": "secret",
                    "whatsapp_port": "number"}
    assert "whatsapp" in ChannelsHandler.EN_FIRST_CHANNELS


def test_singleton_cache_clear_knows_whatsapp(monkeypatch):
    import app
    from channel.whatsapp.whatsapp_channel import WhatsAppChannel

    first = WhatsAppChannel()
    app._clear_singleton_cache("whatsapp")
    assert WhatsAppChannel() is not first


def test_tool_is_exported():
    import agent.tools as tools

    assert "WhatsAppGroupTool" in tools.__all__
    assert tools.WhatsAppGroupTool.name == "whatsapp_group"
