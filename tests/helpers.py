"""Shared test helpers: the synthetic fixture and small editing functions for it."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FULL = (Path(__file__).resolve().parent / "fixtures" / "full.txt").read_text()
GCCLI_SECTIONS = ("status", "stats", "port", "show")


def drop(text, *sections):
    """Remove the given @@sections (marker and body) from a batch output."""
    out, skip = [], False
    for line in text.split("\n"):
        if line.startswith("@@"):
            skip = line[2:].strip() in sections
        if not skip:
            out.append(line)
    return "\n".join(out)


def replace_section(text, section, body):
    """Replace the body of one @@section."""
    return drop(text, section).replace("@@end", "@@%s\n%s\n@@end" % (section, body))


BASELINE = drop(FULL, *GCCLI_SECTIONS)


def environ(**kw):
    """A minimal valid environment for load_config, overridable per test."""
    env = {"GPON_HOST": "ont.example.net", "GPON_INSECURE_ACCEPT_ANY_HOSTKEY": "1"}
    env.update({k: v for k, v in kw.items() if v is not None})
    for k in [k for k, v in kw.items() if v is None]:
        env.pop(k, None)
    return env
