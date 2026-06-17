"""Opt-in, localhost-only HTTP control server that lets an external agent drive the
live AnimGen GUI (screenshot / widget snapshot / click / type / key / set) the way the
Chrome MCP drives a web page. Off by default; enabled via the ANIMGEN_REMOTE env var.
See remote/server.py for the endpoint list."""
