#!/usr/bin/env python3
"""Can this machine reach and write to Azure Postgres? Run this first.

Each layer is a separate check, so a failure names the layer that broke:

    1. settings      PGHOST / PGUSER / PGPASSWORD present
    2. DNS           hostname resolves
    3. TCP           outbound 5432 is open (the usual corporate-firewall block)
    4. TLS           server offers TLS; certificate chain checked
    5. auth          Postgres accepts these credentials from this IP
    6. permissions   this role can do what its job needs, and no more
    7. latency       round trip and batch throughput

A failed layer skips the ones that depend on it, rather than burying the
real problem under a cascade of follow-on errors.

    pip install "psycopg[binary]"     # only needed from step 5 on
    python preflight.py               # reads .env if present
    python preflight.py --device 10.15.22.19 --device 10.15.22.83

Step 6 has two modes. Before schema.sql is applied it only proves the role
can write at all, using a temporary table. After, it runs the real
operations against every real table inside a transaction that is rolled
back, so nothing is left behind. Which operations depends on the role:
members of xl_sync get the sync service's checks, members of xl_reader get
the Flask app's.

Exit status: 0 if nothing failed (warnings allowed), 1 otherwise.
Standard library only, apart from psycopg.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import ssl
import statistics
import sys
import time
import urllib.request
from pathlib import Path

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
_MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL ", SKIP: " skip "}

results: list[tuple[str, str, str]] = []


def record(level: str, check: str, detail: str = "") -> None:
    results.append((level, check, detail))
    print(f"[{_MARK[level]}] {check}")
    for line in detail.splitlines():
        print(f"         {line}")


def skip(check: str, because: str) -> None:
    record(SKIP, check, f"skipped: {because}")


def load_env(path: str = ".env") -> bool:
    """Minimal .env reader; real environment variables win."""
    f = Path(path)
    if not f.exists():
        return False
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    return True


# ------------------------------------------------------------- 1. settings


def check_settings() -> dict[str, str] | None:
    cfg = {
        "host": os.environ.get("PGHOST", ""),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "postgres"),
        "user": os.environ.get("PGUSER", ""),
        "password": os.environ.get("PGPASSWORD", ""),
    }
    missing = [k for k in ("PGHOST", "PGUSER", "PGPASSWORD") if not os.environ.get(k)]
    if missing:
        record(FAIL, "1. settings", f"missing {', '.join(missing)}. Put them in .env or the environment.\n"
                                    "PGHOST looks like <server>.postgres.database.azure.com")
        return None
    if not cfg["port"].isdigit():
        record(FAIL, "1. settings", f"PGPORT={cfg['port']!r} is not a port number")
        return None
    record(OK, "1. settings", f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}")
    return cfg


# ------------------------------------------------------------------ 2. DNS


def check_dns(host: str) -> bool:
    try:
        t0 = time.monotonic()
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        ms = (time.monotonic() - t0) * 1000
    except socket.gaierror as exc:
        record(FAIL, "2. DNS", f"cannot resolve {host}: {exc}\n"
                               "Either the name is wrong, or this network's DNS does not forward external lookups.")
        return False
    ips = sorted({i[4][0] for i in infos})
    detail = f"{host} -> {', '.join(ips)} in {ms:.0f} ms"
    if any(ip.startswith(("10.", "192.168.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip) for ip in ips):
        record(WARN, "2. DNS", detail + "\nResolves to a private address: a private endpoint or split DNS. "
                                        "Fine if intended; otherwise the public firewall rule will not apply.")
    else:
        record(OK, "2. DNS", detail)
    return True


# ------------------------------------------------------------------ 3. TCP


def check_tcp(host: str, port: int) -> bool:
    times = []
    try:
        for _ in range(3):
            t0 = time.monotonic()
            with socket.create_connection((host, port), timeout=10):
                times.append((time.monotonic() - t0) * 1000)
    except socket.timeout:
        record(FAIL, f"3. TCP {port}",
               f"timed out connecting to {host}:{port}.\n"
               f"Most likely the corporate firewall blocks outbound {port}. Ask IT to allow outbound\n"
               f"TCP {port} to this one hostname. If that is refused, the fallback is to push over\n"
               f"HTTPS to a small API in Azure instead of connecting to Postgres directly.\n"
               + https_fallback_note())
        return False
    except OSError as exc:
        hint = ("refused: something answered but nothing listens on that port"
                if isinstance(exc, ConnectionRefusedError) else "network unreachable or reset in transit")
        record(FAIL, f"3. TCP {port}", f"{exc}\n{hint}.\n" + https_fallback_note())
        return False
    record(OK, f"3. TCP {port}", f"connect {min(times):.0f}-{max(times):.0f} ms over 3 attempts")
    return True


def https_fallback_note() -> str:
    """Is the HTTPS fallback even viable from here? TCP connect only; nothing is sent."""
    try:
        with socket.create_connection(("management.azure.com", 443), timeout=5):
            return "Outbound 443 to Azure works from here, so the HTTPS fallback is viable."
    except OSError:
        return "Outbound 443 to Azure ALSO fails - this machine may have no internet route at all."


# ------------------------------------------------------------------ 4. TLS


def check_tls(host: str, port: int) -> bool:
    """Postgres negotiates TLS inside its own protocol (an SSLRequest
    message, then the handshake), so a plain TLS probe would not work.

    The certificate is verified against this machine's trust store. The
    driver's sslmode=require does not verify it, so a failure here is a
    warning: the connection would still work, but it could be intercepted -
    usually by a TLS-inspecting corporate proxy."""
    def handshake(verify: bool):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        sock = socket.create_connection((host, port), timeout=10)
        sock.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")  # SSLRequest
        reply = sock.recv(1)
        if reply != b"S":
            sock.close()
            return reply, None
        tls = ctx.wrap_socket(sock, server_hostname=host)
        return reply, tls

    try:
        reply, tls = handshake(verify=True)
    except ssl.SSLCertVerificationError as exc:
        issuer = "unknown"
        try:
            _, tls = handshake(verify=False)
            der = tls.getpeercert(binary_form=True)
            tls.close()
            issuer = _issuer_from_der(der)
        except Exception:  # noqa: BLE001
            pass
        record(WARN, "4. TLS", f"certificate did not verify: {exc.verify_message}\n"
                               f"Presented by: {issuer}\n"
                               "If that is not DigiCert or Microsoft, a proxy is inspecting this traffic.\n"
                               "Otherwise this machine's CA bundle is out of date.")
        return True
    except OSError as exc:
        record(FAIL, "4. TLS", f"handshake failed: {exc}")
        return False

    if tls is None:
        record(FAIL, "4. TLS", f"server answered {reply!r} to SSLRequest instead of 'S': TLS is off.\n"
                               "Azure requires TLS, so this is probably not the Azure server.")
        return False
    with tls:
        cert = tls.getpeercert() or {}
        issuer = ", ".join(f"{k}={v}" for rdn in cert.get("issuer", ()) for k, v in rdn
                           if k in ("organizationName", "commonName"))
        expires = cert.get("notAfter", "?")
        record(OK, "4. TLS", f"{tls.version()} {tls.cipher()[0]}\nissuer {issuer}; expires {expires}")
    return True


def _issuer_from_der(der: bytes) -> str:
    # The stdlib cannot decode an unverified certificate's fields, so pull
    # printable organisation-ish strings out of the DER as a best effort.
    strings = re.findall(rb"[\x13\x0c]([\x01-\x7f])([ -~]{3,64})", der)
    out = [s.decode() for n, s in strings if n[0] == len(s)]
    return ", ".join(dict.fromkeys(out[:4])) or "unreadable"


# ----------------------------------------------------------------- 5. auth


def check_auth(cfg: dict[str, str]):
    try:
        import psycopg
        from psycopg.conninfo import make_conninfo
    except ImportError:
        record(FAIL, "5. auth", 'psycopg is not installed. Run: pip install "psycopg[binary]"')
        return None

    # The same connection settings the sync service uses. Keepalives matter:
    # Azure drops idle connections.
    conninfo = make_conninfo(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
        sslmode="require", connect_timeout="10", application_name="xl-preflight",
        keepalives="1", keepalives_idle="30", keepalives_interval="10", keepalives_count="3",
    )
    try:
        t0 = time.monotonic()
        conn = psycopg.connect(conninfo, autocommit=False)
        ms = (time.monotonic() - t0) * 1000
    except psycopg.OperationalError as exc:
        text = str(exc).strip()
        m = re.search(r'no pg_hba\.conf entry for host "([^"]+)"', text)
        if m:
            hint = (f"Azure's firewall does not allow this site. Azure sees this machine as {m.group(1)}:\n"
                    f"add that address in the portal under Networking -> Firewall rules.\n"
                    f"That is the plant's PUBLIC egress IP, not this machine's LAN address.")
        elif "password authentication failed" in text:
            hint = "Credentials rejected. Check PGUSER/PGPASSWORD."
        elif "does not exist" in text and "database" in text:
            hint = f'Database "{cfg["dbname"]}" does not exist. Create it, or set PGDATABASE.'
        elif "timeout" in text.lower():
            hint = "TCP worked but Postgres did not answer in time."
        else:
            hint = ""
        record(FAIL, "5. auth", f"{text}\n{hint}".rstrip())
        return None

    with conn.cursor() as cur:
        cur.execute("select version(), current_setting('ssl'), inet_client_addr()::text, current_user")
        version, ssl_on, client_ip, user = cur.fetchone()
    conn.rollback()
    detail = (f"connected as {user} in {ms:.0f} ms\n{version.split(' on ')[0]}\n"
              f"Azure sees this machine as {client_ip or 'unknown'} - that is the address in its firewall rule")
    if ssl_on != "on":
        record(WARN, "5. auth", detail + "\nssl is OFF for this session - unexpected on Azure")
    else:
        record(OK, "5. auth", detail)
    major = int(re.search(r"PostgreSQL (\d+)", version).group(1)) if "PostgreSQL" in version else 0
    if major and major < 14:
        record(FAIL, "5. auth", f"PostgreSQL {major}: schema.sql needs 14 or later")
    return conn


# ---------------------------------------------------------- 6. permissions

EXPECTED_TABLES = (
    "device", "device_config_epoch", "sync_state", "sync_heartbeat", "sync_quarantine",
    "dimension_value", "current_state", "part_standard",
    "hour_record", "shift_record", "state_event", "part_run", "job_run", "quality_event",
)

# What the sync service does, table by table. Every statement is the same
# shape as the real one (upsert, so SELECT + INSERT + UPDATE), aimed at a
# probe key no device will ever use. All rolled back.
PROBE = -1
SYNC_OPS: tuple[tuple[str, str], ...] = (
    ("device", "INSERT INTO device (device_id, host) VALUES ('FR8', '10.15.22.19') "
               "ON CONFLICT (device_id) DO UPDATE SET last_seen = now()"),
    ("sync_state", "INSERT INTO sync_state (device_id, channel) VALUES ('FR8', '_preflight') "
                   "ON CONFLICT (device_id, channel) DO UPDATE SET last_synced_at = now()"),
    ("sync_heartbeat", "INSERT INTO sync_heartbeat (instance_id, last_heartbeat) VALUES ('_preflight', now()) "
                       "ON CONFLICT (instance_id) DO UPDATE SET last_heartbeat = now()"),
    ("sync_quarantine", "INSERT INTO sync_quarantine (device_id, channel, natural_key, payload, error) "
                        "VALUES ('FR8', '_preflight', '-1', '{}', 'probe') "
                        "ON CONFLICT (device_id, channel, natural_key) WHERE resolved_at IS NULL "
                        "DO UPDATE SET attempts = sync_quarantine.attempts + 1"),
    ("dimension_value", "INSERT INTO dimension_value (device_id, dimension, key) VALUES ('FR8', '_preflight', 'x') "
                        "ON CONFLICT (device_id, dimension, key) DO UPDATE SET synced_at = now()"),
    ("current_state", "INSERT INTO current_state (device_id, attempted_at) VALUES ('FR8', now()) "
                      "ON CONFLICT (device_id) DO UPDATE SET attempted_at = now()"),
    *(
        (t, f"INSERT INTO {t} (device_id, {k}, sync_id, start_time) VALUES ('FR8', {PROBE}, 1, now()) "
            f"ON CONFLICT (device_id, {k}) DO UPDATE SET updated_at = now(), deleted_at = NULL "
            f"WHERE {t}.sync_id IS NULL OR EXCLUDED.sync_id >= {t}.sync_id")
        for t, k in (("hour_record", "shift_hour_event_id"), ("shift_record", "shift_event_id"),
                     ("state_event", "process_state_event_id"), ("part_run", "part_event_id"),
                     ("job_run", "job_event_id"))
    ),
    ("quality_event", f"INSERT INTO quality_event (device_id, record_id, sync_id, production_day_number) "
                      f"VALUES ('FR8', {PROBE}, 1, 20700) ON CONFLICT (device_id, record_id) "
                      f"DO UPDATE SET updated_at = now()"),
    ("refresh_part_standard()", "SELECT refresh_part_standard()"),
)
# Things the sync role must NOT be able to do.
SYNC_DENIED: tuple[tuple[str, str], ...] = (
    ("write device_config_epoch", "INSERT INTO device_config_epoch (device_id, config_epoch, effective_from) "
                                  "VALUES ('FR8', 999, now())"),
    ("write part_standard.sap_standard", "UPDATE part_standard SET sap_standard = 1"),
)

READER_OPS: tuple[tuple[str, str], ...] = (
    *((v, f"SELECT * FROM {v} LIMIT 1") for v in (
        "daily_time_accounting", "downtime_by_reason", "activity_hours", "part_performance",
        "data_quality", "sync_status", "collector_status", "sync_alerts", "current_state")),
    ("part_standard SAP columns", "INSERT INTO part_standard (part, sap_standard, sap_unit) "
                                  "VALUES ('_preflight', 1, 's_per_piece') "
                                  "ON CONFLICT (part) DO UPDATE SET sap_standard = EXCLUDED.sap_standard"),
)
READER_DENIED: tuple[tuple[str, str], ...] = (
    ("write hour_record", f"INSERT INTO hour_record (device_id, shift_hour_event_id) VALUES ('FR8', {PROBE})"),
    ("write part_standard.ideal_cycle_time", "UPDATE part_standard SET ideal_cycle_time = 1"),
    ("write device_config_epoch", "INSERT INTO device_config_epoch (device_id, config_epoch, effective_from) "
                                  "VALUES ('FR8', 999, now())"),
)


def _attempt(conn, sql: str) -> Exception | None:
    """Run one statement inside a savepoint; return its error, if any."""
    import psycopg

    try:
        with conn.transaction():  # nested inside the outer one: a savepoint
            with conn.cursor() as cur:
                cur.execute(sql)
        return None
    except psycopg.Error as exc:
        return exc


def _describe(exc: Exception) -> str:
    state = getattr(getattr(exc, "diag", None), "sqlstate", None) or "?"
    return f"{state} {str(exc).strip().splitlines()[0]}"


def check_permissions(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE pg_has_role(current_user, oid, 'member')")
        roles = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        superuser = cur.fetchone()[0]
        cur.execute("SELECT t FROM unnest(%s::text[]) t WHERE to_regclass('public.' || t) IS NULL",
                    (list(EXPECTED_TABLES),))
        missing = [r[0] for r in cur.fetchall()]
    conn.rollback()

    admin = superuser or "azure_pg_admin" in roles
    if admin:
        record(WARN, "6. permissions: role",
               "connected as a server ADMIN. Every check below will pass trivially.\n"
               "Re-run with the xl_sync_svc credentials to test what the sync service will really have.")

    if len(missing) == len(EXPECTED_TABLES):
        _generic_write(conn)
        record(WARN, "6. permissions: schema", "schema.sql has not been applied yet, so only generic write\n"
                                               "access was tested. Apply it, then run preflight again.")
        return
    if missing:
        record(FAIL, "6. permissions: schema", f"schema.sql is partly applied; missing: {', '.join(missing)}")
        return

    if "xl_reader" in roles and "xl_sync" not in roles and not admin:
        mode, ops, denied = "app (xl_reader)", READER_OPS, READER_DENIED
    else:
        if "xl_sync" not in roles and not admin:
            record(WARN, "6. permissions: role", f"{', '.join(sorted(roles))} is in neither xl_sync nor xl_reader;\n"
                                                 "testing it as the sync service")
        mode, ops, denied = "sync (xl_sync)", SYNC_OPS, SYNC_DENIED

    with conn.transaction(force_rollback=True):
        failures = [(name, _describe(err)) for name, sql in ops if (err := _attempt(conn, sql))]
        allowed = [name for name, sql in denied if _attempt(conn, sql) is None]

    if failures:
        record(FAIL, f"6. permissions: {mode}",
               "\n".join(f"{n}: {e}" for n, e in failures)
               + "\n42501 = missing GRANT (re-run the roles section of schema.sql);"
                 "\n42P01/42703 = schema older than this preflight")
    else:
        record(OK, f"6. permissions: {mode}", f"all {len(ops)} operations work (rolled back, nothing written)")
    if allowed and not admin:
        record(WARN, f"6. permissions: {mode} limits",
               "this role can also: " + ", ".join(allowed) + "\nIt should not. Check for extra grants.")
    elif not allowed:
        record(OK, f"6. permissions: {mode} limits", f"correctly denied: {', '.join(n for n, _ in denied)}")


def _generic_write(conn) -> None:
    import psycopg

    try:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:
                cur.execute("CREATE TEMPORARY TABLE _preflight (x int)")
                cur.execute("INSERT INTO _preflight VALUES (1)")
        record(OK, "6. permissions: write", "created and wrote a temporary table")
    except psycopg.Error as exc:
        record(FAIL, "6. permissions: write", _describe(exc))


# -------------------------------------------------------------- 7. latency


def check_latency(conn) -> None:
    import psycopg

    samples = []
    with conn.cursor() as cur:
        for _ in range(20):
            t0 = time.monotonic()
            cur.execute("SELECT 1")
            cur.fetchone()
            samples.append((time.monotonic() - t0) * 1000)
    conn.rollback()
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[int(len(samples) * 0.95) - 1]
    detail = f"round trip median {median:.1f} ms, p95 {p95:.1f} ms, worst {samples[-1]:.1f} ms"

    # Batch throughput, the number that matters for the first backfill.
    rows = 1000
    try:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:
                cur.execute("CREATE TEMPORARY TABLE _preflight_batch (k bigint PRIMARY KEY, v double precision)")
                t0 = time.monotonic()
                cur.executemany(
                    "INSERT INTO _preflight_batch VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                    [(i, i * 0.5) for i in range(rows)],
                )
                secs = time.monotonic() - t0
        rate = rows / secs
        # ~1,000 rows/day across four devices, ~a year on each device.
        backfill = 365_000 / rate
        detail += (f"\nbatched upsert {rate:,.0f} rows/s; a year's backfill (~365k rows)"
                   f" is ~{backfill / 60:.1f} min of database time"
                   f"\n(row-by-row it would cap near {1000 / max(median, 0.1):,.0f} rows/s)")
    except psycopg.Error as exc:
        detail += f"\nbatch test skipped: {_describe(exc)}"

    record(OK if median < 100 else WARN, "7. latency", detail
           + ("" if median < 100 else "\nslow, but workable: a sync every five minutes is not latency-bound"))


# ------------------------------------------------------- plant side (opt.)


def check_device(host: str) -> None:
    """The sync box needs the LAN as well as Azure. GET only."""
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(f"http://{host}/rest/cpe/attributes", timeout=5) as resp:
            import json
            attrs = json.loads(resp.read())
        ms = (time.monotonic() - t0) * 1000
        record(OK, f"device {host}", f"{attrs.get('asset_name')} firmware {attrs.get('version')} "
                                     f"data model {attrs.get('data_version')} in {ms:.0f} ms")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, f"device {host}", f"{exc}\nOpen http://{host} in a browser from this machine to "
                                       f"tell a network problem from a device problem.")


# -------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default=".env", help="env file to read (default .env)")
    ap.add_argument("--device", action="append", default=[], metavar="IP",
                    help="also check an XL device on the LAN; repeatable")
    args = ap.parse_args()

    env_loaded = load_env(args.env)
    print(f"preflight from {socket.gethostname()}" + (f", settings from {args.env}" if env_loaded else "") + "\n")

    cfg = check_settings()
    if cfg is None:
        for c in ("2. DNS", "3. TCP", "4. TLS", "5. auth", "6. permissions", "7. latency"):
            skip(c, "no settings")
    elif not check_dns(cfg["host"]):
        for c in ("3. TCP", "4. TLS", "5. auth", "6. permissions", "7. latency"):
            skip(c, "DNS failed")
    elif not check_tcp(cfg["host"], int(cfg["port"])):
        for c in ("4. TLS", "5. auth", "6. permissions", "7. latency"):
            skip(c, "TCP failed")
    elif not check_tls(cfg["host"], int(cfg["port"])):
        for c in ("5. auth", "6. permissions", "7. latency"):
            skip(c, "TLS failed")
    else:
        conn = check_auth(cfg)
        if conn is None:
            for c in ("6. permissions", "7. latency"):
                skip(c, "auth failed")
        else:
            with conn:
                check_permissions(conn)
                check_latency(conn)

    for host in args.device:
        check_device(host)

    failed = [c for lvl, c, _ in results if lvl == FAIL]
    warned = [c for lvl, c, _ in results if lvl == WARN]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed - fix the first one, then re-run:")
        for c in failed:
            print(f"  - {c}")
        return 1
    print("All checks passed" + (f", {len(warned)} warning(s)." if warned else ". This machine can sync to Azure."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
