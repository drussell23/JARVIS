"""macOS-native local resource black-box for the omni-soak diagnostic.

Streams the death curve (RSS / free% / cpu / ctx-rate / swap / disk) to the
operator's terminal + a tee log, every ~1s. Avoids psutil.swap_memory()
(raises OSError on macOS) — uses native `sysctl vm.swapusage` + `vm_stat`.
"""
from __future__ import annotations
import argparse, os, subprocess, sys, time

_LAST_CTX = {"n": None, "ts": None}


def _vm_swapusage():
    """(used_mb, pageouts) from native macOS `sysctl vm.swapusage` + vm_stat."""
    used_mb, pageouts = 0.0, 0
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "vm.swapusage"], text=True, timeout=2)
        # e.g. "total = 2048.00M  used = 512.00M  free = 1536.00M ..."
        for tok in out.replace("=", " ").split():
            if tok.endswith("M"):
                pass
        parts = out.split("used")
        if len(parts) > 1:
            used_mb = float(parts[1].split("=")[1].split("M")[0].strip())
    except Exception:
        pass
    try:
        vms = subprocess.check_output(["vm_stat"], text=True, timeout=2)
        for line in vms.splitlines():
            if "Pageouts" in line:
                pageouts = int(line.split(":")[1].strip().rstrip("."))
    except Exception:
        pass
    return used_mb, pageouts


def _free_pct():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.available / vm.total * 100.0, 1)
    except Exception:
        return -1.0


def _rss_tree_mb():
    try:
        from backend.core.ouroboros.governance.process_tree_probe import (
            probe_process_tree_rss_mb)
        v = probe_process_tree_rss_mb()
        return round(v, 1) if v else -1.0
    except Exception:
        return -1.0


def _ctx_rate():
    try:
        import psutil
        now = time.monotonic()
        n = int(psutil.cpu_stats().ctx_switches)
        prev_n, prev_ts = _LAST_CTX["n"], _LAST_CTX["ts"]
        _LAST_CTX["n"], _LAST_CTX["ts"] = n, now
        if prev_n is None or now <= (prev_ts or now):
            return 0.0
        return round((n - prev_n) / (now - prev_ts), 1)
    except Exception:
        return -1.0


def _cpu_pct():
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        return -1.0


def _disk_free_pct():
    try:
        st = os.statvfs(".")
        return round(st.f_bavail / st.f_blocks * 100.0, 1)
    except Exception:
        return -1.0


def sample() -> dict:
    used_mb, pageouts = _vm_swapusage()
    return {
        "ts": time.time(),
        "rss_tree_mb": _rss_tree_mb(),
        "free_pct": _free_pct(),
        "cpu_pct": _cpu_pct(),
        "ctx_rate": _ctx_rate(),
        "swap_used_mb": used_mb,
        "swap_pageouts": pageouts,
        "disk_free_pct": _disk_free_pct(),
    }


def _fmt(s: dict) -> str:
    return ("rss={rss_tree_mb}MB free={free_pct}% cpu={cpu_pct}% "
            "ctx={ctx_rate}/s swap={swap_used_mb}MB pageouts={swap_pageouts} "
            "disk_free={disk_free_pct}%").format(**s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--log", default="logs/resource_blackbox_local.log")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    sample()  # seed ctx baseline
    time.sleep(min(0.5, args.interval))
    with open(args.log, "a") as fh:
        while True:
            line = _fmt(sample())
            sys.stdout.write(line + "\n"); sys.stdout.flush()
            fh.write(line + "\n"); fh.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
