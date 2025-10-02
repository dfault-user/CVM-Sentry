from typing import List, Optional
from cvmlib import guac_decode, guac_encode, CollabVMRank, CollabVMState, CollabVMClientRenameStatus
import config
import os
import websockets, asyncio
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

log.info(f"CVM-Sentry started")

users = {}
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
    await websocket.send(guac_encode("chat", message))

async def connect(vm_name: str):
    if vm_name not in config.vms:
        log.error(f"VM '{vm_name}' not found in configuration.")
        return
    uri = config.vms[vm_name]
    STATE = None
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
        log.info(f"Connected to VM '{vm_name}' at {uri}")
        STATE = CollabVMState.WS_CONNECTED
        await websocket.send(guac_encode("rename", ""))
        await websocket.send(guac_encode("connect", vm_name))
        if vm_name not in users:
            users[vm_name] = {}
        # response = await websocket.recv()
        async for message in websocket:
            decoded: Optional[List[str]] = guac_decode(str(message))
            match decoded:
                case ["nop"]:
                    await websocket.send(guac_encode("nop"))
                    log.debug((f"({CollabVMState(STATE).name}) Received: {decoded}"))
                case ["auth", config.auth_server]:
                    await asyncio.sleep(1)
                    await websocket.send(
                        guac_encode("login", config.credentials["session_auth"])
                    )
                case ["connect", "1", "1", "1", "0"]:
                    STATE = CollabVMState.VM_CONNECTED
                    log.debug((f"({CollabVMState(STATE).name} - {vm_name}) Connected"))
                case ["login", "1"]:
                    STATE = CollabVMState.LOGGED_IN
                    await send_chat_message(websocket, "RICHARD NIXON TO THE RESCUE")
                case _:
                    if decoded is not None:
                        if decoded[0] in ("sync", "png", "flag", "turn", "size"):
                            continue
                        elif decoded[0] == "chat":
                            user = "System" if len(decoded[1]) == 0 else decoded[1]
                            if user == "System" or STATE < CollabVMState.LOGGED_IN:
                                continue
                            message = decoded[2]
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
                                        await send_chat_message(websocket, f"You are {user} with rank {users[vm_name].get(user, 'Unknown').name}.")
                                    case "about":
                                        await send_chat_message(websocket, "RICHARD NIXONTRON 4000")

                            continue
                        elif decoded[0] == "adduser":
                                if STATE == CollabVMState.LOGGED_IN:
                                    username = decoded[2]
                                    rank = CollabVMRank(int(decoded[3]))
                                    users[vm_name][username] = rank
                                elif STATE < CollabVMState.LOGGED_IN:
                                    initial_user_payload = decoded[2:]
                                    for i in range(0, len(initial_user_payload), 2):
                                        username = initial_user_payload[i]
                                        rank = CollabVMRank(int(initial_user_payload[i + 1]))
                                        users[vm_name][username] = rank
                        elif decoded[0] == "remuser":
                                if STATE == CollabVMState.LOGGED_IN:
                                    username = decoded[2]
                                    if username in users[vm_name]:
                                        del users[vm_name][username]
                        elif decoded[0] == "rename":
                                if STATE == CollabVMState.WS_CONNECTED and decoded[1:3] == ["0", "0"]:
                                    log.debug(f"AUTHORITATIVE GUEST NAME!! {decoded[3]}")
                                    ## SET CURRENT BOT NAME ##
                                    vm_botuser[vm_name] = decoded[3]
                                elif STATE == CollabVMState.LOGGED_IN and decoded[2] in vm_botuser[vm_name]:
                                    log.debug(f"AUTHORITATIVE BOT NAME CHANGE!! {decoded[3]}")
                                    ## SET CURRENT BOT NAME ##
                                    vm_botuser[vm_name] = decoded[3]     
                    log.debug(
                        (
                            f"({CollabVMState(STATE).name} - {vm_name}) Received unfiltered: {decoded}"
                        )
                    )

for vm in config.vms.keys():


    def start_vm_thread(vm_name: str):
        asyncio.run(connect(vm_name))
    async def main():
        tasks = [connect(vm) for vm in config.vms.keys()]
        await asyncio.gather(*tasks)

    asyncio.run(main())