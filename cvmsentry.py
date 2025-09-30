from typing import List, Optional
from cvmlib import guac_decode, guac_encode
import config
import os
import websocket
import logging
import sys
import rel
import threading
import multiprocessing
import argparse

LOG_LEVEL = getattr(config, 'log_level', 'INFO')
user_state = 0 # 0 = disconnected, 1 = connected, 2 = logged in

def full_login(ws: websocket.WebSocketApp):
    # Connect
    vm_name = os.path.basename(ws.url)
    ws.send(guac_encode("connect", vm_name))
    ws.send(guac_encode("login", config.credentials["session_auth"]))
    
def on_message(ws, message: str):
    global user_state
    if guac_decode(message) is not None:
        decoded: Optional[List[str]] = guac_decode(message)
        match decoded:
            case ["nop"]:
                ws.send(guac_encode("nop"))
            case ['connect', '1', '1', '1', '0']:
                print("Connected")
                user_state = 1
            case ["auth", "https://auth.collabvm.org"]:
                full_login(ws)
            case ["login", "1"]:
                print("Logged in")
                user_state = 2
            case _: # For the XTREME!!!
                if decoded is not None:
                    if decoded[0] == "sync" or decoded[0] == "png":
                     return
                    elif decoded[0] == "adduser":
                        number_of_users = int(decoded[1])

                        if user_state == 0 and number_of_users == 1:
                            return # Ignore ourselves...
                    
                        # print(f"Number of users: {number_of_users}")
                        # users = decoded[2:]
                        # print(f"Users: {users}")
                    elif decoded[0] == "chat":
                        user = "System" if len(decoded[1]) == 0 else decoded[1]
                        message = decoded[2]
                        log.info(f"[{user}]: {message}")

               # print(f"Received: {decoded}")

def on_error(ws, error):

    print(error)

def on_close(ws, close_status_code, close_msg):
    print("### closed ###")

def on_open(ws):
    ws.send(guac_encode("rename",""))
# websocket.enableTrace(True)

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

# Parse the command-line argument
parser = argparse.ArgumentParser(description="CVM-Sentry")
parser.add_argument("vm_name", type=str, help="Name of the VM to connect to (e.g., vm7)")
args = parser.parse_args()

# Ensure the provided VM name exists in the config
if args.vm_name not in config.vms:
    log.error(f"VM '{args.vm_name}' not found in configuration.")
    sys.exit(1)

# Use the provided VM name to connect
ws = websocket.WebSocketApp(config.vms[args.vm_name],
                            on_open=on_open,
                            on_message=on_message,
                            on_error=on_error,
                            on_close=on_close,
                            header={"User-Agent": "cvmsentry"}, subprotocols=["guacamole"])
ws.run_forever(dispatcher=rel, reconnect=5)  # Set dispatcher to automatic reconnection, 5 second reconnect delay if connection closed unexpectedly
rel.signal(2, rel.abort)  # Keyboard Interrupt
rel.dispatch()