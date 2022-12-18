import asyncio
import re
import sys
from hashlib import md5
from typing import List, Optional

from loguru import logger

from meross2homie.config import CONFIG
from meross2homie.manager import BridgeManager


def main(argv: Optional[List[str]] = None):
    # Set loglevel to debug
    logger.remove()

    if argv is None:
        argv = sys.argv[1:]

    if "-h" in argv or "--help" in argv:
        print(f"Usage: {sys.argv[0]} [config file]")
        return 0

    if argv:
        CONFIG.load(argv[0])
    else:
        CONFIG.load()

    logger.add(sys.stderr, level=CONFIG.log_level)

    loop = asyncio.get_event_loop()

    try:
        loop.run_until_complete(amain())
    except (KeyboardInterrupt, EOFError):
        pass


async def amain():
    while True:
        # noinspection PyBroadException
        try:
            async with BridgeManager() as manager:
                await manager.process_events()
        except (KeyboardInterrupt, EOFError):
            logger.info("Exiting")
            break
        except Exception:
            logger.exception("Unhandled error; reconnecting in 5 seconds")
            await asyncio.sleep(5)


def calc_mqtt_password(user_id: str, key: str, mac_address: str) -> str:
    macaddr_regex = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})")
    if not macaddr_regex.match(mac_address):
        raise ValueError("MAC address must be in the format 00:11:22:aa:bb:cc")

    try:
        int(user_id)
    except ValueError:
        raise ValueError("User ID must be a number")
    if int(user_id) < 0:
        raise ValueError("User ID must be a positive number")

    password = md5(f"{mac_address.lower()}{key}".encode("utf-8")).hexdigest().lower()
    return f"{user_id}_{password}"


def calc_mqtt_password_main(argv: Optional[List[str]] = None):
    if argv is None:
        argv = sys.argv[1:]

    usage = f"Usage: {sys.argv[0]} <user ID> <key> <MAC address>"
    if "-h" in argv or "--help" in argv or len(argv) != 3:
        print(usage)
        return 0
    user_id, key, mac_address = argv

    try:
        password = calc_mqtt_password(user_id, key, mac_address)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    print("Username:", mac_address.lower())
    print("Password:", password)
