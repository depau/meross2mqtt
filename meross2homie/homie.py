import asyncio
import json
import time
from abc import ABC
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Dict,
    Optional,
    Generic,
    TypeVar,
    Awaitable,
    Type,
    List,
    Iterable,
    ParamSpec,
    cast,
    Union,
    Any,
)

import asyncio_mqtt
import isodate as isodate
from loguru import logger
from paho.mqtt.client import MQTTMessage

T = TypeVar("T")


def is_valid_homie_identifier(topic: str) -> bool:
    return (
        all((c.isalnum() and c.islower()) or c in "-" for c in topic)
        and not topic.startswith("-")
        and not topic.endswith("-")
    )


def validate_homie_identifier(topic: str) -> str:
    if not is_valid_homie_identifier(topic):
        raise ValueError(
            f"Invalid Homie identifier: {topic}. It must be a lower-case alphanumeric string, optionally containing "
            f"dashes, not starting or ending with a dash. "
        )
    return topic


def fallback_json_serializer(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return isodate.duration_isoformat(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def fallback_json_deserializer(s: str) -> Any:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    try:
        return isodate.parse_duration(s)
    except isodate.ISO8601Error:
        pass
    raise TypeError(f"Object of type {s.__class__.__name__} is not JSON deserializable")


class HomieState(Enum):
    INIT = "init"
    READY = "ready"
    LOST = "lost"
    DISCONNECTED = "disconnected"
    SLEEPING = "sleeping"
    ALERT = "alert"


class HomieError(RuntimeError):
    pass


HT = TypeVar("HT", bound="HomieTopologyMixin")


class HomieTopologyMixin(Generic[HT], ABC):
    attributes_cache: Dict[str, str]
    attributes_last_full_update: float
    retained: bool = False
    children: Dict[str, HT] = {}

    def __init__(self, *args, **kwargs):
        super(HomieTopologyMixin, self).__init__(*args, **kwargs)
        self.attributes_cache = {}
        self.attributes_last_full_update = 0

    def get_attributes(self) -> Dict[str, str]:
        raise NotImplementedError

    # noinspection PyMethodMayBeStatic
    def get_value(self) -> Optional[str]:
        return None

    async def post_value(self, topic_suffix: str, value: str, retain: bool = False):
        raise NotImplementedError

    async def refresh(self):
        updates = []
        if self.get_value() is not None:
            updates.append(self.post_value("", self.get_value(), self.retained))

        # Force a full update every minute
        full_update = time.time() - self.attributes_last_full_update > 60
        if full_update:
            self.attributes_last_full_update = time.time()

        attributes = self.get_attributes()
        for topic, value in attributes.items():
            if full_update or topic not in self.attributes_cache or self.attributes_cache[topic] != value:
                # noinspection PyShadowingNames
                async def post_attr(topic, value):
                    await self.post_value(topic, value, True)
                    self.attributes_cache[topic] = value

                updates.append(post_attr(topic, value))

        for child in self.children.values():
            updates.append(child.refresh())

        await asyncio.gather(*updates)

    async def teardown(self):
        for child in self.children.values():
            await child.teardown()


S = TypeVar("S", bound="SubscriptionsMixin")


class SubscriptionsMixin(Generic[S], ABC):
    topic: str
    children: Dict[str, S] = {}

    def get_subscriptions(self) -> Iterable[str]:
        for child in self.children.values():
            for sub in child.get_subscriptions():
                yield f"{self.topic}/{sub}"


class GetChildrenMixin(Generic[T], ABC):
    children: Dict[str, T] = {}

    def __getitem__(self, item: str) -> T:
        return self.children[item]


class StateMixin(ABC):
    @property
    def state(self) -> HomieState:
        raise NotImplementedError


HD = TypeVar("HD", bound="HomieDevice")
P = ParamSpec("P")


class Homie(GetChildrenMixin["HomieDevice"]):
    def __init__(
        self,
        async_mqtt_client_factory: Callable[[asyncio_mqtt.Will, str], asyncio_mqtt.Client],
        root_topic: str = "homie",
    ):
        super(Homie, self).__init__()
        self.topic: str = root_topic
        self.async_mqtt_client_factory = async_mqtt_client_factory
        self.children: Dict[str, "HomieDevice"] = {}
        self.ctx_manager: Optional[AsyncExitStack] = None

    async def add_device(self, device: HD, topic: str):
        logger.info(f"Starting device '{topic}'")
        dev_mqtt = self.async_mqtt_client_factory(
            asyncio_mqtt.Will(f"{self.topic}/{topic}/$state", "lost", True),
            f"m2h.homie_dev.{topic}",
        )
        device.set_mqtt(dev_mqtt, f"{self.topic}/{topic}")
        self.children[device.name] = device
        if self.ctx_manager is not None:
            await self.ctx_manager.enter_async_context(device)
            asyncio.create_task(device.process_messages())

    async def __aenter__(self):
        self.ctx_manager = AsyncExitStack()
        await self.ctx_manager.__aenter__()
        for device in self.children.values():
            await self.ctx_manager.enter_async_context(device)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.ctx_manager.__aexit__(exc_type, exc_val, exc_tb)
        self.ctx_manager = None

    async def process_messages(self):
        await asyncio.gather(*(device.process_messages() for device in self.children.values()))


class HomieDevice(
    HomieTopologyMixin["HomieNode"], SubscriptionsMixin["HomieNode"], StateMixin, GetChildrenMixin["HomieNode"]
):
    topic: str
    mqtt: asyncio_mqtt.Client

    def __init__(self, name: str):
        super(HomieDevice, self).__init__()
        self.name = name
        self.children: Dict[str, HomieNode] = {}
        self._state = HomieState.INIT

    def set_mqtt(self, mqtt: asyncio_mqtt.Client, topic: str):
        self.mqtt = mqtt
        self.topic = topic

    async def __aenter__(self):
        await self.mqtt.__aenter__()
        logger.debug(f"Device {self.name} subscribing to {list(self.get_subscriptions())}")
        await self.mqtt.subscribe([(i, 1) for i in self.get_subscriptions()])
        await self.refresh()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.teardown()
        await self.mqtt.__aexit__(exc_type, exc_val, exc_tb)

    def add_node(self, node: "HomieNode"):
        if self.state != HomieState.INIT:
            raise RuntimeError("Can't add nodes while device is ready")
        self.children[node.topic] = node

    async def set_state(self, state: HomieState):
        self._state = state
        await self.refresh()

    @property
    def state(self) -> HomieState:
        return self._state

    async def teardown(self):
        await self.set_state(HomieState.DISCONNECTED)
        await self.mqtt.unsubscribe([i for i in self.get_subscriptions()])
        await super().teardown()
        self.children.clear()

    async def process_messages(self):
        logger.info(f"Device {self.name} starting message processing")
        async with self.mqtt.unfiltered_messages() as messages:
            async for message in messages:
                # noinspection PyBroadException
                try:
                    message = cast(MQTTMessage, message)
                    topic = message.topic

                    logger.debug(f"Got Homie message on {topic}: {message.payload}")

                    if not topic.startswith(self.topic):
                        logger.warning(f"Got message on {topic} but expected {self.topic}")
                        continue

                    payload = message.payload.decode("utf-8")

                    sub_topic = topic[len(self.topic) + 1 :]
                    for node in self.children.values():
                        if sub_topic.startswith(node.topic):
                            asyncio.create_task(node.process_message(sub_topic, payload))
                except (KeyboardInterrupt, EOFError):
                    raise
                except Exception:
                    logger.exception("Error processing Homie message")
                    raise

    def get_attributes(self) -> dict:
        return {
            "$homie": "4.0.0",
            "$name": self.name,
            "$state": self.state.value,
            "$nodes": ",".join(self.children.keys()),
            "$implementation": "meross2homie",
            "$extensions": ",".join(self.get_extensions()),
        }

    # noinspection PyMethodMayBeStatic
    def get_extensions(self) -> Iterable[str]:
        return []

    async def post_value(self, topic_suffix: str, value: str, retain: bool = False):
        if topic_suffix:
            topic = f"{self.topic}/{topic_suffix}"
        else:
            topic = self.topic
        await self.mqtt.publish(topic, value, qos=1, retain=retain)


class HomieNode(
    HomieTopologyMixin["HomieProperty"],
    SubscriptionsMixin["HomieProperty"],
    StateMixin,
    GetChildrenMixin["HomieProperty"],
):
    def __init__(
        self,
        device: HomieDevice,
        topic: str,
        name: str,
        node_type: str,
    ):
        super(HomieNode, self).__init__()
        self.device = device
        self.topic = topic
        self.name = name
        self.node_type = node_type
        self.children: Dict[str, "HomieProperty"] = {}
        device.add_node(self)

    def add_property(self, prop: "HomieProperty"):
        if self.state != HomieState.INIT:
            raise HomieError("Cannot add properties while device is ready")
        self.children[prop.topic] = prop

    def get_attributes(self) -> Dict[str, str]:
        return {
            "$name": self.name,
            "$type": self.node_type,
            "$properties": ",".join(self.children.keys()),
        }

    @property
    def state(self) -> HomieState:
        return self.device.state

    async def teardown(self):
        await super().teardown()
        self.children.clear()

    async def process_message(self, topic: str, payload: str):
        sub_topic = topic[len(self.topic) + 1 :]
        for prop in self.children.values():
            if sub_topic.startswith(prop.topic):
                await prop.process_message(sub_topic, payload)

    async def post_value(self, topic_suffix: str, value: str, retain: bool = False):
        await self.device.post_value(
            f"{self.topic}/{topic_suffix}" if topic_suffix else self.topic,
            value,
            retain,
        )


class HomieProperty(Generic[T], HomieTopologyMixin[None], SubscriptionsMixin[None], ABC):  # type: ignore
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        datatype: str,
        retained: bool = True,
        setter: Optional[Callable[[T], Awaitable[Optional[T]]]] = None,
        data_format: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        super(HomieProperty, self).__init__()
        self.node = node
        self.topic = topic
        self.name = name
        self.datatype = datatype
        self.setter = setter
        self.retained = retained
        self.data_format = data_format
        self.unit = unit
        self._cached_value: Optional[T] = None
        self._pending_set = False
        node.add_property(self)

    def serialize(self, value: T) -> str:
        raise NotImplementedError

    def deserialize(self, value: str) -> T:
        raise NotImplementedError

    def get_value(self) -> Optional[str]:
        if self._cached_value is not None:
            return self.serialize(self._cached_value)
        return None

    def get_subscriptions(self) -> Iterable[str]:
        if self.setter:
            yield f"{self.topic}/set"

    def get_attributes(self) -> Dict[str, str]:
        attributes = {
            "$name": self.name,
            "$datatype": self.datatype,
            "$settable": "true" if self.setter else "false",
            "$retained": "true" if self.retained else "false",
        }
        if self.data_format:
            attributes["$format"] = self.data_format
        if self.unit:
            attributes["$unit"] = self.unit
        return attributes

    async def handle_set_payload(self, payload: str) -> Optional[T]:
        if not self.setter:
            raise HomieError("Property is not settable")
        res = await self.setter(self.deserialize(payload))
        self._pending_set = True
        if res is not None:
            await self.update_value(res)
        return res

    async def process_message(self, topic: str, payload: str):
        sub_topic = topic[len(self.topic) + 1 :]
        if sub_topic == "set":
            await self.handle_set_payload(payload)
        else:
            raise HomieError(f"Unknown topic {topic}")

    async def post_value(self, topic_suffix: str, value: str, retain: bool = False):
        if not self.node:
            raise HomieError("Property not attached to a node")
        await self.node.post_value(
            f"{self.topic}/{topic_suffix}" if topic_suffix else self.topic,
            value,
            retain,
        )

    async def update_value(self, value: T):
        if value == self._cached_value and not self._pending_set:
            return
        await self.post_value("", self.serialize(value), self.retained)
        self._cached_value = value
        self._pending_set = False


class HomieStringProperty(HomieProperty[str]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[str], Awaitable[None]]] = None,
        data_format: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "string",
            retained,
            setter,
            data_format,
            unit,
        )

    def serialize(self, value: str) -> str:
        return value

    def deserialize(self, value: str) -> str:
        return value


class HomieIntegerProperty(HomieProperty[int]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[int], Awaitable[None]]] = None,
        data_format: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "integer",
            retained,
            setter,
            data_format,
            unit,
        )

    def serialize(self, value: int) -> str:
        return str(value)

    def deserialize(self, value: str) -> int:
        return int(value)


class HomieFloatProperty(HomieProperty[float]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[float], Awaitable[None]]] = None,
        data_format: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "float",
            retained,
            setter,
            data_format,
            unit,
        )

    def serialize(self, value: float) -> str:
        return str(value)

    def deserialize(self, value: str) -> float:
        return float(value)


class HomieBooleanProperty(HomieProperty[bool]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[bool], Awaitable[None]]] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "boolean",
            retained,
            setter,
        )

    def serialize(self, value: bool) -> str:
        return "true" if value else "false"

    def deserialize(self, value: str) -> bool:
        return value == "true"


E = TypeVar("E", bound=Enum)


class HomieEnumProperty(Generic[E], HomieProperty[E]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        enum_type: Type[E],
        retained: bool = True,
        setter: Optional[Callable[[E], Awaitable[Optional[E]]]] = None,
    ):
        enum_values: List[str] = [e.value for e in enum_type]
        assert len(enum_values) > 0 and type(enum_values[0]) is str
        super().__init__(
            node,
            topic,
            name,
            "enum",
            retained,
            setter,
            ",".join(enum_values),
        )
        self.enum = enum_type

    def serialize(self, value: E) -> str:
        return str(value.value)

    def deserialize(self, value: str) -> E:
        return self.enum(value)


class HomieDateTimeProperty(HomieProperty[datetime]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[datetime], Awaitable[None]]] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "datetime",
            retained,
            setter,
        )

    def serialize(self, value: datetime) -> str:
        return value.isoformat()

    def deserialize(self, value: str) -> datetime:
        return datetime.fromisoformat(value)


class HomieDurationProperty(HomieProperty[timedelta]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[timedelta], Awaitable[None]]] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "duration",
            retained,
            setter,
        )

    def serialize(self, value: timedelta) -> str:
        return isodate.duration_isoformat(value)

    def deserialize(self, value: str) -> timedelta:
        return isodate.parse_duration(value)


JsonSerializable = Union[
    None, bool, int, float, str, List["JsonSerializable"], Dict[str, "JsonSerializable"]
]  # type: ignore


class HomieJsonProperty(HomieProperty[JsonSerializable]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        retained: bool = True,
        setter: Optional[Callable[[JsonSerializable], Awaitable[None]]] = None,
    ):
        super().__init__(
            node,
            topic,
            name,
            "string",
            retained,
            setter,
            "json",
        )

    def serialize(self, value: JsonSerializable) -> str:
        return json.dumps(value, default=fallback_json_serializer)

    def deserialize(self, value: str) -> JsonSerializable:
        return json.loads(value, default=fallback_json_deserializer)


class Commands(Enum):
    REQUEST = "REQUEST"


C = TypeVar("C", bound=Enum)


class HomieCommandProperty(Generic[E], HomieEnumProperty[E]):
    def __init__(
        self,
        node: HomieNode,
        topic: str,
        name: str,
        setter: Optional[Callable[[E], Awaitable[Optional[E]]]],
        enum_type: Type[E] = Commands,  # type: ignore
        retained: bool = False,
    ):
        self.enum_type = enum_type
        super().__init__(node, topic, name, enum_type, retained, setter)

    async def handle_set_payload(self, payload: str) -> Optional[E]:
        res = await super().handle_set_payload(payload)
        if res is None and self.enum_type is Commands:
            res = self.enum_type.REQUEST  # type: ignore
        await self.update_value(res)  # type: ignore
        return res


# TODO: Color
