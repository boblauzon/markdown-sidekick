"""Launch the Markdown Sidekick MCP server (stdio) for Claude Desktop / Cursor / VS Code.

Point your MCP client at:
    command: <project>\\.venv\\Scripts\\python.exe
    args:    ["<project>\\run_mcp.py"]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from markdown_sidekick.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
