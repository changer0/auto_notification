import os
from pathlib import Path
MCP_PUBLIC_ORIGIN = os.environ.get("EMAIL_PUBLIC_ORIGIN", "https://email.luxinyi.top").rstrip("/")
MCP_OAUTH_STATE_FILE = Path(os.environ.get("EMAIL_STATE_DIR", str(Path.home() / ".local/state/berry-email"))) / "oauth-state.json"
