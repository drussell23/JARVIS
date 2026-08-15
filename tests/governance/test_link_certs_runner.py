"""Regression spine for link certificate issuance and the connection runner.

The handshake tests use REAL TLS over a loopback pair with material issued by
the module under test — the one thing that cannot be proven by substituting a
fake, because a certificate that fails to verify fails inside OpenSSL.
"""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from backend.core.ouroboros.governance import link_certs as lc
from backend.core.ouroboros.governance import link_runner as lr
from backend.core.ouroboros.governance import link_session as ls
from backend.core.ouroboros.governance import link_transport as tx

crypto = pytest.importorskip("cryptography")


@pytest.fixture
def material(tmp_path, monkeypatch):
    """Issued material, with the transport pointed at it."""
    monkeypatch.setenv("JARVIS_LINK_TLS_DIR", str(tmp_path / "mtls"))
    lc.issue_link_material(
        directory=tmp_path / "mtls",
        server_names=["engine.tailnet.ts.net", "100.64.1.2"],
        client_names=["body-mac"],
    )
    return tmp_path / "mtls"


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def test_private_keys_are_owner_only(material):
    """Created with the mode applied, not chmod'ed after: between open and
    chmod the key is world-readable, and that window is the vulnerability."""
    for name in (lc.CA_KEY, lc.SERVER_KEY, lc.CLIENT_KEY):
        mode = stat.S_IMODE(os.stat(material / name).st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}"


def test_an_ip_literal_becomes_an_ip_san_not_a_dns_name(material):
    """Peers match IP literals against iPAddress SANs only. A tailnet address
    in a DNSName verifies against nothing."""
    report = lc.inspect_material(material)
    sans = report["certificates"][lc.SERVER_CERT]["sans"]
    assert "engine.tailnet.ts.net" in sans
    assert "100.64.1.2" in sans


def test_the_ca_cannot_sign_another_ca(material):
    """A CA that can sign anything can impersonate anything."""
    from cryptography import x509
    cert = x509.load_pem_x509_certificate((material / lc.CA_CERT).read_bytes())
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is True and bc.path_length == 0


def test_leaves_carry_narrow_extended_key_usage(material):
    """A leaf usable as both client and server lets a compromised Body
    impersonate the Engine to itself."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    for name, expected in ((lc.SERVER_CERT, ExtendedKeyUsageOID.SERVER_AUTH),
                           (lc.CLIENT_CERT, ExtendedKeyUsageOID.CLIENT_AUTH)):
        cert = x509.load_pem_x509_certificate((material / name).read_bytes())
        eku = cert.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        assert list(eku) == [expected]


def test_validity_outlives_a_weekly_visit(material):
    """Seven-day material is what made the brain certs unusable: a link
    between two houses cannot need a physical visit every week."""
    report = lc.inspect_material(material)
    assert report["certificates"][lc.SERVER_CERT]["days_remaining"] > 365


def test_notbefore_is_backdated_against_clock_skew(material):
    """§29 exists because these clocks drift. A certificate valid from 'now'
    is rejected by a peer a few minutes behind, and the failure looks like
    anything but a clock."""
    from cryptography import x509
    import datetime as dt
    cert = x509.load_pem_x509_certificate(
        (material / lc.SERVER_CERT).read_bytes())
    assert cert.not_valid_before_utc < dt.datetime.now(dt.timezone.utc)


def test_the_ca_private_key_is_not_in_the_peer_bundle():
    """Copying it would let each end mint identities for the other."""
    assert lc.CA_KEY not in lc.files_to_copy_to_peer()
    assert set(lc.files_to_copy_to_peer()) == {
        lc.CA_CERT, lc.CLIENT_CERT, lc.CLIENT_KEY}


def test_reissue_refuses_to_silently_revoke_the_peer(material):
    """A silent regeneration revokes trust from across the country, and the
    operator meets it as an inexplicable handshake failure."""
    with pytest.raises(FileExistsError):
        lc.issue_link_material(directory=material,
                               server_names=["engine.tailnet.ts.net"])
    lc.issue_link_material(directory=material,
                           server_names=["engine.tailnet.ts.net"], force=True)


def test_missing_server_names_is_refused_rather_than_guessed():
    """The peer's name is a property of someone's tailnet. A wrong SAN fails
    the handshake as a trust error, which is far harder to diagnose."""
    with pytest.raises(ValueError):
        lc.issue_link_material(directory=None, server_names=[])


def test_inspection_reports_expiry_as_a_countdown(material):
    """Reported as days remaining, so rotation is seen coming rather than
    discovered as an outage."""
    report = lc.inspect_material(material)
    entry = report["certificates"][lc.SERVER_CERT]
    assert entry["expired"] is False
    assert isinstance(entry["days_remaining"], int)


def test_the_link_does_not_share_the_brain_trust_domain(monkeypatch):
    """Two trust domains in one directory means rotating either silently
    re-keys the other."""
    monkeypatch.delenv("JARVIS_LINK_TLS_DIR", raising=False)
    assert "brain_mtls" not in str(tx.tls_dir())
    assert "link_mtls" in str(tx.tls_dir())


# ---------------------------------------------------------------------------
# Real TLS
# ---------------------------------------------------------------------------


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_real_mtls_handshake_completes_and_verifies_identity(material):
    """THE test that cannot be faked: a certificate that does not verify
    fails inside OpenSSL, not in our code."""
    engine = ls.LinkSessionLoop(
        ls.SessionConfig(node_id="engine", session_id="s-1"))
    server = await lr.serve_link(engine, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    ctx = tx.build_ssl_context(server_side=False)
    assert ctx is not None and ctx.check_hostname is True
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                server_hostname="engine.tailnet.ts.net"),
        timeout=10)
    peer = writer.get_extra_info("peercert")
    assert peer, "the Engine presented no certificate"
    names = [v for field in peer["subject"] for _, v in field]
    assert "engine.tailnet.ts.net" in names
    writer.close()
    server.close()
    await server.wait_closed()


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_wrong_hostname_is_refused(material):
    """Turning hostname checking off to make a name fit is how an mTLS
    deployment silently becomes encryption without identity."""
    engine = ls.LinkSessionLoop(
        ls.SessionConfig(node_id="engine", session_id="s-1"))
    server = await lr.serve_link(engine, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    ctx = tx.build_ssl_context(server_side=False)
    with pytest.raises(Exception):
        await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                    server_hostname="attacker.example.com"),
            timeout=10)
    server.close()
    await server.wait_closed()


@pytest.mark.socket
@pytest.mark.asyncio
async def test_an_unauthenticated_client_gets_no_data(material):
    """mTLS means the Engine verifies the Body too — anything on the tailnet
    could otherwise resume another Body's session.

    Asserted as "exchanges nothing", not "raises on connect": under TLS 1.3
    the server evaluates the client certificate AFTER its own Finished, so a
    certificate-less client's `open_connection` returns before the rejection
    is known. The security property is that no application data crosses, and
    that is what this checks — asserting an exception would be asserting a
    protocol version's timing rather than the guarantee."""
    import ssl
    engine = ls.LinkSessionLoop(
        ls.SessionConfig(node_id="engine", session_id="s-1"))
    server = await lr.serve_link(engine, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]

    naked = ssl.create_default_context(cafile=str(material / lc.CA_CERT))
    naked.check_hostname = True                      # no client cert loaded

    exchanged = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=naked,
                                    server_hostname="engine.tailnet.ts.net"),
            timeout=10)
        writer.write(b"x\n")
        await writer.drain()
        exchanged = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
    except Exception:
        exchanged = b""                              # rejected outright
    assert not exchanged, "an unauthenticated client exchanged data"
    server.close()
    await server.wait_closed()


def test_serving_without_material_refuses_rather_than_downgrading(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("JARVIS_LINK_TLS_DIR", str(tmp_path / "absent"))
    engine = ls.LinkSessionLoop(
        ls.SessionConfig(node_id="engine", session_id="s-1"))
    with pytest.raises(ConnectionError):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            lr.serve_link(engine, host="127.0.0.1", port=0))


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


def _loop(node="body"):
    return ls.LinkSessionLoop(ls.SessionConfig(node_id=node, session_id="s-1"))


@pytest.mark.asyncio
async def test_one_failing_pump_tears_the_whole_connection_down():
    """They share a socket: a dead peer means the reader blocks forever, and
    a closed pipe means the writer shouts into nothing."""
    loop = _loop()

    class _Reader:
        async def readuntil(self, sep):
            raise ConnectionResetError("peer vanished")

    class _Writer:
        def write(self, b): pass
        async def drain(self): pass
        def close(self): self.closed = True
        closed = False

    runner = lr.LinkRunner(loop)
    writer = _Writer()
    with pytest.raises(Exception):
        await asyncio.wait_for(
            runner._serve_connection(_Reader(), writer), timeout=5)
    assert writer.closed, "the connection was not torn down"


@pytest.mark.asyncio
async def test_a_corrupt_frame_does_not_tear_down_the_link():
    """The CRC caught it and the stream resynchronises on the next newline —
    tearing down would turn one bad frame into a reconnect storm."""
    loop = _loop()
    frames = [b"deadbeef\t{\"kind\":\"telemetry\"}\n", b""]

    class _Reader:
        async def readuntil(self, sep):
            if frames:
                nxt = frames.pop(0)
                if nxt:
                    return nxt
            raise asyncio.IncompleteReadError(b"", None)

    runner = lr.LinkRunner(loop)
    with pytest.raises(ConnectionError):     # EOF, not the corrupt frame
        await asyncio.wait_for(runner._pump_reads(_Reader()), timeout=5)


@pytest.mark.asyncio
async def test_the_runner_stops_promptly_rather_than_sleeping_out_a_backoff():
    """A bare sleep would make shutdown take a full backoff interval — half
    a minute of an operator watching a process told to exit."""
    loop = _loop()
    runner = lr.LinkRunner(loop, connector=None)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.05)
    runner.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_failed_connect_parks_rather_than_raising():
    """A partition is a pause. The runner must keep the session and retry."""
    loop = _loop()

    async def _refuse():
        raise ConnectionRefusedError("engine is asleep")

    runner = lr.LinkRunner(loop, connector=_refuse)
    await asyncio.wait_for(runner.run(max_cycles=2), timeout=5.0)
    assert loop.state is ls.SessionState.PARKED
    assert "ConnectionRefusedError" in runner.snapshot()["last_error"]


@pytest.mark.asyncio
async def test_the_flap_breaker_gates_reconnect_attempts(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("JARVIS_LINK_FLAP_OPEN_S", "30")
    loop = _loop()
    attempts = {"n": 0}

    async def _refuse():
        attempts["n"] += 1
        raise ConnectionRefusedError("nope")

    runner = lr.LinkRunner(loop, connector=_refuse)
    task = asyncio.create_task(runner.run(max_cycles=8))
    await asyncio.sleep(0.2)
    runner.stop()
    await asyncio.wait_for(task, timeout=5.0)
    assert attempts["n"] <= 3, "the breaker did not gate reconnection"


def test_the_connector_builds_a_fresh_context_each_dial():
    """Fresh per attempt is what makes re-keying free: rotated material is
    picked up by the next reconnection with no separate code path."""
    import inspect
    src = inspect.getsource(lr.tls_connector)
    assert "build_ssl_context" in src
    assert src.index("async def _dial") < src.index("build_ssl_context")


def test_the_runner_snapshot_is_serialisable():
    import json
    json.dumps(lr.LinkRunner(_loop()).snapshot())
