"""Scrub a raw pw-dump into a committable test fixture.

    pw-dump > /tmp/raw.json
    uv run python scripts/scrub_pw_dump.py /tmp/raw.json \
        tests/fixtures/pw_dump_real.json

Capture it while an application is playing audio, or the dump contains no
Stream/Output/Audio node and the fixture misses the case --app depends on.

Values are redacted by key and identifiers substituted by value, but every
object, key, nesting level and JSON type is kept intact: the fixture exists to
prove the parser handles the REAL schema, so the shape has to survive. Node
names are kept too - they carry hardware models and PCI paths, not personal
data, and the parser, `devices` and --app all key on them.

Prints what it redacted and re-checks the output for leaks. Read that report
before committing: a raw dump carries a username, hostname, machine-id, pids
and device serial numbers, and git history is forever.
"""
import json, os, re, subprocess, sys

RAW, OUT = sys.argv[1], sys.argv[2]

# Values replaced wherever they appear, in any key.
user = os.environ.get("USER") or subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()

# Keys whose value is redacted outright. Type is preserved.
REDACT = {
    "application.process.machine-id": "0" * 32,
    "application.process.session-id": "1",
    "application.process.id": "1000",
    "application.process.user": "user",
    "application.process.host": "host",
    "pipewire.sec.pid": 1000,
    "pipewire.sec.uid": 1000,
    "pipewire.sec.gid": 1000,
    "core.name": "pipewire-user-0",
    "device.serial": "redacted-serial",
    "device.sysfs.path": "/sys/devices/redacted",
    "device.bus-path": "redacted-bus-path",
    "alsa.card_name": "Redacted Card",
    "alsa.long_card_name": "Redacted Long Card Name",
    "api.alsa.card.longname": "Redacted Long Card Name",
    "api.alsa.card.name": "Redacted Card",
    "api.v4l2.cap.card": "Redacted Camera",
    "api.v4l2.cap.bus_info": "usb-0000:00:00.0-0",
    "api.v4l2.cap.driver": "uvcvideo",
    "host-name": "host",
    "user-name": "user",
}
# object.serial is deliberately NOT redacted: the parser keys on it and the
# regression test asserts every node has one.
KEEP = {"object.serial"}

report = {}

def scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in REDACT and k not in KEEP:
                replacement = REDACT[k]
                if isinstance(v, int) and not isinstance(replacement, int):
                    replacement = 1000
                if isinstance(v, str) and not isinstance(replacement, str):
                    replacement = str(replacement)
                report[k] = report.get(k, 0) + 1
                out[k] = replacement
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        s = obj
        if user:
            s = s.replace(user, "user")
        if host:
            s = s.replace(host, "host")
        if s != obj:
            report["<value substitution>"] = report.get("<value substitution>", 0) + 1
        return s
    return obj

data = json.load(open(RAW))
clean = scrub(data)
text = json.dumps(clean, indent=2) + "\n"
open(OUT, "w").write(text)

print("=== redactions applied ===")
for k, c in sorted(report.items()):
    print(f"  {k:38} x{c}")

print("\n=== leak check on the OUTPUT ===")
leaks = 0
for label, needle in (("username", user), ("hostname", host)):
    n = text.count(needle) if needle else 0
    print(f"  {label:10} '{needle}': {n} occurrences" + ("  <-- LEAK" if n else "  clean"))
    leaks += n
for label, pat in (("32-hex machine-id", r"\b[0-9a-f]{32}\b"), ("home path", r"/home/[a-z]")):
    found = [m for m in re.findall(pat, text) if m != "0" * 32]
    print(f"  {label:10}: {len(found)} occurrences" + ("  <-- LEAK" if found else "  clean"))
    leaks += len(found)
print(f"\nRESULT: {'LEAKS FOUND' if leaks else 'clean'}")
