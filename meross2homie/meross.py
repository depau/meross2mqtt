import json
import random
import time
from hashlib import md5
from typing import Protocol, Optional, Iterable, Union, List, TypeVar, Tuple

from meross_iot.manager import TransportMode
from meross_iot.model.enums import OnlineStatus, Namespace
from meross_iot.model.http.device import HttpDeviceInfo

from meross2homie.config import CONFIG

T = TypeVar("T")


# Adapted from
# https://github.com/albertogeniola/ha-meross-local-broker/blob/364f7abbe17625cdb342363b14772eec68a66785/meross_local_broker/rootfs/opt/custom_broker/protocol.py
def _gen_boilerplate(dev_key: str) -> Tuple[int, str, str]:
    md5_hash = md5()
    md5_hash.update(str(random.randint(0xFFFFFFFF, 0xFFFFFFFFF)).encode("utf-8"))
    message_id = md5_hash.hexdigest().lower()
    timestamp = int(round(time.time()))

    md5_hash = md5()
    to_be_hashed = f"{message_id}{dev_key}{timestamp}"
    md5_hash.update(to_be_hashed.encode("utf8"))
    signature = md5_hash.hexdigest().lower()

    return timestamp, message_id, signature


def meross_mqtt_payload(
    method: str,
    namespace: str,
    payload: dict,
    dev_key: str,
    header_from: str = "/appliance/m2h-bridge/subscribe",
) -> Tuple[bytes, str]:
    timestamp, message_id, signature = _gen_boilerplate(dev_key)
    data = {
        "header": {
            "from": header_from,
            "messageId": message_id,  # Example: "122e3e47835fefcd8aaf22d13ce21859"
            "method": method,  # Example: "GET",
            "namespace": namespace,  # Example: "Appliance.System.All",
            "payloadVersion": 1,
            "sign": signature,  # Example: "b4236ac6fb399e70c3d61e98fcb68b74",
            "timestamp": timestamp,
            "triggerSrc": "Agent",
        },
        "payload": payload,
    }
    return json.dumps(data).encode("utf-8"), message_id


def meross_http_payload(
    method: str,
    namespace: str,
    payload: dict,
    dev_key: str,
) -> dict:
    timestamp, message_id, signature = _gen_boilerplate(dev_key)
    data = {
        "header": {
            "messageId": message_id,  # Example: "122e3e47835fefcd8aaf22d13ce21859"
            "method": method,  # Example: "GET",
            "namespace": namespace,  # Example: "Appliance.System.All",
            "payloadVersion": 1,
            "sign": signature,  # Example: "b4236ac6fb399e70c3d61e98fcb68b74",
            "timestamp": timestamp,
        },
        "payload": payload,
    }
    return data


def is_uuid(uuid: str) -> bool:
    return len(uuid) == 32 and all(c in "0123456789abcdef" for c in uuid)


class IMerossManager(Protocol):
    """Manager interface that code in meross_iot expects."""

    async def async_execute_cmd(
        self,
        mqtt_hostname: str,
        mqtt_port: int,
        destination_device_uuid: str,
        method: str,
        namespace: Namespace,
        payload: dict,
        timeout: float = None,
        override_transport_mode: TransportMode = None,
        *a,
        **kw,
    ):
        """
        This method sends a command to the device, locally via HTTP or via the MQTT Meross broker.
        :param mqtt_hostname: the mqtt broker hostname
        :param mqtt_port: the mqtt broker port
        :param destination_device_uuid:
        :param method: Can be GET/SET
        :param namespace: Command namespace
        :param payload: A dict containing the payload to be sent
        :param timeout: Maximum time interval in seconds to wait for the command-answer
        :param override_transport_mode: when set, overrides the manager transport mode
        :return:
        """
        ...

    def find_devices(
        self,
        device_uuids: Optional[Iterable[str]] = None,
        internal_ids: Optional[Iterable[str]] = None,
        device_type: Optional[str] = None,
        device_class: Optional[Union[type, Iterable[type]]] = None,
        device_name: Optional[str] = None,
        online_status: Optional[OnlineStatus] = None,
    ) -> List[T]:
        """
        Lists devices that have been discovered via this manager. When invoked with no arguments,
        it returns the whole list of registered devices. When one or more filter arguments are specified,
        it returns the list of devices that satisfy all the filters (consider multiple filters as in logical AND).
        :param device_uuids: List of Meross native device UUIDs. When specified, only devices that have a native UUID
            contained in this list are returned.
        :param internal_ids: Iterable List of MerossIot device ids. When specified, only devices that have a
            derived-ids contained in this list are returned.
        :param device_type: Device type string as reported by meross app (e.g. "mss310" or "msl120"). Note that this
            field is case-sensitive.
        :param device_class: Filter based on the resulting device class or list of classes. When this parameter is
            a list of types, the filter returns al the devices that matches at least one of the types in the list
            (logic OR). You can filter also for capability Mixins, such as
            :code:`meross_iot.controller.mixins.toggle.ToggleXMixin` (returns all the devices supporting ToggleX
            capability) or :code:`meross_iot.controller.mixins.light.LightMixin`
            (returns all the device that supports light control). Similarly, you can identify all the HUB devices
            by specifying :code:`meross_iot.controller.device.HubDevice`, Sensors as
            :code:`meross_iot.controller.subdevice.Ms100Sensor` and Valves as
            :code:`meross_iot.controller.subdevice.Mts100v3Valve`.
        :param device_name: Filter the devices based on their assigned name (case sensitive)
        :param online_status: Filter the devices based on their :code:`meross_iot.model.enums.OnlineStatus`
            as reported by the HTTP api or byt the relative hub (when dealing with subdevices).
        :return:
            The list of devices that match the provided filters, if any.
        """
        ...

    async def rpc(
        self,
        uuid: str,
        method: str,
        namespace: Namespace,
        payload: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        ...


class MerossMqttDeviceInfo(HttpDeviceInfo):
    ip_address: str
    mac_address: str
    firmware_name: str

    def __init__(self, *args, ip_address: str, mac_address: str, firmware_name: str, **kwargs):
        super().__init__(
            *args,
            **kwargs,
        )
        self.ip_address = ip_address
        self.mac_address = mac_address
        self.firmware_name = firmware_name

    @classmethod
    def from_system_all_payload(cls, payload: dict):
        data = payload["all"]["system"]
        digest = payload["all"].get("digest", {})

        uuid = data["hardware"]["uuid"]
        if (dev := CONFIG.devices.get(uuid)) and dev.pretty_name:
            dev_name = dev.pretty_name
        else:
            dev_name = f'Meross {data["hardware"]["type"].upper()} ({data["hardware"]["macAddress"]})'

        # Guess channels
        # Adapted from:
        # https://github.com/albertogeniola/ha-meross-local-broker/blob/364f7abbe17625cdb342363b14772eec68a66785/meross_local_broker/rootfs/opt/custom_broker/broker_agent.py#L235-L240
        channels = [
            {
                "device_uuid": data["hardware"]["uuid"],
                "channel_id": i["channel"],
            }
            for i in digest.get("togglex", digest.get("toggle", []))
        ]

        fw_name = f'{data["hardware"]["type"]}-{data["hardware"]["chipType"]}'

        return cls(
            uuid=uuid,
            online_status=OnlineStatus.ONLINE,
            dev_name=dev_name,
            dev_icon_id="",
            bind_time=0,
            device_type=data["hardware"]["type"],
            sub_type=data["hardware"]["subType"],
            channels=channels,
            region="not-being-tracked-land",
            fmware_version=data["firmware"]["version"],
            hdware_version=data["hardware"]["version"],
            user_dev_icon="",
            icon_type=0,
            skill_number="",
            domain="yolo:1234",
            reserved_domain="yolo:1234",
            ip_address=data["firmware"]["innerIp"],
            mac_address=data["hardware"]["macAddress"],
            firmware_name=fw_name,
        )
