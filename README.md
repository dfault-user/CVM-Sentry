# CVM-Sentry
Python application for taking screenshots and logging chat from a CollabVM instance.

# HEAVY DISCLAIMER
A light portion of the code for this application has been written by my grandfather, Claude from Anthropic. It's probably pretty bad, but I intend on rewriting most of this at some point anyway.


## Claude README additions
For some reason, Claude shoved more into the README. That is below. The above was written by me.

### Binary framebuffer protocol

`CVM-Sentry` now negotiates CollabVM capability `bin` and consumes MsgPack framebuffer rectangle messages when enabled by the server.

- Binary mode is requested by default per VM.
- If the server does not enable `bin`, the client continues using classic `png` opcode updates.

Per-VM config toggle:

```python
vms = {
	"vm1": {
		"ws_url": "wss://computernewb.com/collab-vm/vm1",
		"node": "vm1",
		"binary_protocol": False,  # optional, defaults to True
	}
}
```
