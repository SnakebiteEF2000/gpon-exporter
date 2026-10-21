"""Configuration, host key policy, command batch, backoff and baseline phase tests. No network access."""
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import paramiko

from helpers import BASELINE, FULL, ROOT, environ
import gpon_exporter as ge


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_poller(clock=None, **env):
    return ge.Poller(ge.load_config(environ(**env)), clock=clock or FakeClock())


class TestConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(2048)
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def known_hosts(self, *names):
        path = os.path.join(self.tmp.name, "known_hosts_%d" % len(os.listdir(self.tmp.name)))
        with open(path, "w") as fh:
            for name in names:
                fh.write("%s ssh-rsa %s\n" % (name, self.key.get_base64()))
        return path

    def test_host_is_required(self):
        with self.assertRaisesRegex(ge.ConfigError, "GPON_HOST is required"):
            ge.load_config(environ(GPON_HOST=None))

    def test_defaults(self):
        cfg = ge.load_config(environ())
        self.assertEqual((cfg.port, cfg.user, cfg.interval, cfg.timeout, cfg.baseline_seconds, cfg.listen_port),
                         (22, "root", 30.0, 10.0, 0.0, 9617))
        self.assertEqual(cfg.password, "")

    def test_password_file(self):
        path = os.path.join(self.tmp.name, "pw")
        with open(path, "w") as fh:
            fh.write("not-a-real-password\n")
        self.assertEqual(ge.load_config(environ(GPON_PASSWORD_FILE=path)).password, "not-a-real-password")
        with self.assertRaisesRegex(ge.ConfigError, "GPON_PASSWORD_FILE"):
            ge.load_config(environ(GPON_PASSWORD_FILE=path + ".missing"))

    def test_bad_number(self):
        with self.assertRaisesRegex(ge.ConfigError, "GPON_PORT"):
            ge.load_config(environ(GPON_PORT="ssh"))

    def test_host_key_source_is_required(self):
        with self.assertRaisesRegex(ge.ConfigError, "GPON_KNOWN_HOSTS"):
            ge.load_config(environ(GPON_INSECURE_ACCEPT_ANY_HOSTKEY=None))
        with self.assertRaisesRegex(ge.ConfigError, "GPON_KNOWN_HOSTS"):
            ge.load_config(environ(GPON_INSECURE_ACCEPT_ANY_HOSTKEY="0"))

    def test_insecure_values(self):
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(ge.load_config(environ(GPON_INSECURE_ACCEPT_ANY_HOSTKEY=value)).insecure, value)

    def test_known_hosts_name(self):
        self.assertEqual(ge.known_hosts_name("ont.example.net", 22), "ont.example.net")
        self.assertEqual(ge.known_hosts_name("192.0.2.10", 2222), "[192.0.2.10]:2222")

    def test_known_hosts_with_nonstandard_port(self):
        path = self.known_hosts("[192.0.2.10]:2222")
        env = dict(GPON_HOST="192.0.2.10", GPON_INSECURE_ACCEPT_ANY_HOSTKEY=None, GPON_KNOWN_HOSTS=path)
        self.assertEqual(ge.load_config(environ(GPON_PORT="2222", **env)).known_hosts, path)
        # the same file does not cover port 22, and a plain "192.0.2.10" entry does not cover port 2222
        with self.assertRaisesRegex(ge.ConfigError, r"no host key for 192\.0\.2\.10 "):
            ge.load_config(environ(**env))
        env["GPON_KNOWN_HOSTS"] = self.known_hosts("192.0.2.10")
        with self.assertRaisesRegex(ge.ConfigError, r"\[192\.0\.2\.10\]:2222"):
            ge.load_config(environ(GPON_PORT="2222", **env))

    def test_known_hosts_hashed_entry(self):
        path = self.known_hosts(paramiko.HostKeys.hash_host("[192.0.2.10]:2222"))
        cfg = ge.load_config(environ(GPON_HOST="192.0.2.10", GPON_PORT="2222", GPON_KNOWN_HOSTS=path,
                                     GPON_INSECURE_ACCEPT_ANY_HOSTKEY=None))
        self.assertEqual(cfg.known_hosts, path)

    def test_known_hosts_missing_file(self):
        with self.assertRaisesRegex(ge.ConfigError, "cannot read GPON_KNOWN_HOSTS"):
            ge.load_config(environ(GPON_KNOWN_HOSTS=os.path.join(self.tmp.name, "nope")))

    def test_policy_selection(self):
        path = self.known_hosts("ont.example.net")
        pinned = ge.load_config(environ(GPON_KNOWN_HOSTS=path, GPON_INSECURE_ACCEPT_ANY_HOSTKEY=None))
        both = ge.load_config(environ(GPON_KNOWN_HOSTS=path))
        insecure = ge.load_config(environ())
        self.assertIsInstance(ge.host_key_policy(pinned), paramiko.RejectPolicy)
        self.assertIsInstance(ge.host_key_policy(both), paramiko.RejectPolicy)  # pinning wins
        self.assertIsInstance(ge.host_key_policy(insecure), ge.AcceptAnyHostKey)

    def test_reject_policy_rejects_unknown_key(self):
        with self.assertRaises(paramiko.SSHException):
            paramiko.RejectPolicy().missing_host_key(mock.Mock(), "[192.0.2.10]:2222", self.key)

    def test_accept_any_logs_a_pinnable_line(self):
        with self.assertLogs("gpon-exporter", "WARNING") as logs:
            ge.AcceptAnyHostKey().missing_host_key(None, "[192.0.2.10]:2222", self.key)
        self.assertIn("UNVERIFIED", logs.output[0])
        self.assertIn("[192.0.2.10]:2222 ssh-rsa " + self.key.get_base64(), logs.output[0])

    def test_connect_uses_the_selected_policy(self):
        path = self.known_hosts("ont.example.net")
        with mock.patch.object(ge.paramiko, "SSHClient", FakeSSHClient):
            FakeSSHClient.instances, FakeSSHClient.connect_error = [], None
            p = make_poller(GPON_KNOWN_HOSTS=path, GPON_INSECURE_ACCEPT_ANY_HOSTKEY=None)
            p.connect()
            p.connect()
            q = make_poller()
            q.connect()
        clients = FakeSSHClient.instances
        self.assertEqual(clients[0].host_keys_file, path)
        self.assertIsInstance(clients[0].policy, paramiko.RejectPolicy)
        self.assertTrue(clients[0].closed)  # the second connect closed the first session first
        self.assertFalse(clients[1].closed)
        self.assertIsNone(clients[2].host_keys_file)
        self.assertIsInstance(clients[2].policy, ge.AcceptAnyHostKey)

    def test_failed_connect_closes_the_half_open_session(self):
        errors = (paramiko.BadHostKeyException("ont.example.net", self.key, self.key),
                  paramiko.AuthenticationException("Authentication failed."), socket.timeout())
        for error in errors:
            with mock.patch.object(ge.paramiko, "SSHClient", FakeSSHClient):
                FakeSSHClient.instances, FakeSSHClient.connect_error = [], error
                p = make_poller()
                with self.assertRaises(type(error)):
                    p.connect()
            self.assertTrue(FakeSSHClient.instances[0].closed, type(error).__name__)
            self.assertIsNone(p.client)


class FakeSSHClient:
    """Stands in for paramiko.SSHClient in connect() tests."""
    instances, connect_error = [], None

    def __init__(self):
        self.policy, self.host_keys_file, self.closed = None, None, False
        FakeSSHClient.instances.append(self)

    def load_host_keys(self, path):
        self.host_keys_file = path

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, *a, **kw):
        if FakeSSHClient.connect_error:
            raise FakeSSHClient.connect_error

    def get_transport(self):
        return mock.Mock()

    def close(self):
        self.closed = True


class TestCommands(unittest.TestCase):
    READ_ONLY = re.compile(r"^(gccli (gpon status|gpon stats|port status 0|sys show)"
                           r"|cat /proc/(uptime|loadavg|meminfo)|grep '\^cpu ' /proc/stat)$")

    def test_every_command_is_on_the_read_only_allowlist(self):
        for _, cmd, _ in ge.COMMANDS:
            self.assertRegex(cmd, self.READ_ONLY)

    def test_full_batch(self):
        cmd = ge.remote_command(True)
        self.assertTrue(cmd.startswith('echo "@@status"; gccli gpon status; '))
        self.assertTrue(cmd.endswith('echo "@@end"'))
        for section, command, _ in ge.COMMANDS:
            self.assertIn('echo "@@%s"; %s; ' % (section, command), cmd)
        self.assertNotIn("&", cmd)  # nothing runs in parallel on the stick

    def test_baseline_batch_has_no_gccli(self):
        cmd = ge.remote_command(False)
        self.assertNotIn("gccli", cmd)
        self.assertEqual(re.findall(r"@@(\w+)", cmd), ["uptime", "loadavg", "cpu", "meminfo", "end"])


class TestBackoff(unittest.TestCase):
    def test_doubling_and_cap(self):
        self.assertEqual([ge.backoff_delay(n) for n in range(1, 8)], [60, 120, 240, 480, 600, 600, 600])

    def test_huge_failure_count_does_not_overflow(self):
        self.assertEqual(ge.backoff_delay(10 ** 6), 600)

    def test_step_schedule_backs_off_and_resets_after_success(self):
        p = make_poller()
        results = iter([OSError("refused"), OSError("refused"), socket.timeout("timed out"), FULL, OSError("reset")])

        def fetch(full=True):
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r
        p.fetch = fetch
        with self.assertLogs("gpon-exporter", "INFO"):
            delays = [p.step() for _ in range(5)]
        self.assertEqual(delays, [60, 120, 240, 30, 60])
        self.assertEqual((p.errors, p.failures), (4, 1))
        self.assertEqual(p.data["state"], 5)  # the last good poll stays published

    def test_slow_poll_shortens_the_next_sleep(self):
        clock = FakeClock()
        p = make_poller(clock)

        def fetch(full=True):
            clock.t += 12
            return FULL
        p.fetch = fetch
        self.assertEqual(p.step(), 18)


class FakeChannel:
    def __init__(self):
        self.closed = False
        self.exit_status = 0

    def exit_status_ready(self):
        return True

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, read):
        self.channel = FakeChannel()
        self.read = read


class FakeClient:
    """Stands in for a connected paramiko.SSHClient; `read` decides what a command batch returns."""

    def __init__(self, read):
        self.read, self.streams, self.closed = read, [], False

    def get_transport(self):
        return mock.Mock(is_active=lambda: not self.closed)

    def exec_command(self, cmd, timeout=None):
        out = FakeStream(self.read)
        self.streams.append(out)
        return None, out, FakeStream(lambda: b"")

    def close(self):
        self.closed = True


class TestFetch(unittest.TestCase):
    def test_one_batch_in_flight(self):
        state = {"now": 0, "max": 0}
        lock = threading.Lock()

        def read():
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return FULL.encode()
        p = make_poller()
        p.client = FakeClient(read)
        threads = [threading.Thread(target=p.fetch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1)
        self.assertEqual(len(p.client.streams), 4)
        self.assertTrue(all(s.channel.closed for s in p.client.streams))

    def test_timeout_closes_channel_and_session(self):
        def read():
            raise socket.timeout()  # paramiko raises it without a message
        p = make_poller()
        client = p.client = FakeClient(read)
        with self.assertLogs("gpon-exporter", "WARNING") as logs:
            delay = p.step()
        self.assertEqual(delay, 60)
        self.assertTrue(client.streams[0].channel.closed)
        self.assertTrue(client.closed)
        self.assertIsNone(p.client)
        self.assertIn("poll failed (1 in a row): TimeoutError; next attempt in 60s", logs.output[0])

    def test_truncated_output_is_a_failure(self):
        p = make_poller()
        p.client = FakeClient(lambda: FULL.replace("@@end", "").encode())
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            p.fetch()


class TestBaselinePhase(unittest.TestCase):
    def test_in_baseline(self):
        self.assertFalse(ge.in_baseline(0, 0))
        self.assertTrue(ge.in_baseline(0, 600))
        self.assertTrue(ge.in_baseline(599.9, 600))
        self.assertFalse(ge.in_baseline(600, 600))

    def test_poller_switches_from_baseline_to_full(self):
        clock = FakeClock()
        p = make_poller(clock, GPON_BASELINE_SECONDS="600")
        self.assertTrue(p.baseline_active)  # already before the first poll
        calls = []

        def fetch(full=True):
            calls.append(full)
            return FULL if full else BASELINE
        p.fetch = fetch
        p.step()
        self.assertEqual((calls, p.baseline_active, p.data["state"]), ([False], True, None))
        self.assertEqual(p.data["load1"], 0.15)
        clock.t += 599
        p.step()
        clock.t += 1
        with self.assertLogs("gpon-exporter", "INFO") as logs:
            p.step()
        self.assertEqual((calls, p.baseline_active, p.data["state"]), ([False, False, True], False, 5))
        self.assertIn("baseline phase over", logs.output[0])

    def test_no_baseline_by_default(self):
        p = make_poller()
        calls = []
        p.fetch = lambda full=True: calls.append(full) or FULL
        p.step()
        self.assertEqual((calls, p.baseline_active), ([True], False))


class TestStartup(unittest.TestCase):
    def run_exporter(self, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GPON_")}
        clean.update(env)
        return subprocess.run([sys.executable, str(ROOT / "gpon_exporter.py")], env=clean,
                              capture_output=True, text=True, timeout=30)

    def test_missing_host_exits_with_a_clear_error(self):
        r = self.run_exporter()
        self.assertEqual(r.returncode, 1)
        self.assertIn("GPON_HOST is required", r.stderr)

    def test_missing_host_key_source_exits_with_a_clear_error(self):
        r = self.run_exporter(GPON_HOST="ont.example.net")
        self.assertEqual(r.returncode, 1)
        self.assertIn("GPON_KNOWN_HOSTS", r.stderr)


if __name__ == "__main__":
    unittest.main()
