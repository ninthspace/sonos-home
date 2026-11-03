import sys, os, time, subprocess, urllib.request, json
LOG = os.environ.get("PROBE_LOG", "/tmp/probe.log")
def log(*a):
    with open(LOG, "a") as f: f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
log("=== probe start", sys.executable, "uid", os.getuid(), "tty", os.isatty(0), "ppid", os.getppid())
import glob
sys.path[:0] = [os.environ["PROBE_SITE"]] if os.environ.get("PROBE_SITE") else glob.glob(os.environ.get("REPO", ".") + "/.venv/lib/python3.*/site-packages")
try:
    import soco
    zs = soco.discover(timeout=5, allow_network_scan=True) or set()
    log("discover:", sorted((z.player_name, z.ip_address) for z in zs))
except Exception as e: log("discover ERR", repr(e))
try:
    r = urllib.request.urlopen("http://192.168.0.21:1400/xml/device_description.xml", timeout=5); log("GET device_description:", r.status)
except Exception as e: log("GET ERR", repr(e))
f = os.environ.get("PROBE_FILE") or os.path.expanduser("~/Dropbox/opus-opus/z0ne2.wav")
try:
    out = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-show_entries", "format=duration,format_name", "-of", "json", f], capture_output=True, text=True, timeout=60)
    log("ffprobe rc", out.returncode, out.stdout.replace("\n", " ")[:200], out.stderr[:200])
except Exception as e: log("ffprobe ERR", repr(e))
try:
    n = len(open(f, "rb").read(1_000_000)); log("read 1MB from Dropbox ok", n)
except Exception as e: log("read ERR", repr(e))
log("=== probe end")
