#!/usr/bin/env python3
"""gpon-exporter: Prometheus exporter for Galachip GC1601 based GPON ONT SFP sticks
(e.g. FS GPON-SFP-ONT-MAC-I, SKU 351553), polled over SSH with the stick's `gccli` tool.

Keeps one persistent SSH session, runs one read-only command batch per poll in a background
thread (default every 30 s) and serves /metrics on :9617. Configuration via environment variables:
  GPON_HOST                         SSH address of the stick, or of a port-forward/NAT in front of it (required)
  GPON_PORT                         SSH port (default 22)
  GPON_USER                         SSH user (default root)
  GPON_PASSWORD_FILE                file containing the password (preferred, e.g. a mounted Secret), or
  GPON_PASSWORD                     the password itself
  GPON_KNOWN_HOSTS                  known_hosts file with the stick's host key (host key pinning), or
  GPON_INSECURE_ACCEPT_ANY_HOSTKEY  1 = accept any host key, with a warning (one of the two is required)
  GPON_INTERVAL                     poll interval seconds (default 30)
  GPON_TIMEOUT                      SSH connect and command timeout seconds (default 10)
  GPON_BASELINE_SECONDS             poll only /proc (no gccli) for this long after start (default 0 = off)
  GPON_LISTEN_PORT                  HTTP port (default 9617)
  GPON_ONESHOT                      1 = print one parsed full poll and exit, raw = print its raw output
  LOG_LEVEL                         INFO (default) / DEBUG
"""
import logging
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

import paramiko
from prometheus_client import start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, InfoMetricFamily, REGISTRY

log = logging.getLogger("gpon-exporter")

# Every command the exporter runs on the stick, in batch order: (section, command, is_gccli).
# All of them only read. Nothing here may set, save, reset or restart anything (see README).
COMMANDS = (
    ("status", "gccli gpon status", True),
    ("stats", "gccli gpon stats", True),
    ("port", "gccli port status 0", True),
    ("uptime", "cat /proc/uptime", False),
    ("show", "gccli sys show", True),
    ("loadavg", "cat /proc/loadavg", False),
    ("cpu", "grep '^cpu ' /proc/stat", False),
    ("meminfo", "cat /proc/meminfo", False),
)

# gccli gpon stats counter name -> (metric suffix, direction)
STATS_MAP = {
    "dsOcts": ("bytes_total", "downstream"), "usOcts": ("bytes_total", "upstream"),
    "dsPkts": ("packets_total", "downstream"), "usPkts": ("packets_total", "upstream"),
    "dsBcastPkts": ("broadcast_packets_total", "downstream"), "usBcastPkts": ("broadcast_packets_total", "upstream"),
    "dsMcastPkts": ("multicast_packets_total", "downstream"), "usMcastPkts": ("multicast_packets_total", "upstream"),
    "dsCrcErrPkts": ("crc_errors_total", "downstream"), "usCrcErrPkts": ("crc_errors_total", "upstream"),
    "dsDiscards": ("discards_total", "downstream"), "usDiscards": ("discards_total", "upstream"),
    "dsErr": ("errors_total", "downstream"), "usErr": ("errors_total", "upstream"),
    "dsDropEvents": ("drop_events_total", "downstream"), "usDropEvents": ("drop_events_total", "upstream"),
}
COUNTER_HELP = {
    "bytes_total": "Octets seen on the PON side", "packets_total": "Packets seen on the PON side",
    "broadcast_packets_total": "Broadcast packets on the PON side", "multicast_packets_total": "Multicast packets on the PON side",
    "crc_errors_total": "CRC-errored packets on the PON side", "discards_total": "Discarded packets on the PON side",
    "errors_total": "Errored packets on the PON side", "drop_events_total": "Drop events on the PON side",
}
SHOW_FIELDS = {
    "Gpon SN": "serial", "Software Version": "firmware", "Boot Version": "boot_version",
    "Hardware Version": "hw_version", "Vendor Id": "vendor_id", "Mac Address": "mac", "Model Name": "model",
}
# The first 8 fields of the "cpu " line of /proc/stat (guest and guest_nice are already part of user and nice)
CPU_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

# After a failed connect or poll: wait BACKOFF_BASE seconds, doubling per further failure up to BACKOFF_CAP
BACKOFF_BASE = 60.0
BACKOFF_CAP = 600.0

TRUE_VALUES = ("1", "true", "yes", "on")


class ConfigError(Exception):
    pass


def known_hosts_name(host, port):
    """The host name as known_hosts files and paramiko spell it: plain for port 22, [host]:port otherwise."""
    return host if port == 22 else "[%s]:%d" % (host, port)


def load_config(environ):
    """Read and validate the configuration from an environment mapping. Raises ConfigError."""
    def get(name, default=None):
        v = environ.get(name)
        return v if v not in (None, "") else default

    def number(name, default, cast=float):
        try:
            return cast(get(name, default))
        except ValueError:
            raise ConfigError("%s must be a number, got %r" % (name, get(name))) from None

    host = get("GPON_HOST")
    if not host:
        raise ConfigError("GPON_HOST is required: the stick's SSH address, "
                          "or the address of a port-forward/NAT in front of it")
    cfg = SimpleNamespace(
        host=host,
        port=number("GPON_PORT", "22", int),
        user=get("GPON_USER", "root"),
        interval=number("GPON_INTERVAL", "30"),
        timeout=number("GPON_TIMEOUT", "10"),
        baseline_seconds=number("GPON_BASELINE_SECONDS", "0"),
        listen_port=number("GPON_LISTEN_PORT", "9617", int),
        known_hosts=get("GPON_KNOWN_HOSTS"),
        insecure=get("GPON_INSECURE_ACCEPT_ANY_HOSTKEY", "").lower() in TRUE_VALUES,
    )

    password_file = get("GPON_PASSWORD_FILE")
    if password_file:
        try:
            with open(password_file) as fh:
                cfg.password = fh.read().strip()
        except OSError as e:
            raise ConfigError("cannot read GPON_PASSWORD_FILE: %s" % e) from None
    else:
        cfg.password = get("GPON_PASSWORD", "")

    if cfg.known_hosts:
        try:
            keys = paramiko.HostKeys(cfg.known_hosts)
        except OSError as e:
            raise ConfigError("cannot read GPON_KNOWN_HOSTS: %s" % e) from None
        name = known_hosts_name(cfg.host, cfg.port)
        if keys.lookup(name) is None:
            raise ConfigError("GPON_KNOWN_HOSTS file %s has no host key for %s "
                              "(for a port other than 22 the entry must read [host]:port)" % (cfg.known_hosts, name))
    elif not cfg.insecure:
        raise ConfigError("set GPON_KNOWN_HOSTS to a known_hosts file with the stick's host key (recommended), "
                          "or GPON_INSECURE_ACCEPT_ANY_HOSTKEY=1 to skip host key verification")
    return cfg


class AcceptAnyHostKey(paramiko.MissingHostKeyPolicy):
    """GPON_INSECURE_ACCEPT_ANY_HOSTKEY=1: accept whatever host key the server presents, and say so."""

    def missing_host_key(self, client, hostname, key):
        log.warning("accepting UNVERIFIED host key %s %s from %s. To pin it, check the fingerprint out of band "
                    "and put this line into the GPON_KNOWN_HOSTS file: %s %s %s",
                    key.get_name(), key.fingerprint, hostname, hostname, key.get_name(), key.get_base64())


def host_key_policy(cfg):
    """RejectPolicy when a known_hosts file is configured (it wins over the insecure opt-out), else AcceptAnyHostKey."""
    return paramiko.RejectPolicy() if cfg.known_hosts else AcceptAnyHostKey()


def backoff_delay(failures, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """Seconds to wait before the next attempt after `failures` (>= 1) consecutive failed polls."""
    return min(cap, base * 2 ** min(failures - 1, 32))


def in_baseline(elapsed, baseline_seconds):
    """True while the baseline phase (only /proc reads, no gccli) is running."""
    return baseline_seconds > 0 and elapsed < baseline_seconds


def remote_command(full=True):
    """The one shell command string of a poll; the @@ markers split its output into sections.
    full=False (baseline phase) leaves out every gccli call."""
    parts = ['echo "@@%s"; %s; ' % (section, cmd) for section, cmd, is_gccli in COMMANDS if full or not is_gccli]
    return "".join(parts) + 'echo "@@end"'


def split_sections(text):
    sections, cur = {}, None
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("@@"):
            cur = line[2:].strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def parse(text, full=True):
    """Parse the output of one batch into a flat dict. Raises ValueError if it is incomplete: @@end is always
    required, @@status as well in a full poll. Every other section is optional: when it is missing, its values
    stay None (or empty) and the poll still counts as successful."""
    sec = split_sections(text)
    if "end" not in sec or (full and "status" not in sec):
        raise ValueError("incomplete output (missing %s)" % ("@@status/@@end" if full else "@@end"))
    d = {"counters": {}, "info": {}, "cpu": {}}
    status = sec.get("status")
    m = re.search(r"los\s*=\s*(\d+)", status or "");     d["los"] = int(m.group(1)) if m else None
    m = re.search(r"state\s*=\s*O(\d)", status or "");   d["state"] = int(m.group(1)) if m else (0 if status is not None else None)
    m = re.search(r"fec\s*=\s*(\d+)", status or "");     d["fec"] = int(m.group(1)) if m else None
    for line in sec.get("stats", "").split("\n"):
        m = re.match(r"\s*(\w+)\s*=\s*(\d+)", line)
        if m and m.group(1) in STATS_MAP:
            d["counters"][m.group(1)] = int(m.group(2))
    p = sec.get("port", "")
    m = re.search(r"isUp\s+(\d)", p);                      d["uni_up"] = int(m.group(1)) if m else None
    m = re.search(r"speed\s+(\d+)M", p);                   d["uni_speed_mbps"] = int(m.group(1)) if m else None
    m = re.search(r"duplex\s+(\w+)", p);                   d["uni_full_duplex"] = (1 if m.group(1) == "full" else 0) if m else None
    m = re.match(r"\s*([\d.]+)", sec.get("uptime", ""));   d["uptime"] = float(m.group(1)) if m else None
    for line in sec.get("show", "").split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in SHOW_FIELDS:
                d["info"][SHOW_FIELDS[k]] = v
    m = re.match(r"\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", sec.get("loadavg", ""))
    d["load1"], d["load5"], d["load15"] = (float(x) for x in m.groups()) if m else (None, None, None)
    m = re.search(r"^cpu +(\d[\d ]*)", sec.get("cpu", ""), re.M)
    if m:
        d["cpu"] = dict(zip(CPU_MODES, (int(x) for x in m.group(1).split())))
    mem = {k: int(v) * 1024 for k, v in re.findall(r"^(\w+):\s+(\d+) kB", sec.get("meminfo", ""), re.M)}
    d["mem_total_bytes"] = mem.get("MemTotal")
    if "MemAvailable" in mem:
        d["mem_available_bytes"] = mem["MemAvailable"]
    elif all(k in mem for k in ("MemFree", "Buffers", "Cached")):  # kernels before 3.14 have no MemAvailable
        d["mem_available_bytes"] = mem["MemFree"] + mem["Buffers"] + mem["Cached"]
    else:
        d["mem_available_bytes"] = None
    return d


class Poller(threading.Thread):
    def __init__(self, cfg, clock=time.monotonic, sleep=time.sleep):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.lock = threading.Lock()     # guards the published poll result
        self.io_lock = threading.Lock()  # at most one connection and one command batch in flight
        self.data = None
        self.last_ok = 0.0
        self.last_duration = 0.0
        self.errors = 0
        self.failures = 0                # consecutive failed polls, drives the backoff
        self.baseline_active = cfg.baseline_seconds > 0
        self.client = None

    def connect(self):
        self.close()
        c = paramiko.SSHClient()
        if self.cfg.known_hosts:
            c.load_host_keys(self.cfg.known_hosts)
        c.set_missing_host_key_policy(host_key_policy(self.cfg))
        try:
            c.connect(self.cfg.host, port=self.cfg.port, username=self.cfg.user, password=self.cfg.password,
                      look_for_keys=False, allow_agent=False, timeout=self.cfg.timeout,
                      banner_timeout=self.cfg.timeout, auth_timeout=self.cfg.timeout)
        except Exception:
            c.close()  # paramiko leaves the transport open after a host key or auth failure
            raise
        c.get_transport().set_keepalive(30)
        self.client = c
        log.info("connected to %s:%s", self.cfg.host, self.cfg.port)

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def connected(self):
        t = self.client.get_transport() if self.client else None
        return t is not None and t.is_active()

    def fetch(self, full=True):
        with self.io_lock:
            if not self.connected():
                self.connect()
            _, out, err = self.client.exec_command(remote_command(full), timeout=self.cfg.timeout)
            chan = out.channel
            try:
                text = out.read().decode(errors="replace")  # raises socket.timeout if the stick stalls
                if "@@end" not in text:
                    rc = chan.exit_status if chan.exit_status_ready() else None
                    raise RuntimeError("truncated output rc=%s stderr=%s" % (rc, err.read().decode(errors="replace")[:200]))
                return text
            finally:
                chan.close()

    def step(self):
        """One poll. Returns the seconds to sleep before the next one."""
        t0 = self.clock()
        full = not in_baseline(t0 - self.started, self.cfg.baseline_seconds)
        if full and self.baseline_active:
            log.info("baseline phase over (%.0fs), switching to full polling", self.cfg.baseline_seconds)
        self.baseline_active = not full
        try:
            d = parse(self.fetch(full), full)
        except Exception as e:
            self.last_duration = self.clock() - t0
            self.errors += 1
            self.failures += 1
            self.close()
            delay = backoff_delay(self.failures)
            reason = "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__  # a paramiko timeout has no message
            log.warning("poll failed (%d in a row): %s; next attempt in %.0fs", self.failures, reason, delay)
            return delay
        self.last_duration = self.clock() - t0
        with self.lock:
            self.data = d
            self.last_ok = time.time()
        if self.failures:
            log.info("poll ok again after %d failed", self.failures)
        self.failures = 0
        log.debug("poll ok baseline=%s state=%s los=%s uptime=%s", not full, d["state"], d["los"], d["uptime"])
        return max(1.0, self.cfg.interval - self.last_duration)

    def run(self):
        while True:
            self.sleep(self.step())

    def oneshot(self):
        try:
            text = self.fetch(True)
        finally:
            self.close()
        return text, parse(text)


class Collector:
    def __init__(self, poller):
        self.p = poller

    def collect(self):
        p = self.p
        with p.lock:
            d = p.data
            last_ok = p.last_ok
        stale = (time.time() - last_ok) > 3 * p.cfg.interval if last_ok else True
        yield GaugeMetricFamily("gpon_up", "1 if the last poll of the stick succeeded recently", value=0 if stale else 1)
        yield GaugeMetricFamily("gpon_last_poll_success_timestamp_seconds", "Unix time of the last successful poll", value=last_ok)
        yield GaugeMetricFamily("gpon_poll_duration_seconds", "Duration of the last poll", value=p.last_duration)
        yield CounterMetricFamily("gpon_poll_errors_total", "Failed polls since start", value=p.errors)
        yield GaugeMetricFamily("gpon_exporter_baseline_phase",
                                "1 while only /proc is polled (GPON_BASELINE_SECONDS after start), 0 with full gccli polling",
                                value=1 if p.baseline_active else 0)
        if d is None:
            return

        def g(name, help_, key):
            if d.get(key) is not None:
                yield GaugeMetricFamily("gpon_" + name, help_, value=d[key])
        yield from g("state", "GPON ONU activation state as number (1=O1 ... 5=O5 operational, 6=O6, 7=O7; 0=unknown)", "state")
        yield from g("los", "1 if the stick reports loss of signal on the PON", "los")
        yield from g("fec", "1 if forward error correction is active on the PON", "fec")
        yield from g("uptime_seconds", "Uptime of the stick's Linux (resets on reboot)", "uptime")
        yield from g("uni_up", "1 if the host-side (UNI/SGMII) port is up", "uni_up")
        yield from g("uni_speed_mbps", "Host-side port speed in Mbit/s", "uni_speed_mbps")
        yield from g("uni_full_duplex", "1 if the host-side port is full duplex", "uni_full_duplex")
        fams = {}
        for raw, (suffix, direction) in STATS_MAP.items():
            if raw in d["counters"]:
                fam = fams.get(suffix)
                if fam is None:
                    fam = fams[suffix] = CounterMetricFamily("gpon_" + suffix, COUNTER_HELP[suffix], labels=["direction"])
                fam.add_metric([direction], d["counters"][raw])
        yield from fams.values()
        if d["info"]:
            yield InfoMetricFamily("gpon", "Static information read from gccli sys show", value=d["info"])
        yield from g("stick_load1", "1-minute load average of the stick's Linux", "load1")
        yield from g("stick_load5", "5-minute load average of the stick's Linux", "load5")
        yield from g("stick_load15", "15-minute load average of the stick's Linux", "load15")
        if d["cpu"]:
            cpu = CounterMetricFamily("gpon_stick_cpu_jiffies_total",
                                      "CPU time of the stick's Linux per mode from /proc/stat, in jiffies (USER_HZ ticks)",
                                      labels=["mode"])
            for mode, jiffies in d["cpu"].items():
                cpu.add_metric([mode], jiffies)
            yield cpu
        yield from g("stick_memory_total_bytes", "Total memory of the stick's Linux (MemTotal)", "mem_total_bytes")
        yield from g("stick_memory_available_bytes",
                     "Available memory of the stick's Linux (MemAvailable, or MemFree+Buffers+Cached on old kernels)",
                     "mem_available_bytes")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL") or "INFO", format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        sys.exit("gpon-exporter: configuration error: %s" % e)
    if cfg.known_hosts:
        if cfg.insecure:
            log.info("GPON_KNOWN_HOSTS is set, ignoring GPON_INSECURE_ACCEPT_ANY_HOSTKEY")
    else:
        log.warning("GPON_INSECURE_ACCEPT_ANY_HOSTKEY is set: the stick's host key is NOT verified; "
                    "set GPON_KNOWN_HOSTS to pin it")
    poller = Poller(cfg)
    oneshot = os.environ.get("GPON_ONESHOT")
    if oneshot:
        text, d = poller.oneshot()
        print(text if oneshot == "raw" else d)
        return
    REGISTRY.register(Collector(poller))
    poller.start()
    start_http_server(cfg.listen_port)
    log.info("listening on :%d, polling %s:%s every %.0fs%s", cfg.listen_port, cfg.host, cfg.port, cfg.interval,
             " (baseline phase: /proc only for the first %.0fs)" % cfg.baseline_seconds if cfg.baseline_seconds > 0 else "")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
