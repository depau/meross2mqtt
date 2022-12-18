from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Tuple

from dataclasses_json import DataClassJsonMixin, config
from marshmallow import fields
from yamldataclassconfig import YamlDataClassConfig

from meross2homie.homie import validate_homie_identifier


def _validate_topic(topic: Optional[str]) -> Optional[str]:
    if topic is None:
        return None
    return validate_homie_identifier(topic)


@dataclass
class DeviceConfig(DataClassJsonMixin):
    pretty_name: Optional[str] = None
    pretty_topic: Optional[str] = field(metadata=config(decoder=_validate_topic, encoder=str), default=None)
    meross_key: Optional[str] = None


@dataclass
class Config(YamlDataClassConfig):
    # MQTT broker standard parameters
    mqtt_host: str = None  # type: ignore
    mqtt_port: int = 1883
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_clean_session: bool = False

    # Meross MQTT protocol
    enable_http: bool = True
    """Enable direct HTTP connection to Meross devices. This is usually more stable than MQTT, so it is advised to have
    a direct connection between the devices and this bridge in order for HTTP communication to work. The IP address of
    the device will be auto-discovered via MQTT."""

    meross_key: str = ""
    """Meross protocol key. Defaults to an empty string. It can be overridden for each device by the `meross_key` 
    field in the device config."""

    meross_prefix: str = "/appliance"
    """MQTT prefix of Meross devices in the configured MQTT broker. Default is "/appliance"."""

    meross_sent_prefix: Optional[str] = None
    """MQTT prefix that Meross devices see as their own prefix in their own MQTT broker. Useful when the broker is
    configured to map the prefix to another one. Defaults to the same as meross_prefix if None."""

    meross_bridge_topic: str = field(metadata=config(decoder=_validate_topic, encoder=str), default="bridge")
    """MQTT topic that the bridge uses to communicate with the Meross devices. It will be prefixed with 'm2h-'. 
    Default is 'bridge'."""

    # Homie MQTT protocol
    homie_prefix: str = field(metadata=config(decoder=_validate_topic, encoder=str), default="homie")
    """MQTT prefix of Homie devices in the configured MQTT broker. Default is the standard 'homie'. If you change it, 
    make sure your controller software can handle it. openHAB is compliant and handles the convention correctly."""

    # Bridge
    persistence_file: Path = field(
        metadata=config(decoder=Path, encoder=str, mm_field=fields.Str()), default=Path("devices.json")
    )
    """Path to the file where the bridge will persist the discovered devices. Defaults to 'devices.json' in the 
    current directory."""

    devices: Dict[str, DeviceConfig] = field(default_factory=dict)
    """Mapping from device UUIDs to
    {pretty_name: "Homie Device Name", pretty_topic: "homie-device-id", meross_key: "overridden dev key"}"""

    polling_interval: Optional[int] = 10
    """Interval in seconds between each polling of the devices, or None to disable polling. Default: 10 seconds."""

    command_timeout: int = 2
    """Timeout in seconds for commands sent to Meross devices. Default is 2 seconds."""

    interview_command_timeout: int = 10
    """Timeout in seconds for commands sent to Meross devices for the initial interview. Default is 10 seconds."""

    timed_out_commands_threshold: int = 5
    """Number of failed commands before the device is considered offline. Default: 5 in a row."""

    interview_retry_times: int = 3
    """Number of times to retry the interview in case of timeout. Default is 3 times."""

    interview_retry_delay_range: Tuple[float, float] = field(
        default=(3, 5), metadata=config(mm_field=fields.Tuple((fields.Float(), fields.Float())))
    )
    """Range of seconds to wait before retrying the interview. Default is 3 to 5 seconds."""

    try_reboot_on_timeout: bool = False
    """If True, the bridge will attempt to reboot the device if it times out. This is a bit hacky and it requires 
    direct connectivity between the bridge and the device. Default: False. """


CONFIG = Config()
