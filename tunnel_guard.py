import io
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# the staff app's port. run.py calls app.run() with no port argument, so it
# binds flask's default 5000 - if run.py ever binds a port explicitly, this
# constant moves with it. this module cannot import run.py to learn the value:
# run.py imports app, and app is on the forbidden-import list every
# patient-side selftest enforces (patient_accessor.py, patient_app/chat.py).
STAFF_PORT = 5000

# set this to a cloudflared config file to arm the guard. unset means the
# guard is bypassed - the local-dev case (D-01), announced loudly (D-02).
ENV_VAR = "TUNNEL_CONFIG_PATH"

# arch doc 5.2: the catch-all at the bottom is what makes an omission fail
# closed rather than open, so its absence is a finding in its own right.
CATCH_ALL_SERVICE = "http_status:404"

# patient_app/net.py owns this name. re-declared here rather than imported:
# importing patient_app.net would execute patient_app/__init__.py, which pulls
# in flask, flask_wtf, patient_auth and storage - and this guard has to decide
# whether the app may boot at all before any of that loads. same reason
# STAFF_PORT is a local constant instead of an import from run.py.
# the duplication is pinned against the real module in selftest section 11.
TRUST_ENV_VAR = "PATIENT_TRUST_FORWARDED_IP"
TRUST_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def trust_enabled():
    return os.environ.get(TRUST_ENV_VAR, "").strip().lower() not in TRUST_OFF_VALUES


def patient_port_rule(config_path, patient_port):
    # the first hostname rule that maps the patient app, or None. pure, and
    # deliberately forgiving - check_ingress has already rejected anything
    # malformed by the time guard_or_exit calls this.
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("ingress"), list):
        return None
    for i, rule in enumerate(doc["ingress"], start=1):
        if not isinstance(rule, dict):
            continue
        if rule.get("hostname") and service_port(rule.get("service", "")) == patient_port:
            return describe(i, rule)
    return None


def service_port(service):
    # port from a url-shaped service, else None. matches the parsed port, not
    # a substring, so http://localhost:50001 is not read as the staff port.
    # http_status:404 has no scheme separator and yields None.
    if not isinstance(service, str) or "://" not in service:
        return None
    try:
        return urlsplit(service).port
    except ValueError:
        return None


def describe(index, rule):
    if not isinstance(rule, dict):
        return f"ingress rule {index} is not a mapping"
    service = rule.get("service", "<no service>")
    hostname = rule.get("hostname")
    if hostname:
        return f"ingress rule {index} maps {hostname} to {service}"
    return f"ingress rule {index} (catch-all) sends to {service}"


def check_ingress(config_path, patient_port):
    # returns a list of violation strings - empty means the config is safe.
    # pure: reads the file, returns findings, prints nothing, exits nothing.
    # the patient port is an argument, not an import, so patient_run.py stays
    # its single source and there is no circular import.
    path = Path(config_path)

    # D-04: once armed, a config that cannot be verified is a refusal, not a pass
    if not path.exists():
        return [f"{path} does not exist - an armed guard cannot verify a missing config"]

    try:
        doc = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        return [f"{path} could not be read as yaml: {e}"]

    if not isinstance(doc, dict) or not isinstance(doc.get("ingress"), list) or not doc["ingress"]:
        return [f"{path} has no non-empty ingress list"]

    rules = doc["ingress"]
    violations = []

    for i, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            violations.append(f"{describe(i, rule)} - every ingress rule must be a mapping")
            continue

        port = service_port(rule.get("service", ""))

        # 1. nothing in this file may point at the staff app
        if port == STAFF_PORT:
            violations.append(f"{describe(i, rule)} - that is the staff app")
        # 2. a hostname rule must land on the patient app and nothing else.
        # elif, not if: a hostname rule on the staff port already reported the
        # more serious finding above and does not need a second line.
        elif rule.get("hostname") and port != patient_port:
            violations.append(
                f"{describe(i, rule)} - expected the patient app on port {patient_port}"
            )

    # 3. the last rule must be the fail-closed catch-all
    last = rules[-1]
    if not isinstance(last, dict) or last.get("hostname") or last.get("service") != CATCH_ALL_SERVICE:
        violations.append(
            f"{describe(len(rules), last)} - the last rule must be the catch-all "
            f"'service: {CATCH_ALL_SERVICE}', or an unmatched hostname fails open"
        )

    return violations


def guard_or_exit(patient_port):
    config_path = os.environ.get(ENV_VAR)

    # D-01: unset is a bypass, so a local run is never blocked. D-02: it says
    # so, so a production boot that forgot the variable is not silent.
    if not config_path:
        print(
            f"*** TUNNEL GUARD DISARMED: {ENV_VAR} is not set, so the cloudflared "
            f"ingress config is NOT being checked. if this host is behind a tunnel, "
            f"stop now and point {ENV_VAR} at the config. ***",
            file=sys.stderr,
        )
        return

    violations = check_ingress(config_path, patient_port)
    if not violations:
        # the ingress is correct - which means a tunnel really is in front of
        # the patient app. that is exactly when attribution has to be on, or
        # every patient row records cloudflared and the per-source login
        # throttle becomes one shared bucket for the whole clinic (CHAT-09).
        rule = patient_port_rule(config_path, patient_port)
        if rule and not trust_enabled():
            print(f"TUNNEL GUARD REFUSED TO START - {config_path}:", file=sys.stderr)
            print(
                f"  {rule}, but {TRUST_ENV_VAR} is not set - every patient audit row "
                f"would record the tunnel's own address, and the per-source login "
                f"throttle would collapse into one shared bucket",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"tunnel guard: {config_path} ok - patient port {patient_port} only")
        return

    # D-05: name the offending rule, so this is fixable from the message alone
    print(f"TUNNEL GUARD REFUSED TO START - {config_path}:", file=sys.stderr)
    for v in violations:
        print(f"  {v}", file=sys.stderr)
    sys.exit(1)


def selftest():
    import tempfile

    minimal = (
        "tunnel: 7f3a-not-a-real-tunnel-id\n"
        "credentials-file: /path/to/credentials.json\n"
        "ingress:\n"
        "  - hostname: patients.clinic-example.com\n"
        "    service: http://localhost:{port}\n"
        "  - service: http_status:404\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def write(name, text):
            path = tmp / name
            path.write_text(text)
            return path

        # 1. the 5.2 minimal config on the patient port is clean
        good = write("good.yml", minimal.format(port=5001))
        v = check_ingress(good, 5001)
        assert v == [], f"1: the minimal config should pass, got {v}"

        # 2. the same file pointed at the staff app
        bad = write("bad.yml", minimal.format(port=5000))
        v = check_ingress(bad, 5001)
        assert len(v) == 1, f"2: expected one violation, got {v}"
        assert "5000" in v[0], f"2: the message must name the port, got {v[0]}"
        assert "patients.clinic-example.com" in v[0], f"2: the message must name the rule, got {v[0]}"
        assert "staff app" in v[0], f"2: the message must say what 5000 is, got {v[0]}"

        # 3. the patient hostname repointed at some third port
        third = write("third.yml", minimal.format(port=5002))
        v = check_ingress(third, 5001)
        assert len(v) == 1, f"3: expected one violation, got {v}"
        assert "5002" in v[0] and "5001" in v[0], f"3: name found and expected port, got {v[0]}"

        # 4. catch-all removed - a finding even though every other rule is right
        no_catch_all = write(
            "nocatch.yml",
            "ingress:\n"
            "  - hostname: patients.clinic-example.com\n"
            "    service: http://localhost:5001\n",
        )
        v = check_ingress(no_catch_all, 5001)
        assert len(v) == 1, f"4: expected one violation, got {v}"
        assert CATCH_ALL_SERVICE in v[0], f"4: the message must name the catch-all, got {v[0]}"

        # 5. the substring trap: 50001 is not 5000
        wide = write("wide.yml", minimal.format(port=50001))
        v = check_ingress(wide, 50001)
        assert v == [], f"5: port 50001 must not read as the staff port, got {v}"

        # 6. armed at a path that does not exist (D-04)
        v = check_ingress(tmp / "absent.yml", 5001)
        assert len(v) == 1 and "does not exist" in v[0], f"6: expected a missing-file violation, got {v}"

        # 7. unparseable yaml - a violation, and no exception escapes (D-04)
        broken = write("broken.yml", "ingress: [unclosed\n  - : : :\n")
        v = check_ingress(broken, 5001)
        assert len(v) == 1 and "yaml" in v[0], f"7: expected a parse violation, got {v}"

        # 8. valid yaml, no usable ingress section (D-04)
        no_key = write("nokey.yml", "tunnel: 7f3a\ncredentials-file: /x.json\n")
        v = check_ingress(no_key, 5001)
        assert len(v) == 1 and "ingress" in v[0], f"8: expected an ingress violation, got {v}"
        empty = write("empty.yml", "ingress: []\n")
        v = check_ingress(empty, 5001)
        assert len(v) == 1 and "ingress" in v[0], f"8: an empty ingress list is unverifiable, got {v}"

        # 9. env var unset: the guard returns, but says so on stderr (D-01/D-02)
        saved = os.environ.pop(ENV_VAR, None)
        captured = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = captured
        try:
            guard_or_exit(5001)
        finally:
            sys.stderr = real_stderr
            if saved is not None:
                os.environ[ENV_VAR] = saved
        assert "DISARMED" in captured.getvalue(), \
            f"9: a disarmed boot must announce itself on stderr, got {captured.getvalue()!r}"

        # 10. armed against the bad config: refuses with exit 1 (D-05)
        saved = os.environ.get(ENV_VAR)
        os.environ[ENV_VAR] = str(bad)
        captured = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = captured
        try:
            guard_or_exit(5001)
            raise AssertionError("10: guard_or_exit should have exited on the bad config")
        except SystemExit as e:
            assert e.code == 1, f"10: expected exit code 1, got {e.code}"
        finally:
            sys.stderr = real_stderr
            if saved is None:
                del os.environ[ENV_VAR]
            else:
                os.environ[ENV_VAR] = saved
        assert "5000" in captured.getvalue(), \
            f"10: the refusal must name the offending rule, got {captured.getvalue()!r}"

        # 11. CHAT-09. the ingress is CORRECT here - which is exactly when
        # attribution has to be on. armed at a good config with the trust flag
        # unset must refuse, or the tunnel opens in front of an app that
        # records cloudflared as every patient.
        # NOTE: sections 11-13 are also the first unit coverage of
        # guard_or_exit's SUCCESS path. before this plan the function was
        # exercised only disarmed (9) and against a bad config (10), so a
        # change to the armed-and-clean branch could not fail this suite.
        def armed(config, trust, port=5001):
            saved_cfg = os.environ.get(ENV_VAR)
            saved_trust = os.environ.get(TRUST_ENV_VAR)
            os.environ[ENV_VAR] = str(config)
            if trust is None:
                os.environ.pop(TRUST_ENV_VAR, None)
            else:
                os.environ[TRUST_ENV_VAR] = trust
            captured_out, captured_err = io.StringIO(), io.StringIO()
            real_out, real_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = captured_out, captured_err
            exited = None
            try:
                guard_or_exit(port)
            except SystemExit as e:
                exited = e.code
            finally:
                sys.stdout, sys.stderr = real_out, real_err
                if saved_cfg is None:
                    os.environ.pop(ENV_VAR, None)
                else:
                    os.environ[ENV_VAR] = saved_cfg
                if saved_trust is None:
                    os.environ.pop(TRUST_ENV_VAR, None)
                else:
                    os.environ[TRUST_ENV_VAR] = saved_trust
            return exited, captured_out.getvalue(), captured_err.getvalue()

        code11, _out11, err11 = armed(good, None)
        assert code11 == 1, f"11: a clean ingress with attribution off must refuse, got {code11}"
        assert TRUST_ENV_VAR in err11, \
            f"11: the refusal must name the flag that would fix it, got {err11!r}"
        assert "throttle" in err11, \
            f"11: the refusal must say what it costs, got {err11!r}"

        # 12. the same config with the flag set boots
        code12, out12, _err12 = armed(good, "1")
        assert code12 is None, f"12: a clean ingress with attribution on must not exit, got {code12}"
        assert "ok" in out12, f"12: the passing guard should still confirm on stdout, got {out12!r}"

        # 13. disarmed never consults the flag - a local run with no tunnel is
        # unaffected by any of this
        saved13 = os.environ.pop(ENV_VAR, None)
        saved13t = os.environ.pop(TRUST_ENV_VAR, None)
        captured13 = io.StringIO()
        real13 = sys.stderr
        sys.stderr = captured13
        try:
            guard_or_exit(5001)
        finally:
            sys.stderr = real13
            if saved13 is not None:
                os.environ[ENV_VAR] = saved13
            if saved13t is not None:
                os.environ[TRUST_ENV_VAR] = saved13t
        assert "DISARMED" in captured13.getvalue(), \
            "13: a disarmed boot must still just announce itself, flag or no flag"

        # 14. the duplicated flag name cannot drift. imported HERE and nowhere
        # else - the production path must not pull patient_app in (see the
        # comment on TRUST_ENV_VAR).
        from patient_app import net as _net
        assert TRUST_ENV_VAR == _net.TRUST_ENV_VAR, \
            f"14: flag name drifted - {TRUST_ENV_VAR!r} here vs {_net.TRUST_ENV_VAR!r} in patient_app/net.py"
        for probe in ("", "0", "false", "off", "1", "true"):
            saved14 = os.environ.get(TRUST_ENV_VAR)
            os.environ[TRUST_ENV_VAR] = probe
            try:
                assert trust_enabled() == _net.trust_enabled(), \
                    f"14: the two trust readers disagree on {probe!r}"
            finally:
                if saved14 is None:
                    os.environ.pop(TRUST_ENV_VAR, None)
                else:
                    os.environ[TRUST_ENV_VAR] = saved14

        # 15. the over-fire guard. a clean config that routes NOTHING to the
        # patient app means no tunnel is in front of it, so attribution is not
        # required and the new check must stay quiet. without this, section 11
        # could be satisfied by a check that refuses every armed boot.
        catch_only = write("catchonly.yml", "ingress:\n  - service: http_status:404\n")
        assert check_ingress(catch_only, 5001) == [], \
            "15: a catch-all-only ingress is a valid config"
        code15, out15, err15 = armed(catch_only, None)
        assert code15 is None, \
            f"15: a config with no patient-port rule must not refuse on attribution, got {code15} / {err15!r}"
        assert "ok" in out15, f"15: it should confirm normally, got {out15!r}"

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    print(f"tunnel guard: set {ENV_VAR} to a cloudflared config to arm it")


if __name__ == "__main__":
    main()
