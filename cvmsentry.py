from typing import List
from cvmlib import guac_decode, guac_encode, CollabVMRank, CollabVMState, CollabVMClientRenameStatus
import config
import os, random, websockets, asyncio
from websockets import Subprotocol, Origin
import logging
import sys
from datetime import datetime, timezone
import json

LOG_LEVEL = getattr(config, "log_level", "INFO")

# Prepare logs
if not os.path.exists("logs"):
    os.makedirs("logs")
log_format = logging.Formatter(
    "[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s"
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_format)
log = logging.getLogger("cvmsentry")
log.setLevel(LOG_LEVEL)
log.addHandler(stdout_handler)

users = {}
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

async def connect(vm_name: str):
    global STATE
    global users
    global vm_botuser
    if vm_name not in config.vms:
        log.error(f"VM '{vm_name}' not found in configuration.")
        return
    uri = config.vms[vm_name]
    log_file_path = os.path.join(getattr(config, "log_directory", "logs"), f"{vm_name}.json")
    if not os.path.exists(log_file_path):
        with open(log_file_path, "w") as log_file:
            log_file.write("{}")
    async with websockets.connect(
        uri=uri,
        subprotocols=[Subprotocol("guacamole")],
        origin=Origin(get_origin_from_ws_url(uri)),
        user_agent_header="cvmsentry/1 (https://git.nixlabs.dev/clair/cvmsentry)"
    ) as websocket:
        STATE = CollabVMState.WS_CONNECTED
        log.info(f"Connected to VM '{vm_name}' at {uri}")
        await send_guac(websocket, "rename", "")
        await send_guac(websocket, "connect", vm_name)
        if vm_name not in users:
            users[vm_name] = {}
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
                    await send_guac(websocket, "login", config.credentials["session_auth"])
                case ["connect", *rest]:
                    STATE = CollabVMState.VM_CONNECTED
                    connection_status = "Connected" if rest[0] == "1" else "Disconnected" if rest[0] == "2" else "Connected"
                    turns_status = "Enabled" if rest[1] == "1" else "Disabled"
                    votes_status = "Enabled" if rest[2] == "1" else "Disabled"
                    uploads_status = "Enabled" if rest[3] == "1" else "Disabled"
                    log.debug(f"({STATE.name} - {vm_name}) {connection_status} | Turns: {turns_status} | Votes: {votes_status} | Uploads: {uploads_status}")
                case ["rename", *instructions]:
                    match instructions:
                        case ["0", status, new_name]:
                            if CollabVMClientRenameStatus(int(status)) == CollabVMClientRenameStatus.SUCCEEDED:
                                log.debug(f"({STATE.name} - {vm_name}) Bot rename on VM {vm_name}: {vm_botuser[vm_name]} -> {new_name}")
                                vm_botuser[vm_name] = new_name
                            else:
                                log.debug(f"({STATE.name} - {vm_name}) Bot rename on VM {vm_name} failed with status {CollabVMClientRenameStatus(int(status)).name}")
                        case ["1", old_name, new_name]:
                            if old_name in users[vm_name]:
                                log.debug(f"({STATE.name} - {vm_name}) User rename on VM {vm_name}: {old_name} -> {new_name}")
                                users[vm_name][new_name] = users[vm_name].pop(old_name)
                case ["login", "1"]:
                    STATE = CollabVMState.LOGGED_IN
                    await send_chat_message(websocket, random.choice(config.autostart_messages))
                case ["chat", user, message, *backlog]:
                    system_message = (user == "") 
                    if system_message:
                        continue
                    if not backlog:
                        log.info(f"[{vm_name} - {user}]: {message}")

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
                            for i in range(0, len(backlog), 2):
                                backlog_user = backlog[i]
                                backlog_message = backlog[i + 1]
                                if not any(entry["message"] == backlog_message and entry["username"] == backlog_user for entry in log_data[utc_day]):
                                    log.info(f"[{vm_name} - {backlog_user} (backlog)]: {backlog_message}")
                                    log_data[utc_day].append({
                                        "timestamp": timestamp,
                                        "username": backlog_user,
                                        "message": backlog_message
                                    })

                        log_data[utc_day].append({
                            "timestamp": timestamp,
                            "username": user,
                            "message": message
                        })

                        log_file.seek(0)
                        json.dump(log_data, log_file, indent=4)
                        log_file.truncate()
                    if config.commands["enabled"] and message.startswith(config.commands["prefix"]):
                        command = message[len(config.commands["prefix"]):].strip().lower()
                        match command:
                            case "whoami":
                                await send_chat_message(websocket, f"You are {user} with rank {users[vm_name][user]['rank'].name}.")
                            case "about":
                                await send_chat_message(websocket, config.responses.get("about", "CVM-Sentry (NO RESPONSE CONFIGURED)"))
                            case "dump":
                                if user != "dfu":
                                    await send_chat_message(websocket, "You do not have permission to use this command.")
                                    continue
                                log.debug(f"({STATE.name} - {vm_name}) Dumping user list for VM {vm_name}: {users[vm_name]}")
                                await send_chat_message(websocket, f"Dumped user list to console.")
                case ["adduser", count, *list]:
                    for i in range(int(count)):
                        user = list[i * 2]
                        rank = CollabVMRank(int(list[i * 2 + 1]))
                        if user in users[vm_name]:
                            users[vm_name][user]["rank"] = rank
                            log.info(f"[{vm_name}] User '{user}' rank updated to {rank.name}.")
                        else:
                            users[vm_name][user] = {"rank": rank, "turn_active": False}
                            log.info(f"[{vm_name}] User '{user}' connected with rank {rank.name}.")
                case ["turn", _, "0"]:
                    if STATE < CollabVMState.LOGGED_IN:
                        continue
                    log.debug(f"({STATE.name} - {vm_name}) Turn queue exhausted.")
                case ["turn", turn_time, count, current_turn, *queue]:
                    log.debug(f"({STATE.name} - {vm_name}) Turn queue updated: {queue} | Current turn: {current_turn} | Time left for current turn: {int(turn_time)//1000}s")
                    for user in users[vm_name]:
                        users[vm_name][user]["turn_active"] = (user == current_turn)
                case ["remuser", count, *list]:
                    for i in range(int(count)):
                        username = list[i]
                        if username in users[vm_name]:
                            del users[vm_name][username]
                            log.info(f"[{vm_name}] User '{username}' left.")
                case ["sync", *args] | ["png", *args] | ["flag", *args] | ["size", *args]:
                    continue
                case _:
                    if decoded is not None:
                        log.debug(f"({STATE.name} - {vm_name}) Unhandled message: {decoded}")

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
                    log.warning(f"Connection to VM '{vm_name}' closed with error: {e}. Reconnecting...")
                    await asyncio.sleep(5)  # Wait before attempting to reconnect
                except websockets.exceptions.ConnectionClosedOK:
                    log.warning(f"Connection to VM '{vm_name}' closed cleanly (code 1005). Reconnecting...")
                    await asyncio.sleep(5)  # Wait before attempting to reconnect

        tasks = [connect_with_reconnect(vm) for vm in config.vms.keys()]
        await asyncio.gather(*tasks)

    asyncio.run(main())