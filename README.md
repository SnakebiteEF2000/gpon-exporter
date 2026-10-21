# gpon-exporter

Prometheus exporter for GPON ONT SFP sticks based on the Galachip GC1601, such as the FS GPON-SFP-ONT-MAC-I
(SKU 351553) and its siblings. These sticks have no SNMP, no syslog and a JavaScript-only web UI, but they do
have a BusyBox shell over SSH with the vendor tool `gccli`. The exporter keeps one SSH session open to the stick,
runs one short, read-only command batch per poll (every 30 s by default) and serves the results on `:9617/metrics`.

It reports the PON state (O1 to O7), loss of signal, FEC, PON traffic and error counters, the host-side (UNI) port,
the stick's uptime, and the stick's own CPU, load and memory, so you can see what the monitoring costs it.

## Tested with

- Firmware V1.2.4 on an FS GPON-SFP-ONT-MAC-I.
- The stick runs dropbear 2019.78. It offers only `ssh-rsa` (1024 bit) and `ssh-dss` host keys and
  `ecdh-sha2-nistp256` key exchange (no curve25519). paramiko 3.5 and 4.0 negotiate this without any overrides.
- paramiko 5 removed SHA-1 RSA signatures (`ssh-rsa`) and can no longer verify this stick's host key, so
  `requirements.txt` stays below paramiko 5 on purpose (Dependabot is told to skip 5.x).
- OpenSSH 8.8 and later refuse the `ssh-rsa` host key by default. To log in by hand, use
  `ssh -o HostKeyAlgorithms=+ssh-rsa root@<stick>`.

## What runs on the stick

Each poll opens one exec channel on the existing SSH session and runs this batch in a single `sh -c`. The commands
run one after another; the `@@` markers split the output:

| Section   | Command                    | Phase         |
|-----------|----------------------------|---------------|
| `status`  | `gccli gpon status`        | full          |
| `stats`   | `gccli gpon stats`         | full          |
| `port`    | `gccli port status 0`      | full          |
| `uptime`  | `cat /proc/uptime`         | full, baseline |
| `show`    | `gccli sys show`           | full          |
| `loadavg` | `cat /proc/loadavg`        | full, baseline |
| `cpu`     | `grep '^cpu ' /proc/stat`  | full, baseline |
| `meminfo` | `cat /proc/meminfo`        | full, baseline |

That list is complete (see `COMMANDS` in `gpon_exporter.py`), and it only reads. This is deliberate: the stick is
your internet path, so the exporter never runs anything that sets, saves, resets or restarts something, and nothing
that could make the stick re-range on the PON. It also never runs two batches or two sessions at once. A unit test
checks every command against a read-only allowlist.

**Load on the stick:** one persistent SSH session, which is re-established only after an error. Each full poll
starts about 9 short-lived processes (the shell, 4 `gccli` calls and 4 small `/proc` reads); a baseline poll
starts about 5.

**Failures and backoff:** when a connect or poll fails, the exporter closes the session and waits 60 s before it
tries again. Each further failure doubles the wait, up to 10 min, and the first successful poll resets it. It never
reconnects in a tight loop. Every command batch has a timeout (`GPON_TIMEOUT`). When it expires, the exporter
closes the channel and the session, so sessions can't pile up on the stick.

### Baseline phase

Set `GPON_BASELINE_SECONDS` (for example `3600`) to measure the stick's idle load before the real polling starts.
For that long after start, every poll reads only `/proc` (uptime, load, CPU and memory) and runs no `gccli`.
Then the exporter switches to the full batch. `gpon_exporter_baseline_phase` is 1 during the baseline phase and 0
after it. `gpon_uptime_seconds` and the `gpon_stick_*` metrics come from both phases, so you can compare the
stick's CPU and load with and without the `gccli` calls. The `gccli` metrics (`gpon_state`, `gpon_los`, counters,
`gpon_info` and so on) are absent during the baseline phase.

## Configuration

| Variable                           | Default | Meaning |
|------------------------------------|---------|---------|
| `GPON_HOST`                        | (required) | SSH address of the stick, or of a port-forward/NAT in front of it |
| `GPON_PORT`                        | `22`    | SSH port |
| `GPON_USER`                        | `root`  | SSH user |
| `GPON_PASSWORD_FILE`               |         | File with the SSH password, e.g. a mounted Secret (preferred) |
| `GPON_PASSWORD`                    |         | The SSH password itself, if no file is used |
| `GPON_KNOWN_HOSTS`                 |         | known_hosts file with the stick's host key: the host key is pinned (recommended) |
| `GPON_INSECURE_ACCEPT_ANY_HOSTKEY` |         | `1` accepts any host key and logs a warning; set this or `GPON_KNOWN_HOSTS` |
| `GPON_INTERVAL`                    | `30`    | Poll interval in seconds |
| `GPON_TIMEOUT`                     | `10`    | SSH connect and command timeout in seconds |
| `GPON_BASELINE_SECONDS`            | `0`     | Length of the baseline phase after start; `0` turns it off |
| `GPON_LISTEN_PORT`                 | `9617`  | HTTP port for `/metrics` |
| `GPON_ONESHOT`                     |         | `1` prints one parsed full poll and exits; `raw` prints its raw output |
| `LOG_LEVEL`                        | `INFO`  | `DEBUG` for more detail |

The exporter refuses to start, with a clear message, if `GPON_HOST` is missing, if neither `GPON_KNOWN_HOSTS` nor
`GPON_INSECURE_ACCEPT_ANY_HOSTKEY=1` is set, or if the known_hosts file has no entry for the configured host.

### Host key pinning

With `GPON_KNOWN_HOSTS` set, the exporter connects only if the stick presents exactly that host key (paramiko
`RejectPolicy`). For any port other than 22, the known_hosts entry must be written `[host]:port`, as OpenSSH
writes it. `ssh-keyscan` produces the right form and works against the stick's dropbear:

```sh
ssh-keyscan -t rsa -p 2222 192.0.2.10 > known_hosts   # writes "[192.0.2.10]:2222 ssh-rsa AAAA..."
ssh-keygen -lf known_hosts                              # fingerprint, to check against the stick
```

Run the scan over a network path you trust. If you can't pin the key yet, start the exporter once with
`GPON_INSECURE_ACCEPT_ANY_HOSTKEY=1`. It logs the key it was shown, with its fingerprint, as a ready-to-paste
known_hosts line.

## Reaching the stick

The stick listens for SSH on its own management address, on the host-facing (UNI) side. Two common setups:

- **Direct:** give the interface the stick is plugged into an address in the stick's management subnet, and point
  `GPON_HOST` at the stick.
- **Through a port-forward/NAT** on the router or switch that holds the stick, so the exporter can reach it from
  another network. Here is a MikroTik RouterOS example with documentation addresses: the switch answers on
  192.0.2.10:2222 and forwards to the stick, and only the exporter host (192.0.2.50) may use the forward:

  ```
  /ip address add address=<an-address-in-the-stick's-subnet>/24 interface=<sfp-port-with-the-stick>
  /ip firewall nat add chain=dstnat dst-address=192.0.2.10 protocol=tcp dst-port=2222 src-address=192.0.2.50 \
      action=dst-nat to-addresses=<stick-management-ip> to-ports=22
  /ip firewall nat add chain=srcnat dst-address=<stick-management-ip> protocol=tcp dst-port=22 action=masquerade
  ```

  Then set `GPON_HOST=192.0.2.10` and `GPON_PORT=2222`.

## Running it

Docker:

```sh
# known_hosts from above; the password goes in a file that uid 10001 (the container user) can read
docker run -d --name gpon-exporter -p 9617:9617 --read-only \
  -e GPON_HOST=192.0.2.10 -e GPON_PORT=2222 \
  -e GPON_KNOWN_HOSTS=/etc/gpon/known_hosts -e GPON_PASSWORD_FILE=/etc/gpon/password \
  -v "$PWD/known_hosts:/etc/gpon/known_hosts:ro" -v "$PWD/password:/etc/gpon/password:ro" \
  ghcr.io/snakebiteef2000/gpon-exporter:0.1.0
```

Kubernetes: [`examples/kubernetes.yaml`](examples/kubernetes.yaml) has a Secret for the password, a ConfigMap for
the known_hosts line, a single-replica Deployment (`Recreate`, so two pollers never run at once) and a Service.
Pin the image by digest in production.

Locally, without a container (Python 3.9 or later):

```sh
pip install -r requirements.txt
GPON_HOST=192.0.2.10 GPON_PORT=2222 GPON_KNOWN_HOSTS=./known_hosts GPON_PASSWORD_FILE=./password \
  GPON_ONESHOT=1 python3 gpon_exporter.py
```

## Metrics

| Metric | Type | Labels | Phase | Meaning |
|--------|------|--------|-------|---------|
| `gpon_up` | gauge | | always | 1 if a poll succeeded within the last 3 intervals |
| `gpon_last_poll_success_timestamp_seconds` | gauge | | always | Unix time of the last successful poll |
| `gpon_poll_duration_seconds` | gauge | | always | Duration of the last poll |
| `gpon_poll_errors_total` | counter | | always | Failed polls since start |
| `gpon_exporter_baseline_phase` | gauge | | always | 1 during the baseline phase, else 0 |
| `gpon_state` | gauge | | full | ONU activation state: 1 to 7 = O1 to O7, 5 = O5 operational, 0 = unknown |
| `gpon_los` | gauge | | full | 1 if the stick reports loss of signal |
| `gpon_fec` | gauge | | full | 1 if forward error correction is active |
| `gpon_uni_up` | gauge | | full | 1 if the host-side (UNI/SGMII) port is up |
| `gpon_uni_speed_mbps` | gauge | | full | Host-side port speed in Mbit/s |
| `gpon_uni_full_duplex` | gauge | | full | 1 if the host-side port is full duplex |
| `gpon_bytes_total` | counter | `direction` | full | Octets on the PON side |
| `gpon_packets_total` | counter | `direction` | full | Packets on the PON side |
| `gpon_broadcast_packets_total` | counter | `direction` | full | Broadcast packets |
| `gpon_multicast_packets_total` | counter | `direction` | full | Multicast packets |
| `gpon_crc_errors_total` | counter | `direction` | full | CRC-errored packets |
| `gpon_errors_total` | counter | `direction` | full | Errored packets |
| `gpon_discards_total` | counter | `direction` | full | Discarded packets (see notes) |
| `gpon_drop_events_total` | counter | `direction` | full | Drop events (see notes) |
| `gpon_info` | gauge (1) | `serial`, `firmware`, `boot_version`, `hw_version`, `vendor_id`, `mac`, `model` | full | Static data from `gccli sys show` |
| `gpon_uptime_seconds` | gauge | | both | Uptime of the stick's Linux; resets on reboot |
| `gpon_stick_load1`, `gpon_stick_load5`, `gpon_stick_load15` | gauge | | both | Load averages of the stick |
| `gpon_stick_cpu_jiffies_total` | counter | `mode` = `user`, `nice`, `system`, `idle`, `iowait`, `irq`, `softirq`, `steal` | both | CPU time per mode, in raw jiffies from `/proc/stat` |
| `gpon_stick_memory_total_bytes` | gauge | | both | `MemTotal` |
| `gpon_stick_memory_available_bytes` | gauge | | both | `MemAvailable`, or `MemFree + Buffers + Cached` on old kernels |

`direction` is `downstream` or `upstream`. Every section of the batch except `status` is optional: if a command
is missing or prints something unexpected, only its metrics are left out, and the poll still succeeds. After a
failed poll, the last good values stay exported, so check `gpon_up` before you trust them.

CPU busy ratio, which needs no knowledge of the kernel's tick rate (HZ):

```promql
1 - sum by (instance) (rate(gpon_stick_cpu_jiffies_total{mode="idle"}[10m]))
  / sum by (instance) (rate(gpon_stick_cpu_jiffies_total[10m]))
```

## Alert ideas

```yaml
groups:
  - name: gpon
    rules:
      - alert: GponStickUnreachable
        expr: gpon_up == 0
        for: 5m
      - alert: GponNotOperational          # PON down, so internet down
        expr: gpon_state != 5
        for: 1m
      - alert: GponLossOfSignal
        expr: gpon_los == 1
      - alert: GponStickRebooted
        expr: gpon_uptime_seconds < 900
      - alert: GponUniDown
        expr: gpon_uni_up == 0
        for: 1m
      # Compare against 15 minutes ago instead of increase(): a brand-new series (first scrape, exporter
      # restart) then yields nothing instead of counting the stick's lifetime errors as new.
      - alert: GponPonErrors
        expr: |
          (max by (instance, direction) (gpon_crc_errors_total)
            - max by (instance, direction) (gpon_crc_errors_total offset 15m)) > 10
          or
          (max by (instance, direction) (gpon_errors_total)
            - max by (instance, direction) (gpon_errors_total offset 15m)) > 10
      - alert: GponStickMemoryLow
        expr: gpon_stick_memory_available_bytes / gpon_stick_memory_total_bytes < 0.1
        for: 15m
```

During the baseline phase the `gccli` metrics are absent, so rules on them stay silent. Rules that use `absent()` on
them would fire, so gate those on `gpon_exporter_baseline_phase == 0`.

## Security

- The stick ships with vendor default credentials, and its SSH login is root on the device that carries your
  internet connection. Restrict who can reach its SSH (and its web UI) to the exporter host, for example with
  the `src-address` match in the NAT example above, plus a firewall rule.
- Pin the host key with `GPON_KNOWN_HOSTS`. `GPON_INSECURE_ACCEPT_ANY_HOSTKEY=1` exists only for setups that can't
  pin yet.
- Keep the password in a file or Secret (`GPON_PASSWORD_FILE`), not in an environment variable.
- `gpon_info` carries the stick's GPON serial number and MAC address as labels, and OLTs commonly authenticate an ONT
  by its serial. Treat those metrics as sensitive, or drop the labels with relabeling before you share dashboards.
- The container runs as uid 10001 and works with a read-only root filesystem.

## Notes

- `dsDiscards`/`usDiscards` equal `dsDropEvents`/`usDropEvents` and grow continuously in normal operation at O5,
  with zero CRC errors. On a GPON ONT these are frames filtered on the PON side (other GEM ports, multicast), not
  faults. Don't alert on `gpon_discards_total` or `gpon_drop_events_total`; use `gpon_crc_errors_total` and
  `gpon_errors_total`.

## Development

```sh
pip install -r requirements.txt
python -m unittest discover -v tests
```

The tests use a synthetic fixture (`tests/fixtures/full.txt`) and need no stick. The GitHub workflow runs them,
then builds the image for linux/amd64 and linux/arm64. It pushes the image to `ghcr.io/snakebiteef2000/gpon-exporter`
on `main` (tag `sha-<commit>`) and on `v*` tags (the semver version, e.g. `0.1.0`).

## License

MIT, see [LICENSE](LICENSE).
