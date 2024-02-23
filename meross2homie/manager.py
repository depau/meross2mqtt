import asyncio
import json
import random
from asyncio import Future
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional, Iterable, Union, List, cast, Dict, TypeVar

import aiohttp
import aiomqtt
from aiohttp import ClientTimeout, ClientConnectorError
from loguru import logger
from meross_iot.device_factory import build_meross_device_from_abilities
from meross_iot.manager import TransportMode, DeviceRegistry
from meross_iot.model.enums import OnlineStatus, Namespace
from meross_iot.model.exception import CommandTimeoutError
from paho.mqtt.client import MQTTMessage

from meross2homie.config import CONFIG
from meross2homie.device import MerossHomieDevice
from meross2homie.homie import Homie, HomieState
from meross2homie.meross import (
    IMerossManager,
    meross_http_payload,
    meross_mqtt_payload,
    MerossMqttDeviceInfo,
    is_uuid,
    reboot_device,
)
from meross2homie.persistence import Persistence, DeviceProps

T = TypeVar("T")


def mqtt_factory(will: Optional[aiomqtt.Will] = None, client_id_prefix: Optional[str] = None) -> aiomqtt.Client:
    if not client_id_prefix:
        client_id_prefix = f"meross2homie"
    client_id = f"{client_id_prefix}_{random.randint(0, 1000000)}"

    client = aiomqtt.Client(
        hostname=CONFIG.mqtt_host,
        port=CONFIG.mqtt_port,
        username=CONFIG.mqtt_username,
        password=CONFIG.mqtt_password,
        identifier=client_id,
        will=will,
        clean_session=CONFIG.mqtt_clean_session,
    )
    # Yes Karen, you can send all of them in no time
    client.pending_calls_threshold = 200

    return client


@dataclass
class StagingDevice:
    uuid: str
    dev_info: Optional[MerossMqttDeviceInfo] = None
    abilities: dict = field(default_factory=dict)

    @property
    def is_fully_discovered(self) -> bool:
        return bool(self.dev_info and self.abilities)


class BridgeManager(IMerossManager):
    def __init__(self):
        self.mqtt = mqtt_factory(client_id_prefix="m2h.manager")
        self.homie = Homie(mqtt_factory, CONFIG.homie_prefix)

        self.device_registry = DeviceRegistry()
        self.homie_devices: Dict[str, MerossHomieDevice] = {}
        self.persistence = Persistence.load(CONFIG.persistence_file)
        self.pending_commands: Dict[str, Future[dict]] = {}
        self.timed_out_commands_count: Dict[str, int] = {}

        self.ctx_manager: Optional[AsyncExitStack] = None

    def _debug_dev_name(self, uuid: str) -> str:
        if uuid in self.homie_devices:
            return f"{self.homie_devices[uuid].dev_info.dev_name} ({uuid})"
        else:
            return uuid

    def _load_persisted_devices(self):
        for uuid in self.persistence.devices:
            logger.debug(f"Interviewing remembered device {self._debug_dev_name(uuid)}")
            asyncio.create_task(self._interview(uuid))

    async def _receive_messages(self):
        async for message in self.mqtt.messages:
            message = cast(MQTTMessage, message)
            # We must not block the parent coroutine, or else we won't be able to receive responses to RPC commands
            asyncio.create_task(self._handle_message(str(message.topic), json.loads(message.payload.decode())))

    async def _poll(self):
        while True:
            # noinspection PyBroadException
            try:
                await asyncio.gather(
                    *tuple(
                        map(
                            lambda x: asyncio.wait_for(x, CONFIG.command_timeout * 2),
                            (i.poll() for i in self.homie_devices.values()),
                        )
                    )
                )
            except (CommandTimeoutError, TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                logger.error("Command timed out")
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                logger.exception("Unhandled exception while polling")
            await asyncio.sleep(CONFIG.polling_interval)

    async def process_events(self):
        logger.info("Processing events")
        await asyncio.gather(self._receive_messages(), self._poll(), self.homie.process_messages())

    async def _interview(self, uuid: str, retries_left: Optional[int] = None):
        if retries_left is None:
            retries_left = CONFIG.interview_retry_times

        try:
            system_all = await self.rpc(uuid, "GET", Namespace.SYSTEM_ALL, timeout=CONFIG.interview_command_timeout)
            dev_info = MerossMqttDeviceInfo.from_system_all_payload(system_all["payload"])
            logger.info(f"Discovered device {dev_info.device_type} {dev_info.dev_name} ({dev_info.uuid})")

            # If we already have the device, just update the info
            if self.homie_devices.get(uuid):
                await self.homie_devices[uuid].update_device_info(dev_info)
                return

            abilities = (
                await self.rpc(uuid, "GET", Namespace.SYSTEM_ABILITY, timeout=CONFIG.interview_command_timeout)
            )["payload"]["ability"]

            meross_device = build_meross_device_from_abilities(dev_info, abilities, self)
            homie_device = MerossHomieDevice(meross_device, dev_info, self)

            if (dev := CONFIG.devices.get(uuid)) and dev.pretty_topic:
                topic = dev.pretty_topic
            else:
                topic = uuid

            self.persistence.devices[uuid] = DeviceProps(ip_address=dev_info.ip_address)
            self.persistence.persist(CONFIG.persistence_file)

            logger.debug(f"Registering device {self._debug_dev_name(uuid)} as {topic}")

            self.device_registry.enroll_device(meross_device)
            await self.homie.add_device(homie_device, topic)
            self.homie_devices[uuid] = homie_device

            await homie_device.set_state(HomieState.READY)
            logger.debug(f"Device {self._debug_dev_name(uuid)} ready")

        except CommandTimeoutError as e:
            if retries_left == 1 and uuid in self.persistence.devices and self.persistence.devices[uuid].ip_address:
                logger.warning(f"Before last interview attempt, try rebooting the device {self._debug_dev_name(uuid)}")
                await reboot_device(uuid, self.persistence.devices[uuid].ip_address)

            if retries_left > 0:
                delay = random.uniform(*CONFIG.interview_retry_delay_range)
                logger.error(
                    f"Command timed out while interviewing device {self._debug_dev_name(uuid)}, retrying in {round(delay, 2)} seconds"
                )
                await asyncio.sleep(delay)
                await self._interview(uuid, retries_left - 1)
            else:
                logger.error(
                    f"Command timed out while interviewing device {self._debug_dev_name(uuid)}, giving up: {e.message}"
                )

    async def _handle_message(self, topic: str, message: dict):
        # noinspection PyBroadException
        try:
            if not topic.startswith(CONFIG.meross_prefix):
                return

            header = message["header"]
            payload = message["payload"]
            message_id = header["messageId"]

            if message_id in self.pending_commands:
                logger.trace(f"Received response for message {message_id}")
                self.pending_commands.pop(message_id).set_result(message)
                return

            try:
                namespace = Namespace(header["namespace"])
            except ValueError:
                logger.warning(f"Unknown namespace: {header['namespace']}, {message}")
                return
            method = header["method"]

            if namespace == Namespace.SYSTEM_ALL:
                uuid = payload["all"]["system"]["hardware"]["uuid"]
            else:
                if "from" in header:
                    sent_prefix = CONFIG.meross_sent_prefix or CONFIG.meross_prefix
                    maybe_uuid = header["from"][len(sent_prefix) + 1 :].split("/")[0]
                else:
                    maybe_uuid = topic[len(CONFIG.meross_prefix) + 1 :].split("/")[0]

                if is_uuid(maybe_uuid):
                    uuid = maybe_uuid
                else:
                    logger.warning(
                        f"UUID component in 'from' header {header['from']} does not look like a UUID. Ignoring message."
                    )
                    return

            await self._on_device_responded(uuid)

            # Acknowledge bind request
            if namespace == Namespace.CONTROL_BIND and method == "SET":
                logger.info(f"Received bind request for {self._debug_dev_name(uuid)}. Acknowledging.")
                await self.rpc(uuid, "SETACK", Namespace.CONTROL_BIND)
                await self._interview(uuid)
                return

            # Interview new device
            if uuid not in self.homie_devices:
                logger.info(f"Received message from unknown device {self._debug_dev_name(uuid)}. Starting interview.")
                await self._interview(uuid)
                if uuid not in self.homie_devices:
                    raise RuntimeError("Device interview mysteriously passed without adding a device")

            logger.debug(f"Received message from {self._debug_dev_name(uuid)}: {message}")
            await self.homie_devices[uuid].on_message(message)
        except CommandTimeoutError:
            logger.error("Command timed out")
        except Exception:
            logger.exception(f"Unhandled exception while handling message: {message}")

    async def __aenter__(self):
        self.ctx_manager = AsyncExitStack()
        await self.ctx_manager.__aenter__()
        await self.ctx_manager.enter_async_context(self.mqtt)
        await self.ctx_manager.enter_async_context(self.homie)
        await self.mqtt.subscribe([(i, 1) for i in self.subscriptions])
        self._load_persisted_devices()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.mqtt.unsubscribe(self.subscriptions)
        await self.ctx_manager.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def subscriptions(self) -> List[str]:
        return [
            f"{CONFIG.meross_prefix}/+/publish",
            f"{CONFIG.meross_prefix}/m2h-{CONFIG.meross_bridge_topic}/subscribe",
        ]

    async def _on_device_timeout(self, uuid: str):
        self.timed_out_commands_count[uuid] = self.timed_out_commands_count.get(uuid, 0) + 1
        if self.timed_out_commands_count[uuid] >= CONFIG.timed_out_commands_threshold:
            logger.warning(
                f"Device {self._debug_dev_name(uuid)} has timed out {self.timed_out_commands_count[uuid]} times. Reporting as offline."
            )
            homie_device = self.homie_devices.get(uuid)
            if homie_device:
                await homie_device.set_state(HomieState.LOST)
        # Perform one reboot attempt
        if (
            self.timed_out_commands_count[uuid] % CONFIG.timed_out_commands_threshold == 0
        ) and CONFIG.try_reboot_on_timeout:
            if hd := self.homie_devices.get(uuid):
                asyncio.create_task(hd.reboot())

    async def _on_device_responded(self, uuid: str):
        if self.timed_out_commands_count.get(uuid, -1) != 0:
            logger.info(f"Device {self._debug_dev_name(uuid)} has responded after being offline")
            self.timed_out_commands_count[uuid] = 0
            homie_device = self.homie_devices.get(uuid)
            if homie_device:
                await homie_device.set_state(HomieState.READY)

    async def rpc(
        self,
        uuid: str,
        method: str,
        namespace: Namespace,
        payload: Optional[dict] = None,
        timeout: Optional[float] = None,
        override_transport_mode: TransportMode = None,
    ) -> dict:
        if override_transport_mode is None:
            override_transport_mode = TransportMode.LAN_HTTP_FIRST

        if timeout is None:
            timeout = CONFIG.command_timeout

        if payload is None:
            payload = {}

        sent_prefix = CONFIG.meross_sent_prefix or CONFIG.meross_prefix

        if (dev_config := CONFIG.devices.get(uuid)) and dev_config.meross_key:
            dev_key = dev_config.meross_key
        else:
            dev_key = CONFIG.meross_key

        if (
            CONFIG.enable_http
            and override_transport_mode != TransportMode.MQTT_ONLY
            and (
                (override_transport_mode == TransportMode.LAN_HTTP_FIRST_ONLY_GET and method == "GET")
                or override_transport_mode == TransportMode.LAN_HTTP_FIRST
            )
            and (
                (
                    uuid in self.homie_devices
                    and self.homie_devices[uuid].dev_info
                    and (ip_address := self.homie_devices[uuid].dev_info.ip_address)
                )
                or (uuid in self.persistence.devices and (ip_address := self.persistence.devices[uuid].ip_address))
            )
        ):
            try:
                return await self.rpc_http(uuid, ip_address, method, namespace, payload, dev_key, timeout)
            except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientConnectorError):
                # Try again with MQTT
                pass

        return await self.rpc_mqtt(
            uuid,
            method,
            namespace,
            payload,
            dev_key,
            f"{sent_prefix}/m2h-{CONFIG.meross_bridge_topic}/subscribe",
            timeout,
        )

    async def rpc_mqtt(
        self,
        uuid: str,
        method: str,
        namespace: Namespace,
        payload: dict,
        dev_key: str,
        header_from: str,
        timeout: float,
    ) -> dict:
        message, message_id = meross_mqtt_payload(
            method=method,
            namespace=cast(str, namespace.value),
            payload=payload,
            dev_key=dev_key,
            header_from=header_from,
        )

        future: Future[dict] = asyncio.Future()
        self.pending_commands[message_id] = future

        logger.trace(f"Sending message to {uuid} via MQTT: {message.decode()}")
        await self.mqtt.publish(f"{CONFIG.meross_prefix}/{uuid}/subscribe", message, qos=1)

        try:
            res = await asyncio.wait_for(future, timeout)
            hd = self.homie_devices.get(uuid)
            if hd is not None:
                asyncio.create_task(hd.set_state(HomieState.READY))
            return res
        except (asyncio.TimeoutError, TimeoutError) as e:
            if task := self.pending_commands.pop(message_id):
                task.cancel()
            logger.error(f"Command {method} {namespace.value} to device {self._debug_dev_name(uuid)} timed out.")
            await self._on_device_timeout(uuid)
            raise CommandTimeoutError(message.decode("utf-8"), uuid, timeout) from e
        except asyncio.CancelledError as e:
            if task := self.pending_commands.pop(message_id):
                task.cancel()
            logger.warning(f"Command {method} {namespace.value} to device {self._debug_dev_name(uuid)} was cancelled.")
            raise CommandTimeoutError(message.decode("utf-8"), uuid, timeout) from e

    async def rpc_http(
        self, uuid: str, ip_address: str, method: str, namespace: Namespace, payload: dict, dev_key: str, timeout: float
    ) -> dict:

        message = meross_http_payload(
            method=method,
            namespace=cast(str, namespace.value),
            payload=payload,
            dev_key=dev_key,
        )

        logger.trace(f"Sending message to {self._debug_dev_name(uuid)} via HTTP: {message}")
        try:
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=timeout)) as session:
                # The device will reboot if we send a request to an invalid namespace.
                async with session.post(
                    f"http://{ip_address}/config",
                    json=message,
                ) as resp:
                    j = await resp.json()
                    await self._on_device_responded(uuid)
                    return j
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(f"HTTP request timed out")
            raise
        except ClientConnectorError as e:
            logger.warning(f"Device {self._debug_dev_name(uuid)} is not reachable at {ip_address}: {e}")
            raise

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
    ) -> dict:
        """Used by meross_iot module"""
        return (await self.rpc(destination_device_uuid, method, namespace, payload, timeout, override_transport_mode))[
            "payload"
        ]

    def find_devices(
        self,
        device_uuids: Optional[Iterable[str]] = None,
        internal_ids: Optional[Iterable[str]] = None,
        device_type: Optional[str] = None,
        device_class: Optional[Union[type, Iterable[type]]] = None,
        device_name: Optional[str] = None,
        online_status: Optional[OnlineStatus] = None,
    ) -> List[T]:
        return super().find_devices(
            device_uuids,
            internal_ids,
            device_type,
            device_class,
            device_name,
            online_status,
        )
