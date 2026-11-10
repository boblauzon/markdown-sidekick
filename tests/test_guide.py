"""The bundled user guide and the paste-into-your-AI setup prompt."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from markdown_sidekick import guide


def _config(prompt: str) -> dict:
    """The JSON block the prompt tells the AI to merge."""
    start = prompt.index('{\n  "mcpServers"')
    return json.JSONDecoder().raw_decode(prompt[start:])[0]


class TestLaunchCommand:
    def test_a_source_checkout_runs_the_root_launcher(self):
        command, args = guide._mcp_launch_command()
        assert command == sys.executable
        assert [Path(a).name for a in args] == ["run_mcp.py"] and Path(args[0]).is_file()

    def test_the_standalone_exe_hosts_its_own_server(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\Users\Ann Lee\AppData\Local\Programs\Markdown Sidekick\MarkdownSidekick.exe")
        assert guide._mcp_launch_command() == (sys.executable, ["--mcp"])

    def test_a_pip_install_runs_the_module(self, monkeypatch, tmp_path):
        fake = tmp_path / "site-packages" / "markdown_sidekick" / "guide.py"  # no run_mcp.py two folders up
        monkeypatch.setattr(guide, "__file__", str(fake))
        assert guide._mcp_launch_command() == (sys.executable, ["-m", "markdown_sidekick.mcp_server"])


class TestSetupPrompt:
    def test_the_config_is_valid_json_for_this_install(self):
        command, args = guide._mcp_launch_command()
        server = _config(guide.build_mcp_setup_prompt())["mcpServers"]["markdown-sidekick"]
        assert server == {"command": command, "args": args}

    def test_a_windows_path_with_spaces_survives(self, monkeypatch):
        """Backslashes must be escaped in the JSON and the path quoted on the command line."""
        exe = r"C:\Users\Ann Lee\AppData\Local\Programs\Markdown Sidekick\MarkdownSidekick.exe"
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", exe)
        prompt = guide.build_mcp_setup_prompt()
        assert _config(prompt)["mcpServers"]["markdown-sidekick"] == {"command": exe, "args": ["--mcp"]}
        assert f'claude mcp add markdown-sidekick -- "{exe}" --mcp' in prompt

    def test_an_argument_with_spaces_is_quoted_for_the_command_line(self, monkeypatch):
        launcher = r"C:\My Apps\Markdown Sidekick\run_mcp.py"
        monkeypatch.setattr(guide, "_mcp_launch_command", lambda: (r"C:\py\python.exe", [launcher]))
        prompt = guide.build_mcp_setup_prompt()
        assert f'-- "C:\\py\\python.exe" "{launcher}"' in prompt
        assert _config(prompt)["mcpServers"]["markdown-sidekick"]["args"] == [launcher]

    def test_it_names_each_client_and_how_to_check(self):
        prompt = guide.build_mcp_setup_prompt()
        for needle in ("Claude Desktop", "claude_desktop_config.json", "Claude Code", "Cursor", "list_capabilities"):
            assert needle in prompt


class TestUserGuide:
    def test_the_bundled_guide_loads(self):
        text = guide.load_user_guide()
        assert text.startswith("#") and len(text) > 2000 and "Error codes" in text

    def test_its_links_sit_outside_bold(self):
        """The classic preview doesn't parse a link inside ``**bold**``, so one
        there would show as raw brackets in the in-app guide."""
        inside = re.findall(r"\*\*[^*\n]*\]\(https?://[^*\n]*\*\*", guide.load_user_guide())
        assert inside == []

    def test_a_missing_guide_is_reported_not_raised(self, monkeypatch):
        def gone(package):
            raise FileNotFoundError("USERGUIDE.md")

        monkeypatch.setattr(guide.resources, "files", gone)
        text = guide.load_user_guide()
        assert text.startswith("# User Guide") and "MS-604" in text and guide.KOFI_URL in text
