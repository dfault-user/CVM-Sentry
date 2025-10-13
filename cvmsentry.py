from typing import List
from cvmlib import (
    guac_decode,
    guac_encode,
    CollabVMRank,
    CollabVMState,
    CollabVMClientRenameStatus,
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

LOG_LEVEL = getattr(config, "log_level", "INFO")

# Prepare logs
if not os.path.exists("logs"):
    os.makedirs("logs")
log_format = logging.Formatter(
    "[%(asctime)s:%(name)s] %(levelname)s - %(message)s"
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_format)
log = logging.getLogger("CVMSentry")
log.setLevel(LOG_LEVEL)
log.addHandler(stdout_handler)

vms = {}
vm_botuser = {}
STATE = CollabVMState.WS_DISCONNECTED


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


async def periodic_snapshot_task():
    """Background task that saves VM framebuffers as snapshots in WEBP format."""
    log.info("Starting periodic snapshot task")
    while True:
        try:
            await asyncio.sleep(10)  # Wait 10 seconds
            log.debug("Running periodic framebuffer snapshot capture...")

            for vm_name, vm_data in vms.items():
                # Skip if VM doesn't have a framebuffer
                if not vm_data.get("framebuffer"):
                    continue

                # Create directory structure if it doesn't exist
                date_str = datetime.now().strftime("%b-%d-%Y")
                snapshot_dir = os.path.join("logs", "webp", vm_name, date_str)
                os.makedirs(snapshot_dir, exist_ok=True)

                # Get current epoch timestamp
                epoch_timestamp = int(datetime.now().timestamp())
                filename = f"{epoch_timestamp}.webp"
                filepath = os.path.join(snapshot_dir, filename)

                # Create a hash of the framebuffer for comparison
                framebuffer = vm_data["framebuffer"]
                if not framebuffer:
                    continue

                # Calculate difference hash for the image
                current_hash = str(imagehash.dhash(framebuffer))

                # Only save if the framebuffer has changed since last snapshot
                if current_hash != vm_data.get("last_frame_hash"):
                    try:
                        # Save as webp with good quality/size balance
                        framebuffer.save(filepath, format="WEBP", quality=65, method=6)
                        vm_data["last_frame_hash"] = current_hash
                        log.debug(f"Saved snapshot of {vm_name} to {filepath}")
                    except Exception as e:
                        log.error(f"Failed to save snapshot for {vm_name}: {e}")

        except Exception as e:
            log.error(f"Error in periodic snapshot task: {e}")
            # Continue running even if there's an error


async def connect(vm_name: str):
    global STATE
    global vms
    global vm_botuser
    if vm_name not in config.vms:
        log.error(f"VM '{vm_name}' not found in configuration.")
        return
    vms[vm_name] = {
        "turn_queue": [],
        "active_turn_user": None,
        "users": {},
        "framebuffer": None,
        "last_frame_hash": None,
        "size": (0, 0),
    }
    uri = config.vms[vm_name]
    log_file_path = os.path.join(
        getattr(config, "log_directory", "logs"), f"{vm_name}.json"
    )
    if not os.path.exists(log_file_path):
        with open(log_file_path, "w") as log_file:
            log_file.write("{}")
    async with websockets.connect(
        uri=uri,
        subprotocols=[Subprotocol("guacamole")],
        origin=Origin(get_origin_from_ws_url(uri)),
        user_agent_header="cvmsentry/1 (https://git.nixlabs.dev/clair/cvmsentry)",
    ) as websocket:
        STATE = CollabVMState.WS_CONNECTED
        log.info(f"Connected to VM '{vm_name}' at {uri}")
        await send_guac(websocket, "rename", "")
        await send_guac(websocket, "connect", vm_name)
        if vm_name not in vm_botuser:
            vm_botuser[vm_name] = ""
        # response = await websocket.recv()
        async for message in websocket:
            decoded: List[str] = guac_decode(str(message))
            match decoded:
                case ["nop"]:
                    await send_guac(websocket, "nop")
                case ["auth", config.auth_server]:
                    await asyncio.sleep(1)
                    await send_guac(
                        websocket, "login", config.credentials["session_auth"]
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
                        log.info(
                            f"Connected to VM '{vm_name}' successfully. Turns enabled: {bool(int(turns_enabled))}, Votes enabled: {bool(int(votes_enabled))}, Uploads enabled: {bool(int(uploads_enabled))}"
                        )
                    else:
                        log.error(
                            f"Failed to connect to VM '{vm_name}'. Connection status: {connection_status}"
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
                                    f"({STATE.name} - {vm_name}) Bot rename on VM {vm_name}: {vm_botuser[vm_name]} -> {new_name}"
                                )
                                vm_botuser[vm_name] = new_name
                            else:
                                log.debug(
                                    f"({STATE.name} - {vm_name}) Bot rename on VM {vm_name} failed with status {CollabVMClientRenameStatus(int(status)).name}"
                                )
                        case ["1", old_name, new_name]:
                            if old_name in vms[vm_name]["users"]:
                                log.debug(
                                    f"({STATE.name} - {vm_name}) User rename on VM {vm_name}: {old_name} -> {new_name}"
                                )
                                vms[vm_name]["users"][new_name] = vms[vm_name][
                                    "users"
                                ].pop(old_name)
                case ["login", "1"]:
                    STATE = CollabVMState.LOGGED_IN
                    if config.send_autostart and config.autostart_messages:
                        await send_chat_message(
                            websocket, random.choice(config.autostart_messages)
                        )
                case ["chat", user, message, *backlog]:
                    system_message = user == ""
                    if system_message:
                        continue
                    if not backlog:
                        log.info(f"[{vm_name} - {user}]: {message}")

                    def get_rank(username: str) -> CollabVMRank:
                        return vms[vm_name]["users"].get(username, {}).get("rank")

                    def admin_check(username: str) -> bool:
                        return (
                            username in config.admins
                            and get_rank(username) > CollabVMRank.Unregistered
                        )

                    utc_now = datetime.now(timezone.utc)
                    utc_day = utc_now.strftime("%Y-%m-%d")
                    timestamp = utc_now.isoformat()

                    with open(log_file_path, "r+") as log_file:
                        try:
                            log_data = json.load(log_file)
                        except json.JSONDecodeError:
                            log_data = {}

                        if utc_day not in log_data:
                            log_data[utc_day] = []

                        if backlog:
                            pass
                            # for i in range(0, len(backlog), 2):
                            #     backlog_user = backlog[i]
                            #     backlog_message = backlog[i + 1]
                            #     if not any(entry["message"] == backlog_message and entry["username"] == backlog_user for entry in log_data[utc_day]):
                            #         log.info(f"[{vm_name} - {backlog_user} (backlog)]: {backlog_message}")
                            #         log_data[utc_day].append({
                            #             "timestamp": timestamp,
                            #             "username": backlog_user,
                            #             "message": backlog_message
                            #         })

                        log_data[utc_day].append(
                            {
                                "type": "chat",
                                "timestamp": timestamp,
                                "username": user,
                                "message": message,
                            }
                        )
                        log_file.seek(0)
                        json.dump(log_data, log_file, indent=4)
                        log_file.truncate()

                    if config.commands["enabled"] and message.startswith(
                        config.commands["prefix"]
                    ):
                        command = (
                            message[len(config.commands["prefix"]) :].strip().lower()
                        )
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
                                    f"({STATE.name} - {vm_name}) Dumping user list for VM {vm_name}: {vms[vm_name]['users']}"
                                )
                                await send_chat_message(
                                    websocket, f"Dumped user list to console."
                                )
                case ["adduser", count, *list]:
                    for i in range(int(count)):
                        user = list[i * 2]
                        rank = CollabVMRank(int(list[i * 2 + 1]))
                        
                        if user in vms[vm_name]["users"]:
                            vms[vm_name]["users"][user]["rank"] = rank
                            log.info(
                                f"[{vm_name}] User '{user}' rank updated to {rank.name}."
                            )
                        else:
                            vms[vm_name]["users"][user] = {"rank": rank}
                            log.info(
                                f"[{vm_name}] User '{user}' connected with rank {rank.name}."
                            )
                case ["turn", _, "0"]:
                    if STATE < CollabVMState.LOGGED_IN:
                        continue
                    if (
                        vms[vm_name]["active_turn_user"] is None
                        and not vms[vm_name]["turn_queue"]
                    ):
                        # log.debug(f"({STATE.name} - {vm_name}) Incoming queue exhaustion matches the VM's state. Dropping update.")
                        continue
                    vms[vm_name]["active_turn_user"] = None
                    vms[vm_name]["turn_queue"] = []
                    log.debug(
                        f"({STATE.name} - {vm_name}) Turn queue is naturally exhausted."
                    )
                case ["size", "0", width, height]:
                    log.debug(f"({STATE.name} - {vm_name}) !!! Framebuffer size update: {width}x{height} !!!")
                    vms[vm_name]["size"] = (int(width), int(height))
                case ["png", "0", "0", "0", "0", initial_frame_b64]:
                    if STATE < CollabVMState.LOGGED_IN:
                        try:
                            log.debug(f"({STATE.name} - {vm_name}) !!! Initial framebuffer received !!!")
                            image_data = base64.b64decode(initial_frame_b64)
                            framebuffer = Image.open(BytesIO(image_data))
                            vms[vm_name]["framebuffer"] = framebuffer
                            log.debug(f"({STATE.name} - {vm_name}) Initial framebuffer loaded: {framebuffer.size}")
                        except Exception as e:
                            log.error(f"({STATE.name} - {vm_name}) Failed to process initial framebuffer: {e}")
                    elif STATE >= CollabVMState.LOGGED_IN:
                        try:
                            image_data = base64.b64decode(initial_frame_b64)
                            framebuffer = Image.open(BytesIO(image_data))
                            
                            # Get expected size from VM info
                            expected_width, expected_height = vms[vm_name]["size"]
                            
                            # Ensure the framebuffer matches the expected size
                            if framebuffer.size != (expected_width, expected_height) and expected_width > 0 and expected_height > 0:
                                framebuffer = framebuffer.resize((expected_width, expected_height))

                            vms[vm_name]["framebuffer"] = framebuffer
                            
                            log.debug(f"({STATE.name} - {vm_name}) Framebuffer updated: {framebuffer.size}")
                        except Exception as e:
                            log.error(f"({STATE.name} - {vm_name}) Failed to process framebuffer update: {e}")
                case ["png", "0", "0", x, y, rect_b64]:
                    
                    if vms[vm_name]["framebuffer"] is not None:
                        try:
                            # Check if we have a framebuffer with the right size
                            log.debug(f"({STATE.name} - {vm_name}) !!! Framebuffer rectangle update at ({x},{y}) !!!")
                            expected_width, expected_height = vms[vm_name]["size"]
                            if (vms[vm_name]["framebuffer"].size != (expected_width, expected_height) and 
                                expected_width > 0 and expected_height > 0):
                                log.warning(f"({STATE.name} - {vm_name}) Framebuffer size mismatch: "
                                            f"expected {expected_width}x{expected_height}, got {vms[vm_name]['framebuffer'].size}")
                                continue
                            
                            # Decode and paste the rectangle update
                            x_pos, y_pos = int(x), int(y)
                            rect_data = base64.b64decode(rect_b64)
                            rect_img = Image.open(BytesIO(rect_data))
                            
                            # Create a copy of the framebuffer to work with
                            framebuffer = vms[vm_name]["framebuffer"].copy()
                            framebuffer.paste(rect_img, (x_pos, y_pos))
                            
                            # Verify the framebuffer size is consistent
                            if rect_img.size[0] + x_pos > framebuffer.size[0] or rect_img.size[1] + y_pos > framebuffer.size[1]:
                                log.warning(f"({STATE.name} - {vm_name}) Rectangle update would exceed framebuffer bounds: "
                                            f"rect size {rect_img.size} at position ({x_pos},{y_pos}) exceeds framebuffer {framebuffer.size}")
                                continue

                            # Update the framebuffer
                            vms[vm_name]["framebuffer"] = framebuffer
                            
                        except Exception as e:
                            log.error(f"({STATE.name} - {vm_name}) Failed to process framebuffer rectangle update: {e}")
                    else:
                        log.debug(f"({STATE.name} - {vm_name}) Received rectangle update but framebuffer is not initialized")
                case ["turn", turn_time, count, current_turn, *queue]:
                    if (
                        queue == vms[vm_name]["turn_queue"]
                        and current_turn == vms[vm_name]["active_turn_user"]
                    ):
                        continue
                    for user in vms[vm_name]["users"]:
                        vms[vm_name]["turn_queue"] = queue
                        vms[vm_name]["active_turn_user"] = (
                            current_turn if current_turn != "" else None
                        )
                    if current_turn:
                        log.info(f"[{vm_name}] It's now {current_turn}'s turn. Queue: {queue}")

                        utc_now = datetime.now(timezone.utc)
                        utc_day = utc_now.strftime("%Y-%m-%d")
                        timestamp = utc_now.isoformat()

                        with open(log_file_path, "r+") as log_file:
                            try:
                                log_data = json.load(log_file)
                            except json.JSONDecodeError:
                                log_data = {}

                            if utc_day not in log_data:
                                log_data[utc_day] = []

                            log_data[utc_day].append(
                                {
                                    "type": "turn",
                                    "timestamp": timestamp,
                                    "active_turn_user": current_turn,
                                    "queue": queue,
                                }
                            )

                            log_file.seek(0)
                            json.dump(log_data, log_file, indent=4)
                            log_file.truncate()

                case ["remuser", count, *list]:
                    for i in range(int(count)):
                        username = list[i]
                        if username in vms[vm_name]["users"]:
                            del vms[vm_name]["users"][username]
                            log.info(f"[{vm_name}] User '{username}' left.")
                case (
                    ["flag", *args] | ["png", *args] | ["sync", *args]
                ):
                    continue
                case _:
                    if decoded is not None:
                        log.debug(
                            f"({STATE.name} - {vm_name}) Unhandled message: {decoded}"
                        )


log.info(f"({STATE.name}) CVM-Sentry started")

for vm in config.vms.keys():

    def start_vm_thread(vm_name: str):
        asyncio.run(connect(vm_name))

    async def main():

        async def connect_with_reconnect(vm_name: str):
            while True:
                try:
                    await connect(vm_name)
                except websockets.exceptions.ConnectionClosedError as e:
                    log.warning(
                        f"Connection to VM '{vm_name}' closed with error: {e}. Reconnecting..."
                    )
                    await asyncio.sleep(5)  # Wait before attempting to reconnect
                except websockets.exceptions.ConnectionClosedOK:
                    log.warning(
                        f"Connection to VM '{vm_name}' closed cleanly (code 1005). Reconnecting..."
                    )
                    await asyncio.sleep(5)  # Wait before attempting to reconnect
                except websockets.exceptions.InvalidStatus as e:
                    log.error(
                        f"Failed to connect to VM '{vm_name}' with status code: {e}. Reconnecting..."
                    )
                    await asyncio.sleep(10)  # Wait longer for HTTP errors
                except websockets.exceptions.WebSocketException as e:
                    log.error(
                        f"WebSocket error connecting to VM '{vm_name}': {e}. Reconnecting..."
                    )
                    await asyncio.sleep(5)
                except Exception as e:
                    log.error(
                        f"Unexpected error connecting to VM '{vm_name}': {e}. Reconnecting..."
                    )
                    await asyncio.sleep(10)  # Wait longer for unexpected errors

        # Create tasks for VM connections
        vm_tasks = [connect_with_reconnect(vm) for vm in config.vms.keys()]

        # Add periodic snapshot task
        snapshot_task = periodic_snapshot_task()

        # Run all tasks concurrently
        all_tasks = [snapshot_task] + vm_tasks
        await asyncio.gather(*all_tasks)

    asyncio.run(main())
