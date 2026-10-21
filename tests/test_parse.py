"""Parser and exposition tests against the synthetic fixture."""
import time
import unittest

from prometheus_client import CollectorRegistry, generate_latest

from helpers import BASELINE, FULL, drop, environ, replace_section
import gpon_exporter as ge


def render(data, baseline_active=False):
    """Expose `data` through the real Collector and return the text exposition."""
    p = ge.Poller(ge.load_config(environ()))
    p.data, p.last_ok, p.baseline_active = data, (time.time() if data else 0.0), baseline_active
    reg = CollectorRegistry()
    reg.register(ge.Collector(p))
    return generate_latest(reg).decode()


class TestParseFull(unittest.TestCase):
    def setUp(self):
        self.d = ge.parse(FULL)

    def test_status_port_uptime(self):
        d = self.d
        self.assertEqual((d["state"], d["los"], d["fec"]), (5, 0, 1))
        self.assertEqual((d["uni_up"], d["uni_speed_mbps"], d["uni_full_duplex"]), (1, 1000, 1))
        self.assertEqual(d["uptime"], 86400.25)

    def test_counters(self):
        c = self.d["counters"]
        self.assertEqual(set(c), set(ge.STATS_MAP))
        self.assertEqual(c["dsOcts"], 987654321)
        self.assertEqual(c["usOcts"], 123456789)
        self.assertEqual(c["dsDiscards"], 7500)
        self.assertEqual(c["usCrcErrPkts"], 0)

    def test_info(self):
        self.assertEqual(self.d["info"], {
            "serial": "TEST00000001", "firmware": "V1.2.4", "boot_version": "V1.0.0", "hw_version": "V1.0",
            "vendor_id": "TEST", "mac": "00:00:5e:00:53:01", "model": "GPON-ONT-TEST",
        })

    def test_stick_load(self):
        d = self.d
        self.assertEqual((d["load1"], d["load5"], d["load15"]), (0.15, 0.10, 0.05))
        # guest and guest_nice (fields 9 and 10) are ignored
        self.assertEqual(d["cpu"], {"user": 1000, "nice": 20, "system": 3000, "idle": 400000,
                                    "iowait": 50, "irq": 6, "softirq": 70, "steal": 0})
        self.assertEqual(d["mem_total_bytes"], 120000 * 1024)
        self.assertEqual(d["mem_available_bytes"], 60000 * 1024)


class TestParseOptional(unittest.TestCase):
    def test_missing_optional_sections_still_parse(self):
        d = ge.parse(drop(FULL, "stats", "port", "uptime", "show", "loadavg", "cpu", "meminfo"))
        self.assertEqual(d["state"], 5)
        self.assertEqual((d["counters"], d["info"], d["cpu"]), ({}, {}, {}))
        for key in ("uni_up", "uni_speed_mbps", "uni_full_duplex", "uptime", "load1", "load5", "load15",
                    "mem_total_bytes", "mem_available_bytes"):
            self.assertIsNone(d[key], key)

    def test_each_new_section_is_optional_on_its_own(self):
        for section in ("loadavg", "cpu", "meminfo"):
            d = ge.parse(drop(FULL, section))
            self.assertEqual(d["state"], 5, section)

    def test_garbage_in_optional_sections_is_ignored(self):
        text = FULL
        for section in ("loadavg", "cpu", "meminfo"):
            text = replace_section(text, section, "sh: not found")
        d = ge.parse(text)
        self.assertIsNone(d["load1"])
        self.assertEqual(d["cpu"], {})
        self.assertIsNone(d["mem_total_bytes"])

    def test_meminfo_fallback_without_memavailable(self):
        body = "MemTotal: 120000 kB\nMemFree: 30000 kB\nBuffers: 2000 kB\nCached: 20000 kB"
        d = ge.parse(replace_section(FULL, "meminfo", body))
        self.assertEqual(d["mem_available_bytes"], (30000 + 2000 + 20000) * 1024)

    def test_meminfo_without_any_available_source(self):
        d = ge.parse(replace_section(FULL, "meminfo", "MemTotal: 120000 kB"))
        self.assertEqual(d["mem_total_bytes"], 120000 * 1024)
        self.assertIsNone(d["mem_available_bytes"])

    def test_short_cpu_line_of_an_old_kernel(self):
        d = ge.parse(replace_section(FULL, "cpu", "cpu  10 0 20 300"))
        self.assertEqual(d["cpu"], {"user": 10, "nice": 0, "system": 20, "idle": 300})

    def test_unknown_state_is_zero_in_a_full_poll(self):
        d = ge.parse(replace_section(FULL, "status", "gccli: busy"))
        self.assertEqual(d["state"], 0)
        self.assertIsNone(d["los"])


class TestParseIncomplete(unittest.TestCase):
    def test_missing_status_fails(self):
        with self.assertRaises(ValueError):
            ge.parse(drop(FULL, "status"))

    def test_missing_end_fails(self):
        with self.assertRaises(ValueError):
            ge.parse(FULL.replace("@@end", ""))

    def test_empty_output_fails(self):
        with self.assertRaises(ValueError):
            ge.parse("")

    def test_baseline_needs_only_end(self):
        d = ge.parse(BASELINE, full=False)
        self.assertEqual(d["uptime"], 86400.25)
        with self.assertRaises(ValueError):
            ge.parse(BASELINE.replace("@@end", ""), full=False)


class TestBaselineParse(unittest.TestCase):
    def test_no_gccli_values_in_a_baseline_poll(self):
        d = ge.parse(BASELINE, full=False)
        for key in ("state", "los", "fec", "uni_up", "uni_speed_mbps", "uni_full_duplex"):
            self.assertIsNone(d[key], key)  # None, not 0: gpon_state must not be exported as "unknown"
        self.assertEqual((d["counters"], d["info"]), ({}, {}))
        self.assertEqual(d["load1"], 0.15)
        self.assertEqual(d["cpu"]["idle"], 400000)
        self.assertEqual(d["mem_available_bytes"], 60000 * 1024)


class TestExposition(unittest.TestCase):
    """The metric names are an interface (dashboards and alert rules depend on them): keep them stable."""

    def test_full_poll_metric_names(self):
        text = render(ge.parse(FULL))
        for line in (
            "gpon_up 1.0", "gpon_state 5.0", "gpon_los 0.0", "gpon_fec 1.0", "gpon_uptime_seconds 86400.25",
            "gpon_uni_up 1.0", "gpon_uni_speed_mbps 1000.0", "gpon_uni_full_duplex 1.0",
            'gpon_bytes_total{direction="downstream"} 9.87654321e+08',
            'gpon_bytes_total{direction="upstream"} 1.23456789e+08',
            'gpon_crc_errors_total{direction="upstream"} 0.0',
            'gpon_discards_total{direction="downstream"} 7500.0',
            "gpon_poll_errors_total 0.0", "gpon_poll_duration_seconds 0.0",
            "gpon_exporter_baseline_phase 0.0",
            "gpon_stick_load1 0.15", "gpon_stick_load5 0.1", "gpon_stick_load15 0.05",
            'gpon_stick_cpu_jiffies_total{mode="user"} 1000.0',
            'gpon_stick_cpu_jiffies_total{mode="idle"} 400000.0',
            'gpon_stick_cpu_jiffies_total{mode="steal"} 0.0',
            "gpon_stick_memory_total_bytes 1.2288e+08",
            "gpon_stick_memory_available_bytes 6.144e+07",
        ):
            self.assertIn(line + "\n", text)
        for suffix in ("packets", "broadcast_packets", "multicast_packets", "errors", "drop_events"):
            for direction in ("downstream", "upstream"):
                self.assertIn('gpon_%s_total{direction="%s"}' % (suffix, direction), text)
        self.assertIn('gpon_info{boot_version="V1.0.0",firmware="V1.2.4",hw_version="V1.0",mac="00:00:5e:00:53:01",'
                      'model="GPON-ONT-TEST",serial="TEST00000001",vendor_id="TEST"} 1.0', text)
        self.assertEqual(text.count("gpon_stick_cpu_jiffies_total{"), 8)
        self.assertIn("# TYPE gpon_stick_cpu_jiffies_total counter", text)
        self.assertRegex(text, r"\ngpon_last_poll_success_timestamp_seconds 1\.\d+e\+09\n")

    def test_missing_optional_sections_drop_only_their_metrics(self):
        text = render(ge.parse(drop(FULL, "loadavg", "cpu", "meminfo")))
        self.assertIn("gpon_state 5.0\n", text)
        self.assertNotIn("gpon_stick_", text)

    def test_baseline_poll_exports_no_gccli_metrics(self):
        text = render(ge.parse(BASELINE, full=False), baseline_active=True)
        self.assertIn("gpon_exporter_baseline_phase 1.0\n", text)
        self.assertIn("gpon_uptime_seconds 86400.25\n", text)
        self.assertIn("gpon_stick_load1 0.15\n", text)
        self.assertIn("gpon_stick_memory_available_bytes", text)
        for name in ("gpon_state", "gpon_los", "gpon_fec", "gpon_uni_", "gpon_info", "gpon_bytes_total"):
            self.assertNotIn(name, text)

    def test_before_the_first_poll(self):
        text = render(None)
        self.assertIn("gpon_up 0.0\n", text)
        self.assertIn("gpon_exporter_baseline_phase 0.0\n", text)
        self.assertNotIn("gpon_state", text)


if __name__ == "__main__":
    unittest.main()
