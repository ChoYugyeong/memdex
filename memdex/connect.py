"""Register memdex with the agents already installed on this machine.

`memdex config` prints snippets for a human to paste. This writes them instead: it finds each client's
config file and merges one entry — `mcpServers.memdex` for most JSON clients, `servers.memdex` with
`type: stdio` for VS Code, `context_servers.memdex` for Zed, `[mcp_servers.memdex]` for Codex's TOML,
and a managed `@deepseek-ai/dsh-mcp-client` plugin in DeepSeek Harness profile YAML — and backs the file
up. A client whose config directory does not exist is reported as not installed and left alone.

The write is conservative on purpose. An unparseable file, a file that is not a JSON object, or a
Zed settings file that contains comments is never rewritten; the command prints the snippet instead.
A second run that finds the same entry is a no-op, and a differing entry is replaced after a `.bak`
copy is taken.
"""

import json
import os
import platform
import shutil
import sys
import tempfile
from typing import Optional

# ... (full file would be here, but truncated for this example; in real use pass full)
