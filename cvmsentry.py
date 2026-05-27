from typing import List
from urllib.parse import urlparse
from cvmlib import (
    guac_decode,
    guac_encode,
    CollabVMRank,
    CollabVMState,
    CollabVMClientRenameStatus,
    CollabVMClientModerationPerms
)
import config
import os, random, websockets, asyncio
from websockets import Subprotocol, Origin
import logging
import sys
from datetime import datetime, timezone
import json
from io import BytesIO
from PIL import Image
import base64
import imagehash
import msgpack
LOG_LEVEL = getattr(config, "log_level", "INFO")

# Prepare logs
log_format = logging.Formatter("[%(asctime)s:%(name)s] %(levelname)s - %(message)s")
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_format)
log = logging.getLogger("CVMSentry")
log.setLevel(LOG_LEVEL)
log.addHandler(stdout_handler)

vms = {}
vm_botuser = {}

def get_origin_from_ws_url(ws_url: str) -> str:
    domain = (
        ws_url.removeprefix("ws:")
        .removeprefix("wss:")
        .removeprefix("/")
        .removeprefix("/")
        .split("/", 1)[0]
    )
    is_wss = ws_url.startswith("wss:")
    return f"http{'s' if is_wss else ''}://{domain}/"


async def send_chat_message(websocket, message: str):
    log.debug(f"Sending chat message: {message}")
    await websocket.send(guac_encode(["chat", message]))


async def send_guac(websocket, *args: str):
    await websocket.send(guac_encode(list(args)))


def push_log_entry(vm_log_directory: str, entry_type: str, **data) -> None:
    utc_now = datetime.now(timezone.utc)
    utc_day = utc_now.strftime("%Y-%m-%d")
    daily_log_path = os.path.join(vm_log_directory, f"{utc_day}.json")

    if os.path.exists(daily_log_path):
        with open(daily_log_path, "r") as log_file:
            try:
                log_data = json.load(log_file)
            except json.JSONDecodeError:
                log_data = []
    else:
        log_data = []

    log_data.append(
        {
            "type": entry_type,
            "timestamp": utc_now.isoformat(),
            **data,
        }
    )

    with open(daily_log_path, "w") as log_file:
        json.dump(log_data, log_file, indent=4)


async def periodic_snapshot_task():
    """Background task that saves VM framebuffers as snapshots in WEBP format."""
    log.info("Starting periodic snapshot task")
    while True:
        try:
            await asyncio.sleep(config.snapshot_cadence)
            log.debug("Running periodic framebuffer snapshot capture...")

            save_tasks = []
            for vm_name, vm_data in vms.items():
                # Skip if VM doesn't have a framebuffer
                if not vm_data.get("framebuffer"):
                    continue

                # Create directory structure if it doesn't exist - [date]/[vm] structure in UTC
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                snapshot_dir = os.path.join(config.log_directory, "webp", date_str, vm_name)
                os.makedirs(snapshot_dir, exist_ok=True)

                # Generate formatted timestamp in UTC
                timestamp = datetime.now(timezone.utc).strftime("%H-%M-%S")
                filename = f"{timestamp}.webp"
                filepath = os.path.join(snapshot_dir, filename)

                # Get framebuffer reference (no copy needed)
                framebuffer = vm_data["framebuffer"]
                if not framebuffer:
                    continue

                # Calculate difference hash asynchronously to avoid blocking
                current_hash = await asyncio.to_thread(
                    lambda: str(imagehash.dhash(framebuffer))
                )

                # Only save if the framebuffer has changed since last snapshot
                if current_hash != vm_data.get("last_frame_hash"):
                    # Pass framebuffer directly without copying
                    save_tasks.append(
                        asyncio.create_task(
                            save_image_async(
                                framebuffer, filepath, vm_name, vm_data, current_hash
                            )
                        )
                    )

            # Wait for all save tasks to complete
            if save_tasks:
                await asyncio.gather(*save_tasks)

        except Exception as e:
            log.error(f"Error in periodic snapshot task: {e}")
            # Continue running even if there's an error


async def save_image_async(image, filepath, vm_name, vm_data, current_hash):
    """Save an image to disk asynchronously."""
    try:
        # Run the image saving in a thread pool to avoid blocking
        await asyncio.to_thread(
            image.save, filepath, format="WEBP", quality=65, method=6, minimize_size=True
        )
        vm_data["last_frame_hash"] = current_hash
        log.info(f"Saved snapshot of {vm_name} ({datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC)")
    except Exception as e:
        log.error(f"Failed to save snapshot for {vm_name}: {e}")


def apply_framebuffer_fragment(log_label: str, state: CollabVMState, x: int, y: int, frame_data: bytes, source: str) -> None:
    try:
        fragment_img = Image.open(BytesIO(frame_data))
        expected_width, expected_height = vms[log_label]["size"]
        framebuffer = vms[log_label]["framebuffer"]

        if x == 0 and y == 0 and expected_width > 0 and expected_height > 0 and fragment_img.size == (expected_width, expected_height):
            vms[log_label]["framebuffer"] = fragment_img
            log.debug(
                f"({state.name} - {log_label}) [{source}] Framebuffer updated with full frame, size: {fragment_img.size}"
            )
            return

        if framebuffer is None:
            if expected_width > 0 and expected_height > 0:
                framebuffer = Image.new("RGB", (expected_width, expected_height))
                vms[log_label]["framebuffer"] = framebuffer
            elif x == 0 and y == 0:
                vms[log_label]["framebuffer"] = fragment_img
                log.debug(
                    f"({state.name} - {log_label}) [{source}] Framebuffer initialized from first frame, size: {fragment_img.size}"
                )
                return
            else:
                log.debug(
                    f"({state.name} - {log_label}) [{source}] Dropping fragment at ({x}, {y}) before framebuffer initialization"
                )
                return

        vms[log_label]["framebuffer"].paste(fragment_img, (x, y))
        log.debug(
            f"({state.name} - {log_label}) [{source}] Updated framebuffer at ({x}, {y}), fragment size: {fragment_img.size}"
        )
    except Exception as e:
        log.error(
            f"({state.name} - {log_label}) [{source}] Failed to process framebuffer fragment: {e}"
        )


async def connect(vm_obj: dict):
    log.debug(f"Connecting to VM at {vm_obj['ws_url']} with origin {get_origin_from_ws_url(vm_obj['ws_url'])}")
    global vms
    global vm_botuser
    fqdn = urlparse(vm_obj["ws_url"]).netloc
    STATE = CollabVMState.WS_DISCONNECTED
    log_label = vm_obj.get("log_label") or f"{fqdn}-{vm_obj.get('node', '')}"
    vms[log_label] = {
        "turn_queue": [],
        "active_turn_user": None,
        "users": {},
        "framebuffer": None,
        "last_frame_hash": None,
        "size": (0, 0),
        "binary_requested": bool(vm_obj.get("binary_protocol", True)),
        "binary_enabled": False,
        "self_rank" : None,
    }
    ws_url = vm_obj["ws_url"]
    log_directory = getattr(config, "log_directory", "./logs")
    # Create VM-specific log directory
    vm_log_directory = os.path.join(log_directory, log_label)
    os.makedirs(vm_log_directory, exist_ok=True)

    origin = Origin(vm_obj.get("origin_override", get_origin_from_ws_url(ws_url)))

    async with websockets.connect(
        uri=ws_url,
        subprotocols=[Subprotocol("guacamole")],
        origin=Origin(origin),
        user_agent_header="cvmsentry/1 (https://github.com/dfault-user/CVM-Sentry)",
    ) as websocket:
        STATE = CollabVMState.WS_CONNECTED
        log.info(f"Connected to VM '{log_label}' at {ws_url}")
        await send_guac(websocket, "rename", config.unauth_name)
        if vms[log_label]["binary_requested"]:
            await send_guac(websocket, "cap", "bin")
        await send_guac(websocket, "connect", vm_obj["node"])
        if log_label not in vm_botuser:
            vm_botuser[log_label] = ""
        async for message in websocket:
            if not isinstance(message, str):
                try:
                    unpacked = msgpack.unpackb(bytes(message), raw=False)
                    if isinstance(unpacked, dict) and unpacked.get("type") == 0:
                        rect = unpacked.get("rect") or {}
                        x = int(rect.get("x", 0))
                        y = int(rect.get("y", 0))
                        data = rect.get("data")
                        if isinstance(data, (bytes, bytearray)):
                            apply_framebuffer_fragment(
                                log_label=log_label,
                                state=STATE,
                                x=x,
                                y=y,
                                frame_data=bytes(data),
                                source="bin",
                            )
                        else:
                            log.debug(
                                f"({STATE.name} - {log_label}) [bin] Ignoring rect update with invalid data payload"
                            )
                    else:
                        log.debug(
                            f"({STATE.name} - {log_label}) [bin] Unhandled binary message: {unpacked}"
                        )
                except Exception as e:
                    log.error(
                        f"({STATE.name} - {log_label}) [bin] Failed to decode binary message: {e}"
                    )
                continue

            decoded: List[str] = guac_decode(message)
            match decoded:
                case ["nop"]:
                    await send_guac(websocket, "nop")
                case ["cap", *enabled_caps]:
                    vms[log_label]["binary_enabled"] = "bin" in enabled_caps
                    if vms[log_label]["binary_enabled"]:
                        log.info(f"[{log_label}] Binary framebuffer protocol enabled")
                    else:
                        log.info(f"[{log_label}] Binary framebuffer protocol not enabled by server")
                case ["auth", auth_server]:
                    await asyncio.sleep(1)
                    if vm_obj.get("auth"):
                        await send_guac(
                            websocket,
                            "login",
                            vm_obj["auth"]["session_auth"],
                        )
                    else:
                        log.error(
                            f"Auth server '{auth_server}' not recognized for VM '{log_label}'"
                        )
                case [
                    "connect",
                    connection_status,
                    turns_enabled,
                    votes_enabled,
                    uploads_enabled,
                ]:
                    if connection_status == "1":
                        STATE = CollabVMState.VM_CONNECTED
                        vms[log_label]["self_rank"] = CollabVMRank.Unregistered
                        log.info(
                            f"Connected to VM '{log_label}' successfully. Turns enabled: {bool(int(turns_enabled))}, Votes enabled: {bool(int(votes_enabled))}, Uploads enabled: {bool(int(uploads_enabled))}"
                        )
                    else:
                        log.debug(
                            f"Failed to connect to VM '{log_label}'. Connection status: {connection_status}"
                        )
                        STATE = CollabVMState.WS_DISCONNECTED
                        await websocket.close()
                case ["rename", *instructions]:
                    match instructions:
                        case ["0", status, new_name]:
                            if (
                                CollabVMClientRenameStatus(int(status))
                                == CollabVMClientRenameStatus.SUCCEEDED
                            ):
                                log.debug(
                                    f"({STATE.name} - {log_label}) Bot rename on VM {log_label}: {vm_botuser[log_label]} -> {new_name}"
                                )
                                vm_botuser[log_label] = new_name
                            else:
                                log.debug(
                                    f"({STATE.name} - {log_label}) Bot rename on VM {log_label} failed with status {CollabVMClientRenameStatus(int(status)).name}"
                                )
                        case ["1", old_name, new_name]:
                            if old_name in vms[log_label]["users"]:
                                log.debug(
                                    f"({STATE.name} - {log_label}) User rename on VM {log_label}: {old_name} -> {new_name}"
                                )
                                vms[log_label]["users"][new_name] = vms[log_label][
                                    "users"
                                ].pop(old_name)
                case ["login", "1"]:
                    STATE = CollabVMState.LOGGED_IN
                    vms[log_label]["self_rank"] = CollabVMRank.Registered
                    if config.send_autostart and config.autostart_messages:
                        await send_chat_message(
                            websocket, random.choice(config.autostart_messages)
                        )
                case ["chat", user, message, *backlog]:
                    system_message = user == ""
                    if system_message or backlog:
                        continue
                    log.info(f"[{log_label} - {user}]: {message}")

                    def get_rank(username: str) -> CollabVMRank:
                        return vms[log_label]["users"].get(username, {}).get("rank")

                    def admin_check(username: str) -> bool:
                        return (
                            get_rank(username) >= CollabVMRank.Admin
                            or (username in config.admins
                                and get_rank(username) > CollabVMRank.Unregistered)
                        )

                    push_log_entry(
                        vm_log_directory,
                        "chat",
                        username=user,
                        message=message,
                    )

                    if config.commands["enabled"] and message.startswith(
                        config.commands["prefix"]
                    ):
                        command_full = message[len(config.commands["prefix"]):].strip().lower()
                        command = command_full.split(" ")[0] if " " in command_full else command_full
                        match command:
                            case "whoami":
                                await send_chat_message(
                                    websocket,
                                    f"You are {user} with rank {get_rank(user).name}.",
                                )
                            case "about":
                                await send_chat_message(
                                    websocket,
                                    config.responses.get(
                                        "about", "CVM-Sentry (NO RESPONSE CONFIGURED)"
                                    ),
                                )
                            case "dump":
                                if not admin_check(user):
                                    continue

                                log.info(
                                    f"({STATE.name} - {log_label}) Dumping user list for VM {log_label}: {vms[log_label]['users']}"
                                )

                                await send_chat_message(
                                    websocket, f"Dumped user list to console."
                                )
                            case "auth":
                                if not admin_check(user):
                                    continue
                                
                                if "admin_password" in vm_obj:
                                    await send_guac(websocket, "admin", "2", vm_obj["admin_password"])
                            case "di":
                                if not admin_check(user):
                                    continue
                                for usr in vms[log_label]["users"]:
                                    await send_guac(websocket, "admin", "19", usr)
                                    await asyncio.sleep(0)
                            case "die":
                                if not admin_check(user):
                                    continue
                                os._exit(0)
                                
                case ["adduser", count, *list]:
                    for i in range(int(count)):
                        user = list[i * 2]
                        rank = CollabVMRank(int(list[i * 2 + 1]))

                        if user in vms[log_label]["users"]:
                            if user == vm_botuser[log_label]:
                                vms[log_label]["self_rank"] = rank
                            vms[log_label]["users"][user]["rank"] = rank
                            log.info(
                                f"[{log_label}] User '{user}' rank updated to {rank.name}."
                            )

                            if STATE == CollabVMState.LOGGED_IN and vm_obj.get("auth") is not None:
                                await send_guac(websocket, "admin", "19", user)
                                
                        else:
                            vms[log_label]["users"][user] = {"rank": rank}
                            log.info(
                                f"[{log_label}] User '{user}' connected with rank {rank.name}."
                            )
                            push_log_entry(
                                vm_log_directory,
                                "user_join",
                                username=user,
                            )
                            if STATE == CollabVMState.LOGGED_IN and vm_obj.get("auth") is not None:
                                await send_guac(websocket, "admin", "19", user)
                            await asyncio.sleep(0)
                case ["turn", _, "0"]:
                    if STATE < CollabVMState.LOGGED_IN:
                        continue
                    if (
                        vms[log_label]["active_turn_user"] is None
                        and not vms[log_label]["turn_queue"]
                    ):
                        # log.debug(f"({STATE.name} - {log_label}) Incoming queue exhaustion matches the VM's state. Dropping update.")
                        continue
                    vms[log_label]["active_turn_user"] = None
                    vms[log_label]["turn_queue"] = []
                    log.debug(
                        f"({STATE.name} - {log_label}) Turn queue is naturally exhausted."
                    )
                case ["size", "0", width, height]:
                    log.debug(
                        f"({STATE.name} - {log_label}) !!! Framebuffer size update: {width}x{height} !!!"
                    )
                    vms[log_label]["size"] = (int(width), int(height))
                case ["png", "0", "0", "0", "0", full_frame_b64]:
                    apply_framebuffer_fragment(
                        log_label=log_label,
                        state=STATE,
                        x=0,
                        y=0,
                        frame_data=base64.b64decode(full_frame_b64),
                        source="png",
                    )
                case ["png", "0", "0", x, y, rect_b64]:
                    apply_framebuffer_fragment(
                        log_label=log_label,
                        state=STATE,
                        x=int(x),
                        y=int(y),
                        frame_data=base64.b64decode(rect_b64),
                        source="png",
                    )
                case ["turn", turn_time, count, current_turn, *queue]:
                    if (
                        queue == vms[log_label]["turn_queue"]
                        and current_turn == vms[log_label]["active_turn_user"]
                    ):
                        continue
                    for user in vms[log_label]["users"]:
                        vms[log_label]["turn_queue"] = queue
                        vms[log_label]["active_turn_user"] = (
                            current_turn if current_turn != "" else None
                        )
                    if current_turn:
                        log.info(
                            f"[{log_label}] It's now {current_turn}'s turn. Queue: {queue}"
                        )
                        push_log_entry(
                            vm_log_directory,
                            "turn",
                            active_turn_user=current_turn,
                            queue=queue,
                        )

                case ["remuser", count, *list]:
                    for i in range(int(count)):
                        username = list[i]
                        if username in vms[log_label]["users"]:
                            del vms[log_label]["users"][username]
                            push_log_entry(
                                vm_log_directory,
                                "user_leave",
                                username=username,
                            )
                            log.info(f"[{log_label}] User '{username}' left.")
                case ["flag", *args] | ["png", *args] | ["sync", *args]:
                    continue
                case ["admin", "0"]: # generic auth failure
                    log.info(
                        f"({STATE.name} - {log_label}) Admin authentication failed with generic failure"
                    )
                case ["admin", "1"]:
                    vms[log_label]["self_rank"] = CollabVMRank.Admin
                    log.info(
                        f"({STATE.name} - {log_label}) Admin authentication succeeded with provided password"
                    )
                case ["admin", "3", perm_int]:
                    permissions = CollabVMClientModerationPerms(int(perm_int))
                    vms[log_label]["self_rank"] = CollabVMRank.Mod
                    log.info(
                        f"({STATE.name} - {log_label}) Moderator authentication succeeded with provided password and permissions {permissions}"
                    )
                case ["admin", "19", username, ip]:
                    log.info(
                        f"({STATE.name} - {log_label}) User '{username}' with IP {ip}"
                    )
                case _:
                    if decoded is not None:
                        log.debug(
                            f"({STATE.name} - {log_label}) Unhandled message: {decoded}"
                        )

log.info(f"CVM-Sentry started")

for vm_dict_label, vm_obj in config.vms.items():

    def start_vm_thread(vm_obj: dict):
        asyncio.run(connect(vm_obj))

    async def main():

        async def connect_with_reconnect(vm_obj: dict):
            while True:
                try:
                    await connect(vm_obj)
                except websockets.exceptions.ConnectionClosedError as e:
                    log.error(
                        f"Connection to VM '{vm_obj['ws_url']}' closed with error: {e}. Reconnecting..."
                    )
                    await asyncio.sleep(0)
                except websockets.exceptions.ConnectionClosedOK:
                    log.warning(
                        f"Connection to VM '{vm_obj['ws_url']}' closed cleanly (code 1005). Reconnecting..."
                    )
                    await asyncio.sleep(0)
                except websockets.exceptions.InvalidStatus as e:
                    log.debug(
                        f"Failed to connect to VM '{vm_obj['ws_url']}' with status code: {e}. Reconnecting..."
                    )
                    await asyncio.sleep(0)
                except websockets.exceptions.WebSocketException as e:
                    log.error(
                        f"WebSocket error connecting to VM '{vm_obj['ws_url']}': {e}. Reconnecting..."
                    )
                    await asyncio.sleep(5)
                except Exception as e:
                    log.error(
                        f"Unexpected error connecting to VM '{vm_obj['ws_url']}': {e}. Reconnecting..."
                    )
                    await asyncio.sleep(0)

        # Create tasks for VM connections
        vm_tasks = [connect_with_reconnect(vm) for vm in config.vms.values()]

        # Add periodic snapshot task
        snapshot_task = periodic_snapshot_task()

        # Run all tasks concurrently
        all_tasks = [snapshot_task] + vm_tasks
        await asyncio.gather(*all_tasks)

    asyncio.run(main())
