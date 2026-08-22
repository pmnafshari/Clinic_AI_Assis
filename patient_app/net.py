"""resolves the address behind a patient request.

the patient app sits behind cloudflared, which runs on this same host and
connects over loopback. so request.remote_addr is 127.0.0.1 for every patient
once the tunnel is open - which breaks the §5.5 audit row (it records the
tunnel, not the patient) and collapses patient_auth's per-source login throttle
into one shared bucket for everybody.

this module is the single place that decides what an address means. §5.2's rule
that isolation is never done by header inspection still stands and is untouched:
isolation is the separate socket. attribution cannot be done by socket behind a
proxy, so it is done here, behind three gates.
"""

import ipaddress
import os
import sys

TRUST_ENV_VAR = "PATIENT_TRUST_FORWARDED_IP"

# cloudflare sets this to a single client ip on every request through its edge,
# overwriting whatever the client sent, so a remote caller cannot forge it.
# x-forwarded-for is an appended list and would need trusted-hop counting - the
# usual off-by-one in this kind of fix - so it is deliberately not read here.
FORWARDED_HEADER = "CF-Connecting-IP"

LOOPBACK = frozenset({"127.0.0.1", "::1"})

_OFF = frozenset({"", "0", "false", "no", "off"})


def trust_enabled(env=None):
    # default off, and written so that unset means off rather than a typo
    # meaning on. a host with no tunnel in front must never honour the header:
    # any local caller could then choose its own address and its own throttle
    # bucket, which is worse than the collapse this module exists to fix.
    if env is None:
        env = os.environ
    return env.get(TRUST_ENV_VAR, "").strip().lower() not in _OFF


def client_ip(remote_addr, forwarded_value, trusted):
    # pure - no environment read, no request object. all four gates must pass
    # before the header is believed.
    if not trusted:
        return remote_addr

    # this also covers remote_addr=None: None is not loopback, so a request with
    # no reported peer returns None and never reaches the header path. asserted
    # in the selftest rather than left as an accident of ordering.
    if remote_addr not in LOOPBACK:
        return remote_addr

    if not forwarded_value:
        return remote_addr

    candidate = forwarded_value.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return remote_addr

    return candidate


def from_request(request):
    # the whole wiring surface. every route uses this, so a new route cannot get
    # attribution wrong by forgetting to.
    return client_ip(
        request.remote_addr,
        request.headers.get(FORWARDED_HEADER),
        trust_enabled(),
    )


def selftest():
    import ast
    from pathlib import Path

    patient = "203.0.113.7"

    # 1. trust off - the header is ignored even from a loopback peer
    assert client_ip("127.0.0.1", patient, False) == "127.0.0.1", \
        "1: with trust off the forwarded header must be ignored"

    # 2. trust on, loopback peer, valid header - the patient's address wins
    assert client_ip("127.0.0.1", patient, True) == patient, \
        "2: trust on + loopback peer should honour the forwarded header"
    assert client_ip("::1", "2001:db8::5", True) == "2001:db8::5", \
        "2: the ipv6 loopback is a tunnel peer too"

    # 3. trust on but the peer is NOT loopback - the header is a forgery
    # attempt from something that did not come through cloudflared
    assert client_ip("198.51.100.9", patient, True) == "198.51.100.9", \
        "3: a forwarded header from a non-loopback peer must be ignored"

    # 4. trust on, loopback peer, unparseable header - fall back, never store
    # an attacker-chosen string in the audit row or the throttle key
    for junk in ("not-an-ip", "127.0.0.1; DROP", "999.999.999.999", "   "):
        assert client_ip("127.0.0.1", junk, True) == "127.0.0.1", \
            f"4: a header that is not an ip address must fall back, got {junk!r}"

    # 5. no header at all - falls back in both trust states
    assert client_ip("127.0.0.1", None, True) == "127.0.0.1", \
        "5: a missing header must fall back to the peer address"
    assert client_ip("127.0.0.1", None, False) == "127.0.0.1", \
        "5: a missing header must fall back with trust off too"

    # 6. no reported peer - returns None rather than raising. the ip=None
    # positional default from 19-03 is what eval_chat.py and the selftest
    # negative control rely on, so it has to survive this module.
    assert client_ip(None, None, False) is None, "6: a None peer must stay None"
    assert client_ip(None, patient, True) is None, \
        "6: a None peer must not be overridden by a header"

    # 7. the flag defaults to off, and only explicit off-values read as off
    assert trust_enabled({}) is False, "7: unset must default to trust off"
    for off in ("", "0", "false", "FALSE", "no", "off", "  off  "):
        assert trust_enabled({TRUST_ENV_VAR: off}) is False, \
            f"7: {off!r} must read as trust off"
    for on in ("1", "true", "yes"):
        assert trust_enabled({TRUST_ENV_VAR: on}) is True, \
            f"7: {on!r} must read as trust on"

    # 8. the real environment is not consulted by the pure core - client_ip
    # takes trust as an argument precisely so the gate cannot drift
    saved = os.environ.pop(TRUST_ENV_VAR, None)
    try:
        os.environ[TRUST_ENV_VAR] = "1"
        assert client_ip("127.0.0.1", patient, False) == "127.0.0.1", \
            "8: client_ip must not read the environment behind its own argument"
    finally:
        if saved is None:
            os.environ.pop(TRUST_ENV_VAR, None)
        else:
            os.environ[TRUST_ENV_VAR] = saved

    # 9. the import graph never reaches the staff app or the storage layer -
    # this module runs on every patient request and must stay tiny
    source = Path(__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden_imports = {"storage", "ask", "app", "run", "web_auth", "web_session", "flask"}
    assert not (forbidden_imports & imported), \
        f"9: patient_app/net.py must not import {forbidden_imports & imported}"

    # 10. x-forwarded-for is never read on the request path, only explained in
    # a comment. scoped to the code outside selftest/main - a whole-file scan
    # would trip on this assertion's own source, and a guard that fails on
    # itself proves nothing about the path under test.
    lines = source.splitlines(keepends=True)
    skip = [
        (node.lineno - 1, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"selftest", "main"}
    ]
    assert len(skip) == 2, f"10: expected to exclude exactly selftest and main, got {len(skip)}"
    scannable = "".join(
        line for i, line in enumerate(lines)
        if not any(start <= i < end for start, end in skip)
    )
    assert 'get("X-Forwarded-For")' not in scannable, \
        "10: the request path must not read X-Forwarded-For"
    assert "FORWARDED_HEADER" in scannable, \
        "10: the scannable region must still contain the real header constant"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python -m patient_app.net --selftest")


if __name__ == "__main__":
    main()
