import sys
import uuid
import signal
import asyncio
from aioconsole import ainput
from config.settings import load_config
from config.logger import setup_logging
from core.utils.util import get_local_ip, validate_mcp_endpoint
from core.http_server import SimpleHttpServer
from core.websocket_server import WebSocketServer
from core.utils.util import check_ffmpeg_installed
from core.utils.gc_manager import get_gc_manager

TAG = __name__
logger = setup_logging()


async def wait_for_exit() -> None:
    """
    Block until Ctrl-C or SIGTERM is received.
    - Unix: use add_signal_handler.
    - Windows: rely on KeyboardInterrupt.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":  # Unix / macOS
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        # Keep the Windows loop pending so KeyboardInterrupt reaches asyncio.run
        # and shutdown is not blocked by leftover non-daemon threads.
        try:
            await asyncio.Future()
        except KeyboardInterrupt:  # Ctrl‑C
            pass


async def monitor_stdin():
    """Monitor stdin and consume Enter key presses."""
    while True:
        await ainput()


async def main():
    check_ffmpeg_installed()
    config = await load_config()

    # server.auth_key takes precedence; generate an ephemeral key when unset.
    # The key signs vision JWTs, OTA tokens, and WebSocket authentication tokens.
    auth_key = config["server"].get("auth_key", "")

    if not auth_key or len(auth_key) == 0 or "你" in auth_key:
        auth_key = str(uuid.uuid4().hex)

    config["server"]["auth_key"] = auth_key

    # Start stdin monitoring.
    stdin_task = asyncio.create_task(monitor_stdin())

    # Start the global garbage-collection manager with a five-minute interval.
    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    # Start the WebSocket server.
    ws_server = WebSocketServer(config)
    ws_task = asyncio.create_task(ws_server.start())
    # Start the HTTP server.
    ota_server = SimpleHttpServer(config)
    ota_task = asyncio.create_task(ota_server.start())

    port = int(config["server"].get("http_port", 8003))
    logger.bind(tag=TAG).info(
        "OTA endpoint:\t\thttp://{}:{}/xiaozhi/ota/",
        get_local_ip(),
        port,
    )
    logger.bind(tag=TAG).info(
        "Vision endpoint:\thttp://{}:{}/mcp/vision/explain",
        get_local_ip(),
        port,
    )
    mcp_endpoint = config.get("mcp_endpoint", None)
    if mcp_endpoint is not None and "你" not in mcp_endpoint:
        # Validate the MCP endpoint format.
        if validate_mcp_endpoint(mcp_endpoint):
            logger.bind(tag=TAG).info("MCP endpoint:\t{}", mcp_endpoint)
            # Convert the discovery endpoint into its call endpoint.
            mcp_endpoint = mcp_endpoint.replace("/mcp/", "/call/")
            config["mcp_endpoint"] = mcp_endpoint
        else:
            logger.bind(tag=TAG).error("Invalid MCP endpoint format")
            config["mcp_endpoint"] = "你的接入点 websocket地址"

    # Read the WebSocket port with a safe default.
    websocket_port = 8000
    server_config = config.get("server", {})
    if isinstance(server_config, dict):
        websocket_port = int(server_config.get("port", 8000))

    logger.bind(tag=TAG).info(
        "WebSocket endpoint:\tws://{}:{}/xiaozhi/v1/",
        get_local_ip(),
        websocket_port,
    )

    logger.bind(tag=TAG).info(
        "======= The address above is a WebSocket URL; do not open it in a browser ======="
    )
    logger.bind(tag=TAG).info(
        "Use a Xiaozhi device or compatible client to test the WebSocket connection"
    )
    logger.bind(tag=TAG).info(
        "=============================================================\n"
    )

    try:
        await wait_for_exit()
    except asyncio.CancelledError:
        print("Task cancelled; cleaning up resources...")
    finally:
        # Stop the global garbage-collection manager.
        await gc_manager.stop()

        # Cancel all background tasks.
        stdin_task.cancel()
        ws_task.cancel()
        if ota_task:
            ota_task.cancel()

        # Wait briefly for task termination.
        await asyncio.wait(
            [stdin_task, ws_task, ota_task] if ota_task else [stdin_task, ws_task],
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("Server shut down successfully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user; server stopped.")
