from typing import List, Optional
from enum import IntEnum

def guac_decode(string: str) -> Optional[List[str]]:
    """Implementation of guacamole decoder
    Example: guac_decode(\"4.chat,5.hello\") -> [\"chat\", \"hello\"]"""

    if not string:
        return []

    idx: int = 0
    distance: int
    result: List[str] = []
    chars: List[str] = list(string)

    while True:
        dist_str: str = ""

        while chars[idx].isdecimal():
            dist_str += chars[idx]
            idx = idx + 1

        if idx >= 1:
            idx -= 1

        if not dist_str.isdigit():
            return None

        distance = int(dist_str)
        idx += 1

        if chars[idx] != ".":
            return None

        idx += 1

        addition: str = ""
        for num in range(idx, idx + distance):
            addition += chars[num]

        result.append(addition)

        idx += distance
        if idx >= len(chars):
            return None

        if chars[idx] == ",":
            pass
        elif chars[idx] == ";":
            break
        else:
            return None

        idx += 1

    return result

def guac_encode(*args: str) -> str:
    """Implementation of guacamole encoder
    Example: guac_encode(\"chat\", \"hello\") -> \"4.chat,5.hello;\" """

    return f"{','.join(f'{len(arg)}.{arg}' for arg in args)};"

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