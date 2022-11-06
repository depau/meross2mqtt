import asyncio
from asyncio import Future
from datetime import datetime
from typing import TypeVar, Iterable, Callable, Awaitable, Any, Coroutine

import aiohttp
from aiohttp import ClientTimeout
from loguru import logger
from meross_iot.controller.device import BaseDevice

# noinspection PyProtectedMember
from meross_iot.controller.mixins.consumption import ConsumptionMixin, ConsumptionXMixin, _DATE_FORMAT
from meross_iot.controller.mixins.dnd import SystemDndMixin
from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.controller.mixins.toggle import ToggleMixin, ToggleXMixin
from meross_iot.model.enums import DNDMode, Namespace

from meross2homie.config import CONFIG
from meross2homie.homie import (
    HomieDevice,
    HomieNode,
    HomieBooleanProperty,
    HomieJsonProperty,
    HomieFloatProperty,
    HomieCommandProperty,
)
from meross2homie.meross import MerossMqttDeviceInfo, IMerossManager, meross_http_payload

MD = TypeVar("MD", bound=BaseDevice)
T = TypeVar("T")


def simple_setter(awaitable: Callable[[T], Coroutine[Any]]) -> Callable[[T], Awaitable[T]]:
    async def _setter(value: Any):
        future = Future()

        async def waiter():
            try:
                await awaitable(value)
            finally:
                future.set_result(None)

        asyncio.create_task(waiter())

        try:
            await asyncio.wait_for(future, timeout=0.5)
        except (TimeoutError, asyncio.TimeoutError):
            pass

        return value

    return _setter


def _channel_topic(topic: str, channel_id: int) -> str:
    return topic if channel_id == 0 else f"{topic}-{channel_id}"


def _channel_name(name: str, channel_id: int) -> str:
    return name if channel_id == 0 else f"{name} (channel {channel_id})"


class MerossHomieDevice(HomieDevice):
    def __init__(self, meross_device: MD, dev_info: MerossMqttDeviceInfo, manager: IMerossManager):
        super().__init__(dev_info.dev_name)
        self.dev_info = dev_info
        self.meross_device = meross_device
        self.manager = manager
        self._populate()

    async def update_device_info(self, dev_info: MerossMqttDeviceInfo):
        self.dev_info = dev_info
        await self.refresh()

    @property
    def channels(self) -> Iterable[int]:
        return [channel["channel_id"] for channel in self.dev_info.channels]

    def get_attributes(self) -> dict:
        res = super().get_attributes()
        # Implement the legacy firmware extension
        res.update(
            {
                "$localip": self.dev_info.ip_address,
                "$mac": self.dev_info.mac_address,
                "$fw/name": self.dev_info.firmware_name,
                "$fw/version": self.dev_info.fmware_version,
            }
        )
        return res

    def get_extensions(self) -> Iterable[str]:
        yield "org.homie.legacy-firmware:0.1.1:[4.x]"
        yield from super().get_extensions()

    async def poll(self):
        md = self.meross_device
        updates = []

        await md.async_update()

        if isinstance(md, ToggleMixin) or isinstance(md, ToggleXMixin):
            for channel_id in self.channels:
                updates.append(self[_channel_topic("switch", channel_id)]["power"].update_value(md.is_on(channel_id)))

        if isinstance(md, ConsumptionMixin) or isinstance(md, ConsumptionXMixin):
            for channel_id in self.channels:
                # Get consumption manually because the library unnecessarily mangles the timestamp
                result = await self.manager.rpc(
                    self.dev_info.uuid,
                    method="GET",
                    namespace=isinstance(md, ConsumptionMixin)
                    and Namespace.CONTROL_CONSUMPTION
                    or Namespace.CONTROL_CONSUMPTIONX,
                    payload={"channel": channel_id},
                )
                payload = result["payload"]
                data = payload.get("consumption", payload.get("consumptionx", []))

                # Parse the json data into nice-python native objects
                stats = [
                    {
                        "timestamp": datetime.fromtimestamp(data["timestamp"])
                        if "timestamp" in data
                        else datetime.strptime(x.get("date"), _DATE_FORMAT),
                        "total_consumption_kwh": float(x.get("value")) / 1000,
                    }
                    for x in data
                ]

                last = sorted(stats, key=lambda x: x["timestamp"], reverse=True)[0]
                total = sum(x["total_consumption_kwh"] for x in stats)
                updates.append(self[_channel_topic("energy", channel_id)]["history"].update_value(stats))
                updates.append(
                    self[_channel_topic("energy", channel_id)]["daily"].update_value(last["total_consumption_kwh"])
                )
                updates.append(self[_channel_topic("energy", channel_id)]["total"].update_value(total))

        if isinstance(md, ElectricityMixin):
            for channel_id in self.channels:
                stats = await md.async_get_instant_metrics(channel_id)
                updates.append(self[_channel_topic("electricity", channel_id)]["voltage"].update_value(stats.voltage))
                updates.append(self[_channel_topic("electricity", channel_id)]["current"].update_value(stats.current))
                updates.append(self[_channel_topic("electricity", channel_id)]["power"].update_value(stats.power))

        if isinstance(md, SystemDndMixin):
            updates.append(self["dnd"]["dnd"].update_value((await md.async_get_dnd_mode()) == DNDMode.DND_ENABLED))

        await asyncio.gather(*updates)

    async def on_message(self, message: dict):
        await self.meross_device.async_handle_push_notification(message["header"]["namespace"], message["payload"])

        md = self.meross_device

        namespace = Namespace(message["header"]["namespace"])

        if namespace in (Namespace.CONTROL_TOGGLE, Namespace.CONTROL_TOGGLEX):
            assert isinstance(md, ToggleMixin) or isinstance(md, ToggleXMixin)
            for channel_id in self.channels:
                await self[_channel_topic("switch", channel_id)]["power"].update_value(md.is_on(channel_id))
        else:
            logger.warning(f"Unhandled notification: {message}")

    async def reboot(self):
        logger.info(f"Attempting to reboot device {self.dev_info.uuid} ({self.name}) via HTTP")
        ip_addr = self.dev_info.ip_address
        if (dev_config := CONFIG.devices.get(self.dev_info.uuid)) and dev_config.meross_key:
            dev_key = dev_config.meross_key
        else:
            dev_key = CONFIG.meross_key
        try:
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=CONFIG.command_timeout)) as session:
                # The device will reboot if we send a request to an invalid namespace.
                async with session.post(
                    f"http://{ip_addr}/config",
                    json=meross_http_payload("GET", "Appliance.System.Cucumbers", {}, dev_key),
                ) as resp:
                    # The request WILL time out. But we still need to await it.
                    await resp.json()
                    raise RuntimeError("Device unexpectedly responded to reboot request")
        except (TimeoutError, asyncio.TimeoutError):
            logger.info(f"Reboot request for {self.dev_info.uuid} ({self.name}) sent")

    def _populate(self):
        md = self.meross_device

        logger.info(f"Device has the following channels: {self.channels}")

        node = HomieNode(
            self,
            topic="system",
            name="System",
            node_type="system",
        )

        async def _reboot(value):
            asyncio.create_task(self.reboot())
            return value

        HomieCommandProperty(
            node,
            topic="reboot",
            name="Reboot",
            setter=_reboot,
        )

        if isinstance(md, ToggleMixin) or isinstance(md, ToggleXMixin):
            logger.info("Device supports toggle ability")
            for channel_id in self.channels:
                node = HomieNode(
                    self,
                    topic=_channel_topic("switch", channel_id),
                    name=_channel_name("Switch", channel_id),
                    node_type="switch",
                )
                HomieBooleanProperty(
                    node,
                    topic="power",
                    name="Power",
                    setter=simple_setter(
                        lambda value: md.async_turn_on(channel_id) if value else md.async_turn_off(channel_id)
                    ),
                )
        if isinstance(md, ConsumptionMixin) or isinstance(md, ConsumptionXMixin):
            logger.info("Device supports consumption ability")
            for channel_id in self.channels:
                node = HomieNode(
                    self,
                    topic=_channel_topic("energy", channel_id),
                    name=_channel_name("Energy", channel_id),
                    node_type="stats",
                )
                HomieJsonProperty(
                    node,
                    topic="history",
                    name="History",
                )
                HomieFloatProperty(
                    node,
                    topic="daily",
                    name="Daily consumption",
                    unit="kWh",
                )
                HomieFloatProperty(
                    node,
                    topic="total",
                    name="Total consumption",
                    unit="kWh",
                )

        if isinstance(md, ElectricityMixin):
            logger.info("Device supports electricity ability")
            for channel_id in self.channels:
                node = HomieNode(
                    self,
                    topic=_channel_topic("electricity", channel_id),
                    name=_channel_name("Electricity", channel_id),
                    node_type="stats",
                )
                HomieFloatProperty(
                    node,
                    topic="voltage",
                    name="Voltage",
                    unit="V",
                )
                HomieFloatProperty(
                    node,
                    topic="current",
                    name="Current",
                    unit="A",
                )
                HomieFloatProperty(
                    node,
                    topic="power",
                    name="Power usage",
                    unit="W",
                )

        # system dungeons and dragons
        if isinstance(md, SystemDndMixin):
            logger.info("Device supports DND ability")
            node = HomieNode(
                self,
                topic="dnd",
                name="Do Not Disturb",
                node_type="settings",
            )
            HomieBooleanProperty(
                node,
                topic="dnd",
                name="Do Not Disturb",
                setter=simple_setter(
                    lambda value: md.set_dnd_mode(value and DNDMode.DND_ENABLED or DNDMode.DND_DISABLED)
                ),
            )
