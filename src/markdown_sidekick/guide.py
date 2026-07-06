"""Help & support content: the bundled user guide and the AI-setup prompt.

The guide ships as package data (``USERGUIDE.md`` next to this module), which
works identically for a source checkout, a pip install, and a PyInstaller
build — ``importlib.resources`` resolves all three.
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

KOFI_URL = "https://ko-fi.com/roblauzon"


def _mcp_launch_command() -> tuple[str, list[str]]:
    """The (command, args) that start this install's MCP server over stdio."""
    if getattr(sys, "frozen", False):
        # Standalone build: the exe itself hosts the server via --mcp.
        return sys.executable, ["--mcp"]
    run_mcp = Path(__file__).resolve().parents[2] / "run_mcp.py"
    if run_mcp.exists():
        # Source checkout: venv python + the root launcher.
        return sys.executable, [str(run_mcp)]
    # pip/pipx install: run the server module directly.
    return sys.executable, ["-m", "markdown_sidekick.mcp_server"]


def build_mcp_setup_prompt() -> str:
    """A self-contained prompt the user pastes into any AI assistant.

    The launch command/args are detected from *this* running install, so the
    generated config needs no editing by the user or the assistant.
    """
    command, args = _mcp_launch_command()
    config = {
        "mcpServers": {
            "markdown-sidekick": {
                "command": command,
                "args": args,
            }
        }
    }
    config_json = json.dumps(config, indent=2)
    cli_args = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"""Please set up the "Markdown Sidekick" MCP server in my AI client. It converts local files to Markdown on this machine (PDFs including scanned ones via OCR, Office documents, images, and audio transcription), exposing the tools convert_local_file(file_path, clean) and list_capabilities().

It is a stdio MCP server started with:
  command: {command}
  args: {json.dumps(args)}

Set it up for whichever client I am using:

1. Claude Desktop (Windows): merge the following into %APPDATA%\\Claude\\claude_desktop_config.json (create the file if missing, and preserve any existing "mcpServers" entries):

{config_json}

2. Claude Code: run
   claude mcp add markdown-sidekick -- "{command}" {cli_args}

3. Cursor / VS Code: add an MCP server with transport "stdio", using the command and args above, then restart the editor.

After configuring, restart the client and verify by asking it to call list_capabilities on markdown-sidekick. If the tools do not appear, check that the command path above still exists."""


def load_user_guide() -> str:
    """Return the user guide's Markdown text (a short apology if missing)."""
    try:
        return (
            resources.files("markdown_sidekick")
            .joinpath("USERGUIDE.md")
            .read_text(encoding="utf-8")
        )
    except Exception:
        return (
            "# User Guide\n\nThe bundled guide could not be loaded.\n\n"
            f"Documentation and support: {KOFI_URL}\n"
        )
