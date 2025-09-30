# FUNCTIONAL IMPORTS
import websockets, asyncio, logging, sys
from typing import List, TypedDict

log_format = logging.Formatter(
    "[%(asctime)s:%(name)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s"
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_format)
log = logging.getLogger("CollabVMLib")
log.setLevel("INFO")
log.addHandler(stdout_handler)

# TYPE IMPORTS
from enum import IntEnum
from websockets import Subprotocol, Origin

# ENUMS
class CollabVMState(IntEnum):
    """Represents client connection states."""
    
    WS_DISCONNECTED = -1
    """WebSocket is disconnected."""
    WS_CONNECTED = 0
    """WebSocket is connected."""
    VM_CONNECTED = 1
    """Connected to the VM but not logged in."""
    LOGGED_IN = 2
    """Authenticated with announced auth server."""

class CollabVMRank(IntEnum):
    """Represents user ranks."""
    
    Unregistered = 0
    """Represents an unregistered user."""
    Registered = 1
    """Represents a registered user."""
    Admin = 2
    """Represents an admin user."""
    Mod = 3
    """Represents a moderator user."""

class CollabVMClientRenameStatus(IntEnum):
    """Represents the status of a client rename attempt."""
    
    SUCCEEDED = 0
    """The rename attempt was successful."""
    FAILED_TAKEN = 1
    """The desired name is already taken."""
    FAILED_INVALID = 2
    """The desired name is invalid."""
    FAILED_REJECTED = 3
    """The rename attempt was authoritatively rejected."""

class CollabVMLibConnectionOptions(TypedDict):
    WS_URL: str
    NODE_ID: str
    CREDENTIALS: str | None

# GUACAMOLE
def guac_encode(elements: list) -> str:
    return ','.join([f'{len(element)}.{element}' for element in elements]) + ';'

def guac_decode(instruction: str) -> list:
    elements = []
    position = 0

    # Loop and collect elements
    continueScanning = True
    while continueScanning:
        # Ensure current position is not out of bounds
        if position >= len(instruction):
            raise ValueError(f"Unexpected EOL in guacamole instruction at character {position}")
        # Get position of separator
        separatorPosition = instruction.index('.', position)
        # Read and validate element length
        try:
            elementLength = int(instruction[position:separatorPosition])
        except ValueError:
            raise ValueError(f"Malformed element length in guacamole exception at character {position}")
        if elementLength < 0 or elementLength > len(instruction) - separatorPosition - 1:
            raise ValueError(f"Invalid element length in guacamole exception at character {position}")
        position = separatorPosition + 1

        # Collect element
        element = instruction[position:position+elementLength]
        
        position = position + elementLength

        elements.append(element)

        # Check separator
        if position >= len(instruction):
            raise ValueError(f"Unexpected EOL in guacamole instruction at character {position}")
        
        # Check terminator
        match instruction[position]:
            case ',':
                position = position + 1
            case ';':
                continueScanning = False
            case _:
                raise ValueError(f"Unexpected '{instruction[position]}' in guacamole instruction at character {position}")
    
    return elements

# HELPER FUNCS

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

# HELPER FUNCS ASYNC
async def send_chat_message(websocket, message: str):
    log.debug(f"Sending chat message: {message}")
    await websocket.send(guac_encode(["chat", message]))

async def send_guac(websocket, *args: str):
    await websocket.send(guac_encode(list(args)))

log.info("CollabVMLib imported ...")
