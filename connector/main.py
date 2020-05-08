"""The Firestarter Connector connects to Firehose and writes the output to a kafka topic
"""

import asyncio
import json
import os
import ssl
import time
import warnings
import zlib
from typing import Optional, Tuple
from kafka import KafkaProducer  # type: ignore
from kafka.errors import NoBrokersAvailable  # type: ignore

COMPRESSION: str
USERNAME: Optional[str]
APIKEY: Optional[str]
KEEPALIVE: int
INIT_CMD_ARGS: str
INIT_CMD_TIME: str
SERVERNAME: str
STATS_PERIOD: int

STATS_LOCK: asyncio.Lock
FINISHED: asyncio.Event
LINES_READ: int = 0
BYTES_READ: int = 0
LAST_GOOD_PITR: Optional[int]
LISTEN_PORT: int
PRODUCER: KafkaProducer


class ZlibReaderProtocol(asyncio.StreamReaderProtocol):
    """asyncio Protocol that handles streaming decompression of Firehose data"""

    def __init__(self, mode: str, *args, **kwargs) -> None:
        self._z = None
        if mode == "deflate":  # no header, raw deflate stream
            self._z = zlib.decompressobj(-zlib.MAX_WBITS)
        elif mode == "compress":  # zlib header
            self._z = zlib.decompressobj(zlib.MAX_WBITS)
        elif mode == "gzip":  # gzip header
            self._z = zlib.decompressobj(16 | zlib.MAX_WBITS)
        super().__init__(*args, **kwargs)

    def data_received(self, data: bytes) -> None:
        if not self._z:
            super().data_received(data)
        else:
            super().data_received(self._z.decompress(data))


# pylint: disable=bad-continuation
async def open_connection(
    host: str = None, port: int = None, *, loop=None, limit=2 ** 16, **kwds
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Coroutine copied from asyncio source with a tweak to use our custom ZlibReadProtocol
    for the returned StreamReader
    """

    if loop is None:
        loop = asyncio.get_event_loop()
    else:
        warnings.warn(
            "The loop argument is deprecated since Python 3.8, "
            "and scheduled for removal in Python 3.10.",
            DeprecationWarning,
            stacklevel=2,
        )
    reader = asyncio.StreamReader(limit=limit, loop=loop)
    read_protocol = ZlibReaderProtocol(COMPRESSION, reader, loop=loop)
    write_protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.create_connection(lambda: read_protocol, host, port, **kwds)
    writer = asyncio.StreamWriter(transport, write_protocol, reader, loop)
    return reader, writer


def build_init_cmd(time_mode: str) -> str:
    """Builds the init command based on the environment variables provided in docker-compose
    """
    initiation_command = f"{time_mode} username {USERNAME} password {APIKEY}"
    if COMPRESSION != "":
        initiation_command += f" compression {COMPRESSION}"
    if KEEPALIVE != "":
        initiation_command += f" keepalive {KEEPALIVE}"
    if INIT_CMD_ARGS != "":
        initiation_command += f" {INIT_CMD_ARGS}"
    initiation_command += "\n"

    return initiation_command


def parse_script_args() -> None:
    """Sets global variables based on the environment variables provided in docker-compose
    """
    # pylint: disable=global-statement
    # pylint: disable=line-too-long
    global USERNAME, APIKEY, SERVERNAME, COMPRESSION, STATS_PERIOD, KEEPALIVE, INIT_CMD_TIME, INIT_CMD_ARGS

    # **** REQUIRED ****
    USERNAME = os.getenv("FH_USERNAME")
    APIKEY = os.getenv("FH_APIKEY")
    # **** NOT REQUIRED ****
    SERVERNAME = os.getenv("SERVER")  # type: ignore
    COMPRESSION = os.getenv("COMPRESSION")  # type: ignore
    STATS_PERIOD = int(os.getenv("PRINT_STATS_PERIOD"))  # type: ignore
    KEEPALIVE = int(os.getenv("KEEPALIVE"))  # type: ignore
    INIT_CMD_TIME = os.getenv("INIT_CMD_TIME")  # type: ignore
    if INIT_CMD_TIME.split()[0] not in ["live", "pitr"]:
        raise ValueError(f'$INIT_CMD_TIME value is invalid, should be "live" or "pitr <pitr>"')
    INIT_CMD_ARGS = os.getenv("INIT_CMD_ARGS")  # type: ignore
    for command in ["live", "pitr", "compression", "keepalive", "username", "password"]:
        if command in INIT_CMD_ARGS.split():
            raise ValueError(
                f'$INIT_CMD_ARGS should not contain the "{command}" command. \
                It belongs in its own variable.'
            )


async def event_wait(event: asyncio.Event, timeout: int) -> bool:
    """Wait for event with timeout, return True if event was set, False if we timed out

    This is intended to behave like threading.Event.wait"""
    try:
        return await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        return event.is_set()


async def print_stats(period: int) -> None:
    """Periodically print information about how much data is flowing from Firehose."""
    # pylint: disable=global-statement
    global LINES_READ, BYTES_READ

    total_lines = 0
    total_bytes = 0
    initial_seconds = time.monotonic()
    last_seconds = initial_seconds
    first_pitr = None
    catchup_rate = 0
    while not FINISHED.is_set():
        await event_wait(FINISHED, period)
        now = time.monotonic()
        total_seconds = now - initial_seconds
        period_seconds = now - last_seconds
        if first_pitr:
            if total_seconds:
                catchup_rate = (int(LAST_GOOD_PITR) - int(first_pitr)) / total_seconds
            else:
                catchup_rate = 0
        else:
            first_pitr = LAST_GOOD_PITR
        last_seconds = now
        async with STATS_LOCK:
            total_lines += LINES_READ
            total_bytes += BYTES_READ
            if period_seconds:
                print(
                    f"Period messages/s {LINES_READ / period_seconds:>5.0f}, \
                    period bytes/s {BYTES_READ / period_seconds:>5.0f}"
                )
            if total_seconds:
                print(
                    f"Total  messages/s {total_lines / total_seconds:>5.0f}, \
                    total  bytes/s {total_bytes / total_seconds:>5.0f}"
                )
            if catchup_rate:
                print(f"Total catchup rate: {catchup_rate:.2f}x")
            print(f"Total messages received: {total_lines}")
            print()
            LINES_READ = 0
            BYTES_READ = 0


async def read_firehose(time_mode: str) -> Optional[str]:
    """Open a connection to Firehose and read from it forever,
    passing all messages along to all connected clients.

    Any errors will result in the function returning a string pitr value that
    can be passed to the function on a future call to allow for a reconnection
    without missing any data. The returned value may also be None, meaning that
    an error occurred before any pitr value was received from the server.

    time_mode may be either the string "live" or a pitr string that looks like
    "pitr <pitr>" where <pitr> is a value previously returned by this function
    """
    # pylint: disable=global-statement
    global LAST_GOOD_PITR, LINES_READ, BYTES_READ

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    fh_reader, fh_writer = await open_connection(host=SERVERNAME, port=1501, ssl=context)
    print(f"Opened connection to Firehose at {SERVERNAME}:1501")

    initiation_command = build_init_cmd(time_mode)
    print(initiation_command.strip())
    fh_writer.write(initiation_command.encode())
    await fh_writer.drain()

    pitr = None
    while True:
        timeout = (KEEPALIVE + 10) if KEEPALIVE else None
        try:
            line = await asyncio.wait_for(fh_reader.readline(), timeout)
        except asyncio.TimeoutError:
            print(f"Server connection looks idle (haven't received anything in {timeout} seconds)")
            break
        except (AttributeError, OSError) as error:
            print("Lost server connection:", error)
            break
        if line == b"":
            print("Got EOF from Firehose server, connection intentionally closed")
            break
        message = json.loads(line)
        if message["type"] == "error":
            print(f'Error: {message["error_msg"]}')
            break

        LAST_GOOD_PITR = pitr = message["pitr"]

        async with STATS_LOCK:
            LINES_READ += 1
            BYTES_READ += len(line)

        PRODUCER.send(os.getenv("KAFKA_TOPIC_NAME"), line)
        PRODUCER.flush()

    # We'll only reach this point if something's wrong with the connection.
    return pitr


async def main():
    """Connect to Firehose and write the output to kafka
    """
    # pylint: disable=global-statement
    global PRODUCER, STATS_LOCK, FINISHED, LAST_GOOD_PITR, LISTEN_PORT

    PRODUCER = None
    while PRODUCER is None:
        try:
            PRODUCER = KafkaProducer(bootstrap_servers=["kafka:9092"])
        except NoBrokersAvailable as error:
            print(f"Kafka isn't available ({error}), trying again in a few seconds")
            time.sleep(3)

    STATS_LOCK = asyncio.Lock()
    FINISHED = asyncio.Event()
    LAST_GOOD_PITR = None
    LISTEN_PORT = 1601

    parse_script_args()

    stats_task = None
    if STATS_PERIOD:
        stats_task = asyncio.create_task(print_stats(STATS_PERIOD))
    connection_error_limit = 3
    errors = 0
    time_mode = INIT_CMD_TIME
    while True:
        pitr = await read_firehose(time_mode)
        if pitr:
            time_mode = f"pitr {pitr}"
            print(f'Reconnecting with "{time_mode}"')
            errors = 0
        elif errors < connection_error_limit - 1:
            print(f'Previous connection never got a pitr, trying again with "{time_mode}"')
            errors += 1
        else:
            print(
                f"Connection failed {connection_error_limit} \
                times before getting a non-error message, quitting"
            )
            break

    if stats_task:
        print("Dumping stats one last time...")
        FINISHED.set()
        await stats_task


if __name__ == "__main__":
    asyncio.run(main())
