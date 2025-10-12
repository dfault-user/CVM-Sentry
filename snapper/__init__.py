from typing import List
from cvmlib import guac_decode, guac_encode, CollabVMRank, CollabVMState, CollabVMClientRenameStatus
import config
import os, websockets, asyncio, base64
from websockets import Subprotocol, Origin
import logging
import sys
from PIL import Image
from io import BytesIO
from datetime import datetime
import imagehash
import glob

LOG_LEVEL = getattr(config, "log_level", "INFO")

# Setup logging
log_format = logging.Formatter(
    "[%(asctime)s:%(name)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s"
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_format)
log = logging.getLogger("CVMSnapper")
log.setLevel(LOG_LEVEL)
log.addHandler(stdout_handler)

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

def get_file_dhash(file_path: str) -> str:
    """Get dhash (difference hash) of an image file."""
    try:
        with Image.open(file_path) as img:
            hash_obj = imagehash.dhash(img)
            return str(hash_obj)
    except Exception as e:
        log.error(f"Failed to get dhash for file {file_path}: {e}")
        return ""

def get_image_dhash_from_data(image_data: bytes) -> str:
    """Get dhash (difference hash) of image data."""
    try:
        with Image.open(BytesIO(image_data)) as img:
            hash_obj = imagehash.dhash(img)
            return str(hash_obj)
    except Exception as e:
        log.error(f"Failed to get dhash for image data: {e}")
        return ""

def get_latest_snapshot_path(vm_name: str) -> str:
    """Get the path of the most recent snapshot for a VM."""
    try:
        snapshot_dir = os.path.join(config.log_directory, "webp", vm_name)
        if not os.path.exists(snapshot_dir):
            return ""
        
        # Get all .webp files in the directory
        pattern = os.path.join(snapshot_dir, "snapshot_*.webp")
        files = glob.glob(pattern)
        
        if not files:
            return ""
        
        # Sort by modification time and return the most recent
        latest_file = max(files, key=os.path.getmtime)
        return latest_file
    except Exception as e:
        log.error(f"Failed to get latest snapshot for VM {vm_name}: {e}")
        return ""

def images_are_identical(image_data: bytes, existing_file_path: str) -> bool:
    """Compare image data with an existing file using dhash to check if they're visually similar."""
    try:
        if not os.path.exists(existing_file_path):
            return False
        
        # Get dhash of new image data
        new_hash = get_image_dhash_from_data(image_data)
        if not new_hash:
            return False
        
        # Get dhash of existing file
        existing_hash = get_file_dhash(existing_file_path)
        if not existing_hash:
            return False
        
        # Compare dhashes - they should be identical for very similar images
        # dhash is more forgiving than SHA256 and will detect visually identical images
        return new_hash == existing_hash
    except Exception as e:
        log.error(f"Failed to compare images: {e}")
        return False

async def send_guac(websocket, *args: str):
    await websocket.send(guac_encode(list(args)))

def convert_png_to_webp(b64_png_data: str, output_path: str, vm_name: str) -> bool:
    """Convert base64 PNG data to WebP format and save to file, checking for duplicates."""
    try:
        # Decode base64 PNG data
        png_data = base64.b64decode(b64_png_data)
        
        # Check if this image is identical to the latest snapshot
        latest_snapshot = get_latest_snapshot_path(vm_name)
        if latest_snapshot:
            # Convert PNG to WebP in memory for comparison
            with Image.open(BytesIO(png_data)) as img:
                webp_buffer = BytesIO()
                img.save(webp_buffer, "WEBP", quality=55, method=6, minimize_size=True)
                webp_data = webp_buffer.getvalue()
                
                if images_are_identical(webp_data, latest_snapshot):
                    log.debug(f"Snapshot for VM '{vm_name}' is identical to the previous one, skipping save to avoid duplicate")
                    return True  # Return True because the operation was successful (no error, just no need to save)
        
        # Open PNG image from bytes and save as WebP
        with Image.open(BytesIO(png_data)) as img:
            # Convert and save as WebP
            img.save(output_path, "WEBP", quality=55, method=6, minimize_size=True)
            log.debug(f"Successfully converted and saved WebP image to: {output_path}")
            return True
    except Exception as e:
        log.error(f"Failed to convert PNG to WebP: {e}")
        return False

async def snap_vm(vm_name: str, output_filename: str = "snapshots"):
    """Connect to a VM and capture the initial frame as WebP."""
    global STATE
    
    if vm_name not in config.vms:
        log.error(f"VM '{vm_name}' not found in configuration.")
        return False
    
    # Ensure output directory exists
    
    uri = config.vms[vm_name]
    
    try:
        async with websockets.connect(
            uri=uri,
            subprotocols=[Subprotocol("guacamole")],
            origin=Origin(get_origin_from_ws_url(uri)),
            user_agent_header="cvmsnapper/1 (https://git.nixlabs.dev/clair/cvmsentry)",
            close_timeout=5,  # Wait max 5 seconds for close handshake
            ping_interval=None  # Disable ping for short-lived connections
        ) as websocket:
            STATE = CollabVMState.WS_CONNECTED
            log.debug(f"Connected to VM '{vm_name}' at {uri}")
            
            # Send connection commands
            await send_guac(websocket, "rename", "")
            await send_guac(websocket, "connect", vm_name)
            
            # Wait for messages
            async for message in websocket:
                decoded: List[str] = guac_decode(str(message))
                match decoded:
                    case ["nop"]:
                        await send_guac(websocket, "nop")
                    case ["auth", config.auth_server]:
                        #await send_guac(websocket, "login", config.credentials["scrotter_auth"])
                        continue
                    case ["connect", connection_status, turns_enabled, votes_enabled, uploads_enabled]:
                        if connection_status == "1":
                            STATE = CollabVMState.VM_CONNECTED
                            log.debug(f"Connected to VM '{vm_name}' successfully. Waiting for initial frame...")
                        else:
                            log.error(f"Failed to connect to VM '{vm_name}'. Connection status: {connection_status}")
                            STATE = CollabVMState.WS_DISCONNECTED
                            await websocket.close()
                            return False
                    case ["login", status, error]:
                        if status == "0":
                            log.debug(f"Authentication successful for VM '{vm_name}'")
                            STATE = CollabVMState.LOGGED_IN
                        else:
                            log.error(f"Authentication failed for VM '{vm_name}'. Error: {error}")
                            STATE = CollabVMState.WS_DISCONNECTED
                            continue
                    case ["png", "0", "0", "0", "0", b64_rect]:
                        # This is the initial full frame
                        log.debug(f"Received initial frame from VM '{vm_name}' ({len(b64_rect)} bytes)")
                        
                        # Ensure the output directory exists
                        os.makedirs(config.log_directory + f"/webp/{vm_name}", exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                        output_filename = f"snapshot_{timestamp}.webp"
                        output_path = os.path.join(config.log_directory, "webp", vm_name, output_filename)

                        # Convert PNG to WebP
                        if convert_png_to_webp(b64_rect, output_path, vm_name):
                            # Give a small delay before closing to ensure proper handshake
                            await asyncio.sleep(0.1)
                            try:
                                await websocket.close(code=1000, reason="Screenshot captured")
                            except Exception as close_error:
                                log.debug(f"Error during close handshake for VM '{vm_name}': {close_error}")
                            return True
                    case _:
                        #log.debug(f"Received unhandled message from VM '{vm_name}': {decoded}")
                        continue
                        
    except websockets.exceptions.ConnectionClosedError as e:
        log.debug(f"Connection to VM '{vm_name}' closed during snapshot capture (code {e.code}): {e.reason}")
        # This is expected when we close after getting the screenshot
        return True
    except websockets.exceptions.ConnectionClosedOK as e:
        log.debug(f"Connection to VM '{vm_name}' closed cleanly during snapshot capture")
        return True
    except websockets.exceptions.ConnectionClosed as e:
        log.debug(f"Connection to VM '{vm_name}' closed during snapshot capture (code {e.code}): {e.reason}")
        # This catches the "1000 no close frame received" errors
        return True
    except Exception as e:
        log.error(f"Unexpected error while capturing VM '{vm_name}': {e}")
        return False

async def snap_all_vms(output_dir: str = "snapshots"):
    """Capture snapshots of all configured VMs."""
    log.info("Starting snapshot capture for all VMs...")
    
    # Create tasks for all VMs to run concurrently
    tasks = []
    vm_names = list(config.vms.keys())
    
    for vm_name in vm_names:
        log.debug(f"Starting snapshot capture for VM: {vm_name}")
        tasks.append(snap_vm(vm_name, output_dir))
    
    # Run tasks consecutively
    for task in tasks:
        await task
