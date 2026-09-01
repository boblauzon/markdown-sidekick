"""LLM-polish tests against a mock Ollama-compatible HTTP server."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from markdown_sidekick import polish


class _MockOllama(http.server.BaseHTTPRequestHandler):
    """Configurable /api/generate responder."""

    behaviour = "repair"  # repair | truncate | error
    last_payload: dict | None = None
    tags_payload: dict = {}  # what /api/tags returns (detection tests set this)

    def do_GET(self):  # /api/tags probe
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).tags_payload).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        type(self).last_payload = payload
        if type(self).behaviour == "error":
            self.send_response(500)
            self.end_headers()
            return
        prompt = payload["prompt"]
        chunk = prompt.split("\n\n", 1)[1] if "\n\n" in prompt else prompt
        if type(self).behaviour == "truncate":
            response = "way too short"
        else:
            response = chunk.replace("garb led", "garbled")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"response": response}).encode("utf-8"))

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture()
def mock_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _MockOllama.behaviour = "repair"
    _MockOllama.last_payload = None
    _MockOllama.tags_payload = {}
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestPolish:
    def test_repairs_artifact(self, mock_server):
        text = "Some prose with a garb led word inside a normal paragraph.\n"
        out, changed = polish.polish_markdown(text, mock_server, "test-model")
        assert "garbled" in out
        assert changed == 1

    def test_size_guardrail_keeps_original(self, mock_server):
        _MockOllama.behaviour = "truncate"
        text = "A paragraph that the model tries to shrink drastically. " * 10 + "\n"
        out, changed = polish.polish_markdown(text, mock_server, "m")
        assert out == text
        assert changed == 0

    def test_server_error_keeps_original(self, mock_server):
        _MockOllama.behaviour = "error"
        text = "Original stays on failure.\n"
        out, changed = polish.polish_markdown(text, mock_server, "m")
        assert out == text and changed == 0

    def test_disabled_without_endpoint(self):
        out, changed = polish.polish_markdown("text\n", "", "m")
        assert out == "text\n" and changed == 0

    def test_llm_available(self, mock_server):
        assert polish.llm_available(mock_server)
        assert not polish.llm_available("http://127.0.0.1:9")  # nothing listening
        assert not polish.llm_available("")


class TestChunking:
    def test_never_splits_inside_fence(self):
        fenced = "```python\n" + ("x = 1\n" * 50) + "\n" + ("y = 2\n" * 50) + "```\n"
        text = ("prose line\n\n" * 10) + fenced + ("more prose\n\n" * 10)
        chunks = polish._split_chunks(text, target=200)
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0, "chunk boundary landed inside a fence"

    def test_roundtrip_preserves_text(self):
        text = "a\n\nb\n\nc\n" * 100
        assert "\n".join(polish._split_chunks(text, target=50)) == text


class TestCaption:
    def test_caption_sends_image(self, mock_server, tmp_path):
        img = tmp_path / "fig.png"
        img.write_bytes(b"\x89PNG fake bytes")
        _MockOllama.behaviour = "repair"
        caption = polish.caption_image(img, mock_server, "vision-model")
        assert caption  # mock echoes the prompt text back
        assert _MockOllama.last_payload is not None
        assert _MockOllama.last_payload.get("images"), "image not attached"

    def test_caption_missing_file(self, mock_server):
        assert polish.caption_image("Z:/nope.png", mock_server, "m") is None


class TestDetectOllama:
    def test_running_with_models(self, mock_server):
        from markdown_sidekick.polish import detect_ollama

        _MockOllama.tags_payload = {
            "models": [{"name": "llama3.2:latest"}, {"name": "llava:7b"}]
        }
        status, models = detect_ollama(mock_server)
        assert status == "ok"
        assert models == ["llama3.2:latest", "llava:7b"]

    def test_running_but_no_models_pulled(self, mock_server):
        from markdown_sidekick.polish import detect_ollama

        _MockOllama.tags_payload = {"models": []}
        assert detect_ollama(mock_server) == ("empty", [])

    def test_nothing_listening(self):
        from markdown_sidekick.polish import detect_ollama

        # A port nothing listens on: bind-then-close guarantees it's free.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert detect_ollama(f"http://127.0.0.1:{port}", timeout=1) == (
            "unreachable",
            [],
        )

    def test_blank_endpoint_uses_default(self, monkeypatch):
        from markdown_sidekick import polish

        seen = []

        def fake_urlopen(url, timeout=0):
            seen.append(url)
            raise OSError("no daemon in tests")

        monkeypatch.setattr(polish.urllib.request, "urlopen", fake_urlopen)
        polish.detect_ollama("")
        assert seen == ["http://localhost:11434/api/tags"]

    def test_junk_payload_is_empty_not_crash(self, mock_server):
        from markdown_sidekick.polish import detect_ollama

        _MockOllama.tags_payload = {"models": [{"notname": 1}, {"name": "  "}]}
        assert detect_ollama(mock_server) == ("empty", [])
