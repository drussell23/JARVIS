"""Served identity: the cockpit says WHO answers -- engine, adapter, vision.

A new adapter keeps the same model tag, so the name alone could never show
that the organism had learned; and the adapter training counts against must
be the adapter the operator is shown (one decision, two readers).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.core.ouroboros.governance import served_identity as si
from backend.core.ouroboros.governance.observability import training_handoff as th

SEPT = 1788647690.0
OCT = 1791386447.0


class _Engine:
    def __init__(self, *, jprime=True, versions=None, adapters=None, ollama_version="0.35.1"):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health" and jprime:
                    return self._json(200, {"service": "jarvis_prime", "version": "0.1.0"})
                if self.path == "/api/version":
                    return self._json(200, {"version": ollama_version})
                if self.path.startswith("/v1/adapters/") and jprime:
                    return self._json(200, versions)
                return self._json(404, {"error": "nf"})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/show":
                    return self._json(200, {"adapters": adapters or []})
                return self._json(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PROBE_TIMEOUT_S", "2")
    yield
    si.set_current({})


ORIGIN = {"active": "origin", "versions": [{"version": "origin", "source": {"from": "ollama-store"}}]}
TRAINED = {"active": "20261007-150000-abc", "versions": [
    {"version": "origin", "source": {"from": "ollama-store"}},
    {"version": "20261007-150000-abc", "published_at": OCT, "source": {"trained_through": OCT - 600}}]}


# ------------------------------------------------------------------ the shared decision

def test_a_published_version_names_its_own_evidence_cutoff():
    p = si.adapter_provenance(TRAINED, [{"mtime": SEPT}])
    assert (p.version, p.trained_through, p.source) == ("20261007-150000-abc", OCT - 600,
                                                        "registry:20261007-150000-abc")


def test_a_pre_registry_adapter_is_bounded_by_its_file_mtime():
    p = si.adapter_provenance(ORIGIN, [{"mtime": SEPT}])
    assert (p.version, p.trained_through, p.source) == ("origin", SEPT, "adapter_file_mtime")


def test_no_adapter_is_base_weights():
    assert si.adapter_provenance(ORIGIN, []).source == "no_adapter"
    assert si.adapter_provenance(None, []).source == "no_registry"


def test_an_unreadable_cutoff_fails_closed():
    with pytest.raises(si.AdapterCutoffUnknown):
        si.adapter_provenance(ORIGIN, [{"name": "x"}])


def test_training_counts_against_the_adapter_the_cockpit_shows(monkeypatch):
    calls = {"GET": TRAINED, "POST": {"adapters": [{"mtime": SEPT}]}}

    async def http(method, path, **kw):
        return calls[method]
    monkeypatch.setattr(th, "_http", http)
    through, source = asyncio.run(th._trained_through("qwen3-coder-ov:30b"))
    shown = si.adapter_provenance(TRAINED, [{"mtime": SEPT}])
    assert (through, source) == (shown.trained_through, shown.source)


# ------------------------------------------------------------------ the engines, live HTTP

def test_jprime_is_identified_with_its_adapter():
    e = _Engine(versions=ORIGIN, adapters=[{"mtime": SEPT}])
    ident = si.resolve(base=e.url, model="qwen3-coder-ov:30b")
    assert ident["engine"]["name"] == "J-Prime" and ident["adapter"]["version"] == "origin"
    assert si.short_label(ident) == "J-Prime/origin"
    line = si.describe_line(ident)
    assert line.startswith("qwen3-coder-ov:30b via J-Prime 0.1.0 · adapter origin (learned through ")


def test_ollama_is_named_as_ollama_and_vision_is_named_where_it_runs():
    jp = _Engine(versions=TRAINED, adapters=[{"mtime": SEPT}])
    ol = _Engine(jprime=False)
    ident = si.resolve(base=jp.url, model="qwen3-coder-ov:30b",
                       vision_base=ol.url + "/v1", vision_model="jarvis-vision:8b")
    assert ident["vision"]["engine"]["name"] == "Ollama"
    assert "vision jarvis-vision:8b via Ollama 0.35.1" in si.describe_line(ident)
    assert si.short_label(ident) == "J-Prime/20261007-150000-abc"


def test_an_unreachable_engine_is_named_unidentified_never_guessed():
    ident = si.resolve(base="http://127.0.0.1:9", model="m")
    assert "an unidentified engine" in si.describe_line(ident)
    assert "adapter" not in ident and si.short_label(ident) == ""


def test_an_unknowable_adapter_is_shown_as_unknown():
    e = _Engine(versions=ORIGIN, adapters=[{"name": "no-mtime"}])
    ident = si.resolve(base=e.url, model="m")
    assert si.short_label(ident) == "J-Prime/adapter?" and "adapter unknown" in si.describe_line(ident)


# ------------------------------------------------------------------ the cockpit

def test_the_hydration_frame_carries_the_boot_resolved_identity():
    from backend.core.ouroboros.battle_test import cockpit_attach as ca
    ident = {"model": "qwen3-coder-ov:30b", "engine": {"name": "J-Prime", "version": "0.1.0", "url": "u"},
             "adapter": {"version": "origin", "trained_through": SEPT, "source": "adapter_file_mtime"}}
    si.set_current(ident)
    assert ca.CockpitAttachBridge._served_identity() == ident
    src = open(ca.__file__, encoding="utf-8").read()
    assert '"serving": _safe(self._served_identity, {})' in src


def test_the_toolbar_names_engine_and_adapter_after_the_model():
    from backend.core.ouroboros.cli import ov
    ui = ov.AttachUI()
    ui.set_model("qwen3-coder-ov:30b")
    ui.set_serving({"engine": {"name": "J-Prime"}, "adapter": {"version": "origin"}})
    from backend.core.ouroboros.ui.markup_ansi import markup_to_plain
    hints = markup_to_plain(ui._key_hints())
    assert hints.startswith("qwen3-coder-ov:30b J-Prime/origin · ")
    ui.set_serving({})                                       # an empty frame keeps the last identity
    assert "J-Prime/origin" in markup_to_plain(ui._key_hints())
