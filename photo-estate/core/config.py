"""The default ports, one place for every script (standard library only). Shell scripts and launchd
plists cannot import this; tests/test_core.py checks that the ones that name a port agree with it.

  REAL      8766  the photo app on a real library (`embed.py serve`)
  DEMO      8767  the demo library (`embed.py serve --demo`), so it can run beside the real one
  PREVIEW   8768  the cloud preview on the Mac mini (deploy/preview-branch.sh), published on the tailnet
  SANDBOX   8769  `embed.py serve --sandbox`: a scratch library with practice sources
  REVIEW    8765  `estate.py review`, the near-duplicate page
  FACES     8764  `faces.py serve`, the naming page on its own

  app_port(port, demo=False, sandbox=False)   the photo app's port: --port, else the default for the mode
  mcp_url(port)                               the app's own MCP endpoint, which a task's agent calls (tasks.py)

Before #73, --sandbox and `faces.py serve` both defaulted to 8768, the preview's port.
"""
from __future__ import annotations

REAL = 8766
DEMO = 8767
PREVIEW = 8768
SANDBOX = 8769
REVIEW = 8765
FACES = 8764

PORTS = {"real": REAL, "demo": DEMO, "preview": PREVIEW, "sandbox": SANDBOX, "review": REVIEW, "faces": FACES}


def app_port(port: int | None = None, demo: bool = False, sandbox: bool = False) -> int:
    return port or (DEMO if demo else SANDBOX if sandbox else REAL)


def mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"
