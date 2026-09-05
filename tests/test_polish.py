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
    # /api/tags payload. The default is SHAPED (models list present): the
    # probe requires the shape, so an unshaped {} would read as not-Ollama.
    tags_payload: dict = {"models": []}

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
    _MockOllama.tags_payload = {"models": []}
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


class TestDetectLocalAi:
    def test_running_with_models(self, mock_server):
        _MockOllama.tags_payload = {
            "models": [{"name": "llama3.2:latest"}, {"name": "llava:7b"}]
        }
        status, models, protocol, endpoint = polish.detect_local_ai(mock_server)
        assert (status, protocol, endpoint) == ("ok", "ollama", mock_server)
        assert models == ["llama3.2:latest", "llava:7b"]

    def test_running_but_no_models_pulled(self, mock_server):
        _MockOllama.tags_payload = {"models": []}
        status, models, protocol, _ = polish.detect_local_ai(mock_server)
        assert (status, models, protocol) == ("empty", [], "ollama")

    def test_nothing_listening(self):
        # A port nothing listens on: bind-then-close guarantees it's free.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert polish.detect_local_ai(f"http://127.0.0.1:{port}", timeout=1) == (
            "unreachable",
            [],
            "",
            "",
        )

    def test_blank_endpoint_probes_both_dialects(self, monkeypatch):
        seen = []

        def fake_urlopen(url, timeout=0):
            seen.append(url)
            raise OSError("no daemon in tests")

        monkeypatch.setattr(polish.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(
            polish, "KNOWN_LOCAL_ENDPOINTS", ("http://127.0.0.1:11434",)
        )
        assert polish.detect_local_ai("")[0] == "unreachable"
        # Native probe plus the OpenAI-compatible fallback, per endpoint.
        assert set(seen) == {
            "http://127.0.0.1:11434/api/tags",
            "http://127.0.0.1:11434/v1/models",
        }

    def test_junk_payload_is_empty_not_crash(self, mock_server):
        _MockOllama.tags_payload = {"models": [{"notname": 1}, {"name": "  "}]}
        status, models, _proto, _ = polish.detect_local_ai(mock_server)
        assert (status, models) == ("empty", [])

    def test_catchall_json_server_reads_unreachable(self, mock_server):
        # A dev server that answers 200 + a JSON object on EVERY path (the
        # mock serves tags_payload for every GET) must not be misread as an
        # empty Ollama — the payload shape is required.
        _MockOllama.tags_payload = {"ok": True, "message": "hello"}
        assert polish.detect_local_ai(mock_server)[0] == "unreachable"


class _MockOpenAI(http.server.BaseHTTPRequestHandler):
    """OpenAI-compatible dialect (LM Studio / Jan / LocalAI): /v1 only."""

    last_payload: dict | None = None
    model_ids: list[str] = ["qwen2.5-7b-instruct"]

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"data": [{"id": m} for m in type(self).model_ids]})
        else:  # no /api/tags — that's the point
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        type(self).last_payload = payload
        content = payload["messages"][0]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        chunk = text.split("\n\n", 1)[1] if "\n\n" in text else text
        self._json(
            {"choices": [{"message": {"content": chunk.replace("garb led", "garbled")}}]}
        )

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_openai():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _MockOpenAI.last_payload = None
    _MockOpenAI.model_ids = ["qwen2.5-7b-instruct"]
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestOpenAICompatible:
    def test_detect_openai_runtime(self, mock_openai):
        status, models, protocol, endpoint = polish.detect_local_ai(mock_openai)
        assert (status, protocol) == ("ok", "openai")
        assert models == ["qwen2.5-7b-instruct"]
        assert endpoint == mock_openai

    def test_detect_prefers_native_ollama(self, mock_server):
        _MockOllama.tags_payload = {"models": [{"name": "llama3.2"}]}
        status, models, protocol, _ = polish.detect_local_ai(mock_server)
        assert (status, protocol) == ("ok", "ollama")

    def test_blank_endpoint_scans_known_ports(self, mock_openai, monkeypatch):
        monkeypatch.setattr(polish, "KNOWN_LOCAL_ENDPOINTS", (mock_openai,))
        status, models, protocol, endpoint = polish.detect_local_ai("")
        assert (status, protocol, endpoint) == ("ok", "openai", mock_openai)

    def test_polish_via_openai_dialect(self, mock_openai):
        text = "Some prose with a garb led word inside a normal paragraph.\n"
        out, changed = polish.polish_markdown(text, mock_openai, "qwen2.5-7b-instruct")
        assert "garbled" in out
        assert changed == 1

    def test_caption_via_openai_sends_data_uri(self, mock_openai, tmp_path):
        img = tmp_path / "fig.png"
        img.write_bytes(b"\x89PNG fake bytes")
        caption = polish.caption_image(img, mock_openai, "qwen2.5-7b-instruct")
        assert caption
        content = _MockOpenAI.last_payload["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_runtime_names(self):
        assert polish.runtime_name("http://localhost:11434", "ollama") == "Ollama"
        assert polish.runtime_name("http://localhost:1234", "openai") == "LM Studio"
        assert polish.runtime_name("http://localhost:1337", "openai") == "Jan"
        assert polish.runtime_name("http://localhost:8080", "openai") == "LocalAI"
        # Exact port match: :12345 must NOT read as LM Studio's :1234.
        assert (
            polish.runtime_name("http://gpubox:12345", "openai")
            == "OpenAI-compatible local AI"
        )
