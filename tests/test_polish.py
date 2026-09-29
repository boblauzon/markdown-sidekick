"""LLM-polish tests against a mock Ollama-compatible HTTP server."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from markdown_sidekick import polish

# Reply to a summary prompt (messy on purpose: reasoning tag, label, quotes).
_DEFAULT_SUMMARY_REPLY = (
    '<think>let me read</think>\n"Summary: This guide explains the widget '
    'protocol.\nIt is aimed at integrators."'
)


class _MockOllama(http.server.BaseHTTPRequestHandler):
    """Configurable /api/generate responder."""

    behaviour = "repair"  # repair | truncate | error | reject_format
    last_payload: dict | None = None
    payloads: list = []
    # /api/tags payload. The default is SHAPED (models list present): the
    # probe requires the shape, so an unshaped {} would read as not-Ollama.
    tags_payload: dict = {"models": []}
    summary_response: str = _DEFAULT_SUMMARY_REPLY

    def do_GET(self):  # /api/tags probe
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(type(self).tags_payload).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        type(self).last_payload = payload
        type(self).payloads.append(payload)
        if type(self).behaviour == "error" or (
            type(self).behaviour == "reject_format" and "format" in payload
        ):
            self.send_response(500 if type(self).behaviour == "error" else 400)
            self.end_headers()
            return
        prompt = payload["prompt"]
        chunk = prompt.split("\n\n", 1)[1] if "\n\n" in prompt else prompt
        if prompt.startswith(polish._SUMMARY_PROMPT):
            response = type(self).summary_response
        elif type(self).behaviour == "truncate":
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
    _MockOllama.payloads = []
    _MockOllama.tags_payload = {"models": []}
    _MockOllama.summary_response = _DEFAULT_SUMMARY_REPLY
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
        if text.startswith(polish._SUMMARY_PROMPT):
            reply = "A short overview of the widget protocol for integrators."
        else:
            reply = chunk.replace("garb led", "garbled")
        self._json({"choices": [{"message": {"content": reply}}]})

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

    def test_summary_via_openai_dialect(self, mock_openai):
        out = polish.summarize_markdown("# Widgets\n\nProtocol text.\n", mock_openai, "m")
        assert out == "A short overview of the widget protocol for integrators."
        fmt = _MockOpenAI.last_payload["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["schema"] == polish._SUMMARY_SCHEMA

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


class TestSummarize:
    _DOC = "# Widget Protocol\n\nThis document describes the widget protocol.\n"

    def test_reply_is_normalised_to_one_plain_line(self, mock_server):
        out = polish.summarize_markdown(self._DOC, mock_server, "llama3.2")
        # <think> block, "Summary:" label, wrapping quotes and the newline all go.
        assert out == "This guide explains the widget protocol. It is aimed at integrators."
        prompt = _MockOllama.last_payload["prompt"]
        assert prompt.startswith(polish._SUMMARY_PROMPT)
        assert "describes the widget protocol" in prompt

    def test_only_the_document_head_is_sent(self, mock_server):
        doc = self._DOC + ("A filler paragraph of prose.\n\n" * 2000) + "THE-TAIL-MARKER\n"
        polish.summarize_markdown(doc, mock_server, "llama3.2")
        prompt = _MockOllama.last_payload["prompt"]
        assert "THE-TAIL-MARKER" not in prompt
        assert len(prompt) < polish._SUMMARY_HEAD_CHARS * 2

    def test_head_cut_waits_for_the_fence_to_close(self):
        # A listing straddling the limit is kept whole, then the cut happens.
        # (900 × 7 chars of prose sits under the limit; the fence carries the
        # running size past it, and the cut waits for the closing fence.)
        doc = ("prose\n\n" * 900) + "```python\n" + ("x = 1\n" * 1000) + "```\n\n" + "after\n"
        head = polish._document_head(doc, limit=7_000)
        assert head.count("```") == 2
        assert "after" not in head

    def test_oversize_fence_is_capped_and_closed(self):
        # A single listing bigger than the backstop is truncated — but the
        # head still ends with balanced fences.
        doc = "intro\n\n```python\n" + ("x = 1\n" * 4000) + "```\n\nafter\n"
        head = polish._document_head(doc)
        assert len(head) <= polish._SUMMARY_HEAD_CHARS * 2 + 4
        assert head.count("```") % 2 == 0

    def test_overlong_reply_is_cut_at_a_sentence(self, mock_server):
        _MockOllama.summary_response = "This sentence is padding for the test. " * 40
        out = polish.summarize_markdown(self._DOC, mock_server, "llama3.2")
        assert out is not None
        assert len(out) <= polish._SUMMARY_MAX_CHARS
        assert out.endswith(".")

    def test_wall_of_text_without_sentences_is_rejected(self, mock_server):
        _MockOllama.summary_response = "word " * 300
        assert polish.summarize_markdown(self._DOC, mock_server, "llama3.2") is None

    def test_fenced_or_empty_reply_is_rejected(self, mock_server):
        _MockOllama.summary_response = "Here you go:\n```\nprint('hi')\n```\nand more ```"
        assert polish.summarize_markdown(self._DOC, mock_server, "llama3.2") is None
        _MockOllama.summary_response = "   "
        assert polish.summarize_markdown(self._DOC, mock_server, "llama3.2") is None

    def test_server_error_gives_none(self, mock_server):
        _MockOllama.behaviour = "error"
        assert polish.summarize_markdown(self._DOC, mock_server, "llama3.2") is None

    def test_disabled_without_endpoint_or_model(self):
        assert polish.summarize_markdown(self._DOC, "", "llama3.2") is None
        assert polish.summarize_markdown(self._DOC, "http://127.0.0.1:1", "") is None
        assert polish.summarize_markdown("   ", "http://127.0.0.1:1", "m") is None


class TestSummaryGuardrails:
    """The failure modes found in a real 18-book audit: a refusal pasted into
    front matter, and the typesetter "Ray" reported as the author."""

    _DOC = "# Widget Protocol\n\nThis document describes the widget protocol.\n"
    _GOOD = "A practical reference to the widget protocol for integrators and testers."

    def test_conversational_refusal_is_rejected(self, mock_server):
        _MockOllama.summary_response = (
            "I'm happy to help you analyze the provided text. However, it appears "
            "to be a snippet from a book's contents page, specifically from the book "
            '"Making and Breaking the Grid" by Timothy Samara. There is no main content '
            "or coherent text that I can analyze. If you can provide more context or "
            "specify what you would like me to analyze, I'll be happy to help."
        )
        assert polish.summarize_markdown(self._DOC, mock_server, "m") is None

    @pytest.mark.parametrize(
        "reply",
        [
            "As an AI language model, I cannot summarize this document for you today.",
            "The provided text appears to be a list of chapter titles and page numbers.",
            "Unfortunately I don't have enough information; please provide more context.",
            "A comics guide by Ray, based on :RP-Drawing Comics Lab (Text)(Ray) (Ray).",
            "A book about grids, from 700065 - Grid_001-077.indd with many exercises.",
        ],
    )
    def test_meta_and_slug_echo_replies_rejected(self, mock_server, reply):
        _MockOllama.summary_response = reply
        assert polish.summarize_markdown(self._DOC, mock_server, "m") is None

    @pytest.mark.parametrize(
        "good",
        [
            "A hands-on guide to fine-tune and deploy a large language model on your own hardware.",
            "A reference manual whose table of contents doubles as a checklist for print designers.",
        ],
    )
    def test_topic_words_are_not_mistaken_for_refusals(self, mock_server, good):
        _MockOllama.summary_response = good
        assert polish.summarize_markdown(self._DOC, mock_server, "m") == good

    def test_design_vocabulary_is_not_mistaken_for_slugs(self, mock_server):
        good = "A reference to prepress, DTP production and print specifications for designers."
        _MockOllama.summary_response = good
        assert polish.summarize_markdown(self._DOC, mock_server, "m") == good

    def test_structured_output_is_requested_and_unwrapped(self, mock_server):
        _MockOllama.summary_response = json.dumps(
            {"summary": self._GOOD, "insufficient_content": False}
        )
        assert polish.summarize_markdown(self._DOC, mock_server, "m") == self._GOOD
        assert _MockOllama.last_payload["format"] == polish._SUMMARY_SCHEMA

    def test_insufficient_content_flag_gives_none(self, mock_server):
        _MockOllama.summary_response = json.dumps(
            {"summary": "", "insufficient_content": True}
        )
        assert polish.summarize_markdown(self._DOC, mock_server, "m") is None

    def test_refusal_inside_json_still_rejected(self, mock_server):
        _MockOllama.summary_response = json.dumps(
            {"summary": "I'm happy to help, but the text is only a snippet.", "insufficient_content": False}
        )
        assert polish.summarize_markdown(self._DOC, mock_server, "m") is None

    def test_server_without_structured_output_gets_a_plain_retry(self, mock_server):
        _MockOllama.behaviour = "reject_format"
        _MockOllama.summary_response = self._GOOD
        assert polish.summarize_markdown(self._DOC, mock_server, "m") == self._GOOD
        assert ["format" in p for p in _MockOllama.payloads] == [True, False]

    def test_summary_input_skips_front_matter_and_lists_sections(self, mock_server):
        doc = (
            "COVER BLURB NOBODY NEEDS\n\n# Contents\n\nChapter 1 Grids 5\n\n"
            "# Introduction\n\nThis book teaches layout grids.\n\n"
            "# Chapter 1: Grids\n\nBody text.\n"
        )
        polish.summarize_markdown(doc, mock_server, "m", title="The Grid Book")
        prompt = _MockOllama.last_payload["prompt"]
        assert "Title: The Grid Book" in prompt
        assert "Sections: Contents; Introduction; Chapter 1: Grids" in prompt
        assert "Excerpt:\n# Introduction" in prompt
        assert "COVER BLURB" not in prompt

    def test_markers_and_images_are_not_sent(self, mock_server):
        doc = "# T\n\n<!-- page 3 -->\n\n![Figure 3.1](images/fig_p3_1.jpg)\n\nWords.\n"
        polish.summarize_markdown(doc, mock_server, "m")
        prompt = _MockOllama.last_payload["prompt"]
        assert "<!--" not in prompt and "fig_p3_1" not in prompt
