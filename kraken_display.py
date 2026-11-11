#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Kraken Z (Z53/Z63/Z73) + Smart Device V2 + RAM lights controller for a headless Linux PC.

Every update (5 s) the CPU load points to a mode (idle, light, active, busy, heavy,
max by default). The first reading after start-up sets it directly; after
that a higher mode needs the load to stay there for modes.up_after_min (1 min); going
down it steps through every mode in between, one at a time, spread over
modes.down_after_min (3 min). Each mode sets the light color and
brightness, the pump screen brightness and a minimum fan speed. On top of that:
- every fan and the pump also follow their liquid-temperature curve and a CPU-
  temperature boost, with fast spin-up and gentle spin-down,
- QUIET: after quiet.after_min minutes below quiet.load_below % load (or straight away
  at start-up if the first 5 s are idle), once the mode is quiet.allowed_up_to or
  lower (from higher modes it first steps down), all fans go to quiet.fan_duty %, the
  pump to quiet.pump_duty %, and the
  lights and screen dim; load lasting quiet.wake_after_min (1 min) wakes it, and so
  does rising temperature, immediately.
- DARK: below the mode dark.show_from, all lights are off and the pump screen is black.
- LCD: power, CPU and GPU load, CPU and liquid temps, RAM and GPU memory bars, a
  gradient gauge with a dot on the current mode, and a bottom line that cycles through
  screen.texts every screen.text_seconds (fan %, coming mode change, quiet countdown...).
- Status file:  watch -n2 cat /run/kraken-display/status.txt

All settings live in a JSON file (default /etc/kraken-display.json; change it with
--config PATH or the KRAKEN_DISPLAY_CONFIG environment variable), so this script can be
replaced without losing them. Show the settings in use:  python3 kraken_display.py --print-config
"""
import copy
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from liquidctl import find_liquidctl_devices
from PIL import Image, ImageDraw, ImageFont

CONFIG = os.environ.get("KRAKEN_DISPLAY_CONFIG", "/etc/kraken-display.json")

# Fallback only: used when the settings file is missing, a setting is missing, or a
# value is invalid, so the fans stay controlled even if the file has a typo.
DEFAULT_CONFIG = {
    "_about": "Settings for kraken_display.py. After editing: "
              "systemctl restart kraken-display",
    "modes": {
        "_help": "Picked by CPU load (smoothed, updated every 5 s). At start-up the first "
                 "5 s reading sets the mode directly. Up: the load must stay in a higher "
                 "mode for up_after_min, then it jumps there. Down: it steps through every "
                 "mode in between, one at a time, spread evenly over down_after_min. load_to: upper load % of the mode. color + lights: hub/RAM "
                 "light color and brightness %. screen: pump screen brightness %. fan_min / "
                 "pump_min: minimum duty % for all fans / the pump in this mode. Add, remove or rename modes "
                 "freely; they're sorted by load_to.",
        "up_after_min": 1,
        "down_after_min": 3,
        "list": [
            {"name": "idle", "load_to": 17, "color": "#ffffff", "lights": 30, "screen": 40, "fan_min": 0, "pump_min": 0},
            {"name": "light", "load_to": 33, "color": "#ffc8c8", "lights": 44, "screen": 52, "fan_min": 25, "pump_min": 25},
            {"name": "active", "load_to": 50, "color": "#ff8c8c", "lights": 58, "screen": 64, "fan_min": 40, "pump_min": 40},
            {"name": "busy", "load_to": 67, "color": "#ff4d4d", "lights": 72, "screen": 76, "fan_min": 55, "pump_min": 55},
            {"name": "heavy", "load_to": 83, "color": "#e00000", "lights": 86, "screen": 88, "fan_min": 70, "pump_min": 70},
            {"name": "max", "load_to": 100, "color": "#8b0000", "lights": 100, "screen": 100, "fan_min": 85, "pump_min": 100},
        ],
    },
    "quiet": {
        "_help": "After after_min minutes below load_below % CPU load, all fans go to "
                 "fan_duty %, the pump to pump_duty %, lights/screen dim, as long as the mode is allowed_up_to "
                 "or lower; from a higher mode it waits until the mode has stepped down "
                 "(modes.down_after_min), so the fans cool things off first. at_start: go "
                 "quiet right away if the first 5 s after starting are idle. Load above "
                 "load_below for wake_after_min ends it; liquid >= max_liquid or CPU >= "
                 "max_cpu ends it immediately.",
        "after_min": 1,
        "allowed_up_to": "active",
        "load_below": 10,
        "at_start": True,
        "fan_duty": 10,
        "pump_duty": 80,
        "wake_after_min": 1,
        "max_liquid": 38,
        "max_cpu": 60,
    },
    "screen": {
        "_help": "rotate: 0/90/180/270. interval: seconds between sensor/fan updates. "
                 "quiet_brightness: screen % in quiet mode. texts: what the bottom line cycles through, every "
                 "text_seconds: fans (radiator fan %), next (coming change: next mode, "
                 "quiet, waking up), pump (pump %), time (clock). Empty ones are skipped.",
        "rotate": 0,
        "interval": 5,
        "quiet_brightness": 20,
        "texts": ["fans", "next"],
        "text_seconds": 3,
    },
    "lights": {
        "_help": "mode: auto (follow the modes) or off. quiet_brightness: % in quiet mode.",
        "mode": "auto",
        "quiet_brightness": 20,
    },
    "dark": {
        "_help": "Lights and pump screen only from the mode show_from up; below it all "
                 "lights are off and the screen is black (fans keep working). Modes go up "
                 "only after modes.up_after_min, so short bursts stay dark. show_from: the "
                 "first mode's name = always on.",
        "show_from": "heavy",
    },
    "fans": {
        "_help": "Curves are [x, duty %] points. radiator/case/pump: x = liquid C; they "
                 "stay at their minimum up to 34 C and ramp up above it. temp_boost: x = smoothed CPU "
                 "C. Each fan and the pump run at the highest of their curve, the temp "
                 "boost and the mode's fan_min/pump_min. ramp_up/ramp_down: max % change "
                 "per update. NZXT advises against running the pump low for long periods, "
                 "so it stays at 80 % or more; the Kraken itself won't go below 20 %.",
        "names": {
            "kraken": "radiator pull fans 1-2 (Kraken)",
            "fan1": "radiator push fans 1-2 (hub fan1)",
            "fan2": "rear case fan (hub fan2)",
            "fan3": "top case fan (hub fan3)",
        },
        "radiator_curve": [[34, 20], [36, 35], [38, 55], [40, 75], [42, 100]],
        "case_curve": [[34, 20], [37, 40], [40, 60], [43, 80]],
        "pump_curve": [[34, 80], [38, 90], [40, 100]],
        "temp_boost": [[65, 0], [75, 50], [85, 80], [92, 100]],
        "temp_smoothing": 0.35,
        "load_smoothing": 0.4,
        "ramp_up": 20,
        "ramp_down": 4,
    },
    "openrgb": {
        "host": "127.0.0.1",
        "port": 6742,
    },
}

# setting in the file -> variable used by the code below
KEYMAP = {
    "quiet.after_min": "QUIET_AFTER_MIN", "quiet.load_below": "QUIET_LOAD",
    "quiet.allowed_up_to": "QUIET_UP_TO_NAME",
    "quiet.at_start": "QUIET_AT_START", "quiet.fan_duty": "QUIET_DUTY",
    "quiet.pump_duty": "QUIET_PUMP",
    "quiet.wake_after_min": "QUIET_WAKE_MIN", "quiet.max_liquid": "QUIET_MAX_LIQUID",
    "modes.up_after_min": "MODE_UP_MIN", "modes.down_after_min": "MODE_DOWN_MIN",
    "quiet.max_cpu": "QUIET_MAX_CPU",
    "screen.rotate": "ROTATE", "screen.interval": "INTERVAL",
    "screen.quiet_brightness": "SCREEN_QUIET", "screen.texts": "TEXTS",
    "screen.text_seconds": "TEXT_SECONDS",
    "lights.mode": "LIGHTS",
    "dark.show_from": "DARK_FROM_NAME", "lights.quiet_brightness": "QUIET_LIGHTS",
    "fans.names": "NAMES", "fans.radiator_curve": "FAN", "fans.case_curve": "CASE",
    "fans.pump_curve": "PUMP", "fans.temp_boost": "BOOST",
    "fans.temp_smoothing": "CPU_SMOOTHING", "fans.load_smoothing": "LOAD_SMOOTHING",
    "fans.ramp_up": "RAMP_UP", "fans.ramp_down": "RAMP_DOWN",
}

CHANNELS = ("kraken", "fan1", "fan2", "fan3")
# Own directory, not /tmp: the service runs as root, and fixed names in a world-writable
# directory could be pre-created as symlinks by another user. systemd creates it
# (RuntimeDirectory=) and passes its path; otherwise it's created here.
RUN_DIR = os.environ.get("RUNTIME_DIRECTORY", "/run/kraken-display").split(":")[0]
IMG = os.path.join(RUN_DIR, "screen.png")
STATUS = os.path.join(RUN_DIR, "status.txt")
HUB_EXIT_DUTY = 60     # hub fans on stop: they have no curve of their own to fall back to
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
RAPL = "/sys/class/powercap/intel-rapl:0"
RETRY_S = 60
UNSET = object()


def log(msg):
    print(msg, flush=True)


# ---- settings --------------------------------------------------------------------
def hex_rgb(text):
    """'#ff8c8c' -> (255, 140, 140)"""
    text = str(text).strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"bad color '{text}'")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def parse_modes(items):
    modes = []
    for m in items:
        modes.append({
            "name": str(m["name"]), "load_to": float(m["load_to"]),
            "color": hex_rgb(m["color"]), "lights": float(m["lights"]),
            "screen": int(m["screen"]), "fan_min": int(m["fan_min"]),
            "pump_min": int(m.get("pump_min", m["fan_min"])),
        })
    if not modes:
        raise ValueError("no modes")
    modes.sort(key=lambda m: m["load_to"])
    modes[-1]["load_to"] = max(modes[-1]["load_to"], 100.0)  # last mode covers the rest
    return modes


def dump_config(cfg):
    """JSON text with each [x, duty] pair and each mode on one line, easy to edit."""
    text = json.dumps(cfg, indent=2, ensure_ascii=False)
    text = re.sub(r"\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]", r"[\1, \2]", text)
    text = re.sub(r"\{\s+(\"name\"[^{}]*?)\s+\}",
                  lambda m: "{" + re.sub(r",\s+", ", ", m.group(1)) + "}", text)
    return text + "\n"


def _same_type(default, value):
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def clean_curve(points, default, key, say):
    """[[x, duty], ...] sorted by x, duty clamped to 0-100; the default if malformed."""
    try:
        out = sorted((float(x), min(max(float(d), 0), 100)) for x, d in points)
        if not out:
            raise ValueError("empty")
        return [(x, round(d)) for x, d in out]
    except (TypeError, ValueError) as e:
        say(f"settings: '{key}' is not a list of [x, duty] points ({e}), using the default")
        return [tuple(p) for p in default]


def load_config(path=CONFIG, quiet=False):
    """Defaults, overridden by the settings file; sets the globals, returns the settings."""
    say = (lambda m: None) if quiet else log
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        say(f"no {path}, using built-in settings")
        data = {}
    except Exception as e:
        say(f"{path} unreadable ({e}), using built-in settings")
        data = {}
    for group, values in data.items():
        if group.startswith("_"):
            continue
        if group not in cfg or not isinstance(values, dict):
            say(f"settings: unknown section '{group}' ignored")
            continue
        for key, value in values.items():
            if key.startswith("_"):
                continue
            if key not in cfg[group]:
                say(f"settings: unknown setting '{group}.{key}' ignored")
            elif not _same_type(cfg[group][key], value):
                say(f"settings: '{group}.{key}' has the wrong type, using the default")
            elif key == "names":
                cfg[group][key].update({c: n for c, n in value.items() if c in CHANNELS})
            else:
                cfg[group][key] = value
    g = globals()
    for key, var in KEYMAP.items():
        group, name = key.split(".")
        value = cfg[group][name]
        if var in ("FAN", "CASE", "PUMP", "BOOST"):
            value = clean_curve(value, DEFAULT_CONFIG[group][name], key, say)
        elif var == "NAMES":
            value = dict(value)
        g[var] = value
    g["OPENRGB"] = (cfg["openrgb"]["host"], cfg["openrgb"]["port"])
    try:
        g["MODES"] = parse_modes(cfg["modes"]["list"])
    except Exception as e:
        say(f"settings: modes.list invalid ({e!r}), using the default modes")
        g["MODES"] = parse_modes(DEFAULT_CONFIG["modes"]["list"])
    names = [m["name"].lower() for m in g["MODES"]]
    wanted = str(cfg["quiet"]["allowed_up_to"]).lower()
    if wanted in names:
        g["QUIET_UP_TO"] = names.index(wanted)
    else:
        g["QUIET_UP_TO"] = min(2, len(names) - 1)
        say(f"settings: quiet.allowed_up_to '{wanted}' is not a mode name, "
            f"using '{names[g['QUIET_UP_TO']]}'")
    wanted = str(cfg["dark"]["show_from"]).lower()
    if wanted in names:
        g["DARK_FROM"] = names.index(wanted)
    else:
        g["DARK_FROM"] = 0
        say(f"settings: dark.show_from '{wanted}' is not a mode name, lights always on")
    return cfg


# ---- sensors -----------------------------------------------------------------------
def retry(fn, *args, tries=5):
    """The NZXT devices sometimes drop a USB reply; just try again."""
    for i in range(tries):
        try:
            return fn(*args)
        except Exception as e:
            log(f"{fn.__name__} attempt {i + 1}/{tries} failed: {e}")
            time.sleep(1)
    raise RuntimeError(f"{fn.__name__} failed after {tries} attempts")


def curve_duty(curve, x):
    """Linear interpolation between curve points."""
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, d0), (x1, d1) in zip(curve, curve[1:]):
        if x <= x1:
            return round(d0 + (d1 - d0) * (x - x0) / (x1 - x0))
    return curve[-1][1]


CPU_SENSORS = ("coretemp", "k10temp", "zenpower")   # Intel, AMD, AMD (out-of-tree driver)
_cpu_hwmon = None


def cpu_temp():
    """CPU package temperature in C, or None when no known sensor exists."""
    global _cpu_hwmon
    if _cpu_hwmon is None:
        found = {}
        for h in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(f"{h}/name") as f:
                    found.setdefault(f.read().strip(), h)
            except OSError:
                pass
        _cpu_hwmon = next((found[n] for n in CPU_SENSORS if n in found), "")
        if not _cpu_hwmon:
            log("no CPU temperature sensor (coretemp/k10temp/zenpower): no temp boost, "
                "quiet mode disabled")
    if not _cpu_hwmon:
        return None
    try:
        # temp1 is the package (Intel) or Tctl (AMD) reading
        with open(f"{_cpu_hwmon}/temp1_input") as t:
            return int(t.read()) / 1000
    except OSError:
        return None


_cpu_prev = None


def cpu_load():
    """CPU utilisation in % (all threads) since the previous call, from /proc/stat."""
    global _cpu_prev
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    idle, total = vals[3] + vals[4], sum(vals)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if prev is None or total == prev[1]:
        return 0.0
    return 100 * (1 - (idle - prev[0]) / (total - prev[1]))


_rapl_prev = None


def cpu_power():
    """CPU package power in W since the previous call (Intel RAPL), None if unavailable.
    This is the CPU only, not the whole PC from the wall."""
    global _rapl_prev
    try:
        with open(f"{RAPL}/energy_uj") as f:
            energy = int(f.read())
        with open(f"{RAPL}/max_energy_range_uj") as f:
            wrap = int(f.read())
    except OSError:
        return None
    now = time.monotonic()
    prev, _rapl_prev = _rapl_prev, (energy, now)
    if prev is None or now <= prev[1]:
        return None
    used = energy - prev[0]
    if used < 0:          # counter wrapped around
        used += wrap
    return used / 1e6 / (now - prev[1])


def ram_gb():
    """(used, total) in GB, where used = total - available (same as `free`)."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.split()[0])  # kB
    total = info["MemTotal"] / 1048576
    return total - info["MemAvailable"] / 1048576, total


_igpu_prev = None


def _igpu_load():
    """Intel built-in graphics busy % since the previous call, from the time the GPU
    spent in its RC6 sleep state (busy = not asleep). None if not available."""
    global _igpu_prev
    paths = (glob.glob("/sys/class/drm/card[0-9]*/gt/gt0/rc6_residency_ms")
             + glob.glob("/sys/class/drm/card[0-9]*/power/rc6_residency_ms")
             + glob.glob("/sys/class/drm/card[0-9]*/device/tile0/gt0/gtidle/idle_residency_ms"))
    if not paths:
        return None
    try:
        with open(paths[0]) as f:
            asleep = int(f.read())
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    prev, _igpu_prev = _igpu_prev, (asleep, now)
    if prev is None or now <= prev[1] or asleep < prev[0]:
        return None
    busy = 1 - (asleep - prev[0]) / 1000 / (now - prev[1])
    return min(max(busy * 100, 0.0), 100.0)


def gpu_info():
    """Graphics card or built-in graphics:
      {"kind": "nvidia"/"amd", "used": GB, "total": GB, "load": %}  for graphics cards,
      {"kind": "igpu", "load": %}  for Intel built-in graphics (it borrows main RAM and
      has no memory of its own), or None. Several cards are added up (load: average)."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=True).stdout
            rows = [[float(x) for x in line.split(",")] for line in out.strip().splitlines()]
            if rows:
                return {"kind": "nvidia", "used": sum(r[0] for r in rows) / 1024,
                        "total": sum(r[1] for r in rows) / 1024,
                        "load": sum(r[2] for r in rows) / len(rows)}
        except Exception:
            pass
    used = total = 0
    loads = []
    for card in glob.glob("/sys/class/drm/card[0-9]*/device"):
        try:
            with open(f"{card}/mem_info_vram_used") as f:
                card_used = int(f.read())
            with open(f"{card}/mem_info_vram_total") as f:
                card_total = int(f.read())
        except OSError:
            continue
        used, total = used + card_used, total + card_total
        try:
            with open(f"{card}/gpu_busy_percent") as f:
                loads.append(float(f.read()))
        except (OSError, ValueError):
            pass
    if total:
        return {"kind": "amd", "used": used / 1024 ** 3, "total": total / 1024 ** 3,
                "load": sum(loads) / len(loads) if loads else None}
    for card in glob.glob("/sys/class/drm/card[0-9]*/device/vendor"):
        try:
            with open(card) as f:
                if f.read().strip() == "0x8086":
                    return {"kind": "igpu", "load": _igpu_load()}
        except OSError:
            continue
    return None


# ---- modes, fans, lights -----------------------------------------------------------
def mode_index(load):
    for i, m in enumerate(MODES):
        if load < m["load_to"]:
            return i
    return len(MODES) - 1


class ModeTimer:
    """The current mode.
    Up:   after the smoothed load has pointed higher for MODE_UP_MIN, it jumps straight to
          the mode of the average load over that wait.
    Down: once the load has dropped, it steps down ONE mode at a time, spreading the steps
          evenly over MODE_DOWN_MIN, so max -> idle passes heavy, busy, active and light.
    Short dips don't restart a wait: only GRACE updates in a row against it cancel it.
    The first reading after start-up is taken as is."""

    GRACE = 3     # updates (~15 s) against the trend before a pending change is dropped

    def __init__(self):
        self.current = None
        self.changed_at = float("-inf")   # when the mode last changed
        self._reset()

    def _reset(self, way=0, now=None):
        self.way, self.since, self.total, self.count, self.against = way, now, 0.0, 0, 0
        self.start = self.current          # mode when this change started

    def _set(self, mode, now):
        if mode != self.current:
            self.current, self.changed_at = mode, now

    def update(self, smooth, raw, now):
        level = mode_index(smooth)
        if self.current is None:
            self.current = mode_index(raw)
            return self.current
        way = (level > self.current) - (level < self.current)
        if self.way == 0:
            if way:
                self._reset(way, now)
        elif way != self.way:
            self.against += 1
            if self.against >= self.GRACE:          # the trend really changed
                self._reset(way, now if way else None)
        else:
            self.against = 0
        if self.way > 0:
            self.total, self.count = self.total + raw, self.count + 1
            if now - self.since >= MODE_UP_MIN * 60:
                target = self.target()
                if target > self.current:
                    self._set(target, now)
                self._reset()
        elif self.way < 0:
            self.total, self.count = self.total + raw, self.count + 1
            final = self.final(level)
            wait = MODE_DOWN_MIN * 60
            steps = self.start - final                 # modes to pass on the way down
            done = min(steps, int(steps * (now - self.since) / wait)) if steps > 0 else 0
            if self.start - done < self.current:
                self._set(self.start - done, now)      # one step at a time
            if self.current <= final and now - self.since >= wait:
                self._reset()
        return self.current

    def target(self):
        """Mode of the average load while waiting."""
        return mode_index(self.total / max(self.count, 1))

    def final(self, level=None):
        """Where a step-down ends: the mode of the average load, but not below the mode the
        load is in right now, and at least one step below where it started."""
        final = min(self.target(), self.start - 1)
        return max(final, level) if level is not None and level < self.start else final

    def pending_info(self, now):
        """(name of the mode it's heading to, monotonic time it gets there) or None."""
        if self.way > 0 and self.target() > self.current:
            return MODES[self.target()]["name"], self.since + MODE_UP_MIN * 60
        if self.way < 0 and self.final() < self.current:
            return MODES[self.final()]["name"], self.since + MODE_DOWN_MIN * 60
        return None

    def stepping_from(self):
        """Mode a step-down started from, while one is in progress; otherwise None."""
        return self.start if self.way < 0 and self.final() < self.current else None

    def progress(self, now):
        """(next mode, share of the way to it 0..1, final mode) while a change is pending.
        Going down, 'next' is the next single step; going up it's the final mode."""
        if not self.pending_info(now):
            return None
        if self.way > 0:
            done = (now - self.since) / (MODE_UP_MIN * 60)
            return self.target(), min(max(done, 0.0), 1.0), self.target()
        final = self.final()
        step = MODE_DOWN_MIN * 60 / max(self.start - final, 1)
        nxt_at = self.since + (self.start - self.current + 1) * step
        done = 1 - (nxt_at - now) / step
        return self.current - 1, min(max(done, 0.0), 1.0), final

    def pending(self, now):
        """Text like 'up to max in 40 s', or '' when nothing is pending."""
        info = self.pending_info(now)
        if not info:
            return ""
        left = max(info[1] - now, 0)
        when = f"{left:.0f} s" if left < 90 else f"{left / 60:.1f} min"
        return f"{'up' if self.way > 0 else 'down'} to {info[0]} in {when}"


PUMP_MIN = 20          # the Kraken doesn't accept a lower pump duty


def fan_targets(liquid, cpu_smooth, mode):
    """Fans and pump: the highest of their liquid curve, the CPU temp boost and the
    mode minimum. Returns ({channel: duty}, boost)."""
    temp_boost = curve_duty(BOOST, cpu_smooth)
    boost = max(temp_boost, mode["fan_min"])
    radiator = max(curve_duty(FAN, liquid), boost)
    case = max(curve_duty(CASE, liquid), boost)
    pump = max(curve_duty(PUMP, liquid), temp_boost, mode["pump_min"], PUMP_MIN)
    return {"kraken": radiator, "fan1": radiator, "fan2": case, "fan3": case,
            "pump": min(pump, 100)}, boost


def ramp(now, target):
    """Limit how fast a fan changes: quick up, slow down."""
    if now is None:
        return target
    if target > now:
        return min(target, now + RAMP_UP)
    return max(target, now - RAMP_DOWN)


def scale(rgb, percent):
    return tuple(round(c * percent / 100) for c in rgb)


# ---- devices ------------------------------------------------------------------------
def find(name):
    return next((d for d in find_liquidctl_devices() if name in d.description), None)


def connect():
    """Kraken: initialize and set the fan/pump curves."""
    dev = find("Kraken Z")
    if dev is None:
        raise RuntimeError("Kraken not found")
    dev.connect()
    retry(dev.initialize)
    retry(dev.set_speed_profile, "fan", FAN)
    retry(dev.set_speed_profile, "pump", PUMP)
    log("kraken connected, curves set")
    return dev


def connect_hub():
    hub = find("Smart Device V2")
    if hub is None:
        log("smart device not found, hub control skipped")
        return None
    hub.connect()
    retry(hub.initialize)
    log("smart device connected")
    return hub


def update_hub(hub, duties, sent, rgb):
    """Set hub fan duties and LED color; only changes are sent. Returns hub status."""
    for ch in ("fan1", "fan2", "fan3"):
        if sent.get(ch) != duties[ch]:
            retry(hub.set_fixed_speed, ch, duties[ch])
            sent[ch] = duties[ch]
    if sent.get("lights", UNSET) != rgb:
        if rgb is None:
            retry(hub.set_color, "sync", "off", [])
        else:
            retry(hub.set_color, "sync", "fixed", [rgb])
        sent["lights"] = rgb
    return {k: v for k, v, _ in retry(hub.get_status)}


class RamLights:
    """RAM lights through a local OpenRGB server; quietly skipped if unavailable."""

    def __init__(self):
        self.devs, self.last, self.next_try = None, UNSET, 0.0

    def set(self, rgb):
        if self.devs is None:
            if time.monotonic() < self.next_try:
                return
            self.next_try = time.monotonic() + RETRY_S
            try:
                from openrgb import OpenRGBClient
                from openrgb.utils import DeviceType
                client = OpenRGBClient(*OPENRGB, name="kraken-display")
                self.devs = client.get_devices_by_type(DeviceType.DRAM)
                for d in self.devs:
                    d.set_mode("Direct")
                self.last = UNSET
                log(f"openrgb connected, {len(self.devs)} RAM stick(s)")
            except Exception as e:
                log(f"openrgb not available ({e}), retrying in {RETRY_S}s")
                self.devs = None
                return
        if rgb == self.last:
            return
        try:
            from openrgb.utils import RGBColor
            color = RGBColor(*(rgb or (0, 0, 0)))
            for d in self.devs:
                d.set_color(color)
            self.last = rgb
        except Exception as e:
            log(f"openrgb error, will reconnect: {e}")
            self.devs = None

    def state(self):
        return "not connected" if self.devs is None else f"{len(self.devs)} stick(s)"


# ---- output -------------------------------------------------------------------------
GAUGE_START, GAUGE_SWEEP = 135, 270   # degrees; 0 = 3 o'clock, clockwise: 7:30 -> 4:30
GAUGE_R, SS = 149, 3                  # gauge radius (px), supersampling for smooth curves


def mode_pos(i):
    """Position (0-100 %) of mode i on the gauge: first mode at the start, last at the end."""
    return 100.0 * i / max(len(MODES) - 1, 1)


def load_pos(load):
    """Load % -> position % on the gauge, so each mode's load range lies around its dot
    (mode boundaries sit halfway between two dots)."""
    n = len(MODES)
    xs = [0.0] + [MODES[i]["load_to"] for i in range(n - 1)] + [100.0]
    ys = [0.0] + [(mode_pos(i) + mode_pos(i + 1)) / 2 for i in range(n - 1)] + [100.0]
    load = min(max(load, 0.0), 100.0)
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if load <= x1:
            return y0 + (y1 - y0) * ((load - x0) / (x1 - x0) if x1 > x0 else 1)
    return 100.0


def gauge_color(pct):
    """Color at pct (0-100): the mode colors spread evenly, blended in between.
    Used for the gauge and the memory bars."""
    stops = [(mode_pos(i), m["color"]) for i, m in enumerate(MODES)]
    if pct <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if pct <= p1:
            t = (pct - p0) / (p1 - p0) if p1 > p0 else 1
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return stops[-1][1]


def draw_gauge(img, mode_i, quiet, load=None, dot=None, ghost=None):
    """Gradient arc (white -> dark red by default). The bright part follows the live CPU
    load; the dot marks the current mode (first mode at the start, bottom left; last at
    the end, bottom right, same height). While a mode change is pending, the dot glides
    toward the next mode as the wait runs out (dot = its position, %), and a small ring
    marks where it's heading (ghost, %). Drawn 3x larger, scaled down for smooth edges."""
    size, c = 320 * SS, 160 * SS
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    color = MODES[mode_i]["color"]
    if dot is None:
        dot = 0.0 if quiet else mode_pos(mode_i)                 # % along the gauge
    pos = dot if load is None else load_pos(load)                # how far it's lit
    r, width = GAUGE_R * SS, 6 * SS
    box = (c - r, c - r, c + r, c + r)
    for k in range(GAUGE_SWEEP):
        pct = (k + 0.5) / GAUGE_SWEEP * 100
        col = gauge_color(pct)
        if pct > pos:
            col = scale(col, 30)                                 # not reached yet: dim
        d.arc(box, GAUGE_START + k, GAUGE_START + k + 1.5, fill=col + (255,), width=width)
    for pct in (0, 100):                                         # rounded ends
        ang = math.radians(GAUGE_START + pct / 100 * GAUGE_SWEEP)
        x, y = c + (r - width / 2) * math.cos(ang), c + (r - width / 2) * math.sin(ang)
        end = gauge_color(pct) if pct <= pos else scale(gauge_color(pct), 30)
        d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=end + (255,))
    def spot(pct):
        ang = math.radians(GAUGE_START + pct / 100 * GAUGE_SWEEP)
        return c + (r - width / 2) * math.cos(ang), c + (r - width / 2) * math.sin(ang)

    if ghost is not None:                                        # where it's heading
        x, y = spot(ghost)
        g = 7 * SS
        d.ellipse((x - g, y - g, x + g, y + g), outline=(255, 255, 255, 200), width=2 * SS)
    x, y = spot(dot)                                             # the dot
    outer, inner = 10 * SS, 7 * SS
    fill = gauge_color(dot) if ghost is not None else color
    d.ellipse((x - outer, y - outer, x + outer, y + outer), fill=(255, 255, 255, 255))
    d.ellipse((x - inner, y - inner, x + inner, y + inner),
              fill=scale(fill, 55 if quiet else 100) + (255,))
    layer = layer.resize((320, 320), Image.LANCZOS)
    img.paste(layer, (0, 0), layer)


def render(cpu, liquid, watts=None, load=None, text="", gpu=None, mode_i=None, quiet=False,
           dot=None, ghost=None):
    used, total = ram_gb()
    img = Image.new("RGB", (320, 320), "black")
    if mode_i is not None:
        draw_gauge(img, mode_i, quiet, load, dot, ghost)
    d = ImageDraw.Draw(img)

    # top row: power, CPU load, GPU load
    gpu_load = gpu.get("load") if gpu else None
    columns = ((90, "--" if watts is None else f"{watts:.0f} W", "POWER"),
               (160, "--" if load is None else f"{load:.0f}%", "CPU"),
               (230, "--" if gpu_load is None else f"{gpu_load:.0f}%", "GPU"))
    value_font, label_font = ImageFont.truetype(FONT, 19), ImageFont.truetype(FONT_REG, 13)
    for x, value, name in columns:
        d.text((x, 72), value, font=value_font, fill="#ffc857", anchor="mm")
        d.text((x, 92), name, font=label_font, fill="#888888", anchor="mm")

    big, small = ImageFont.truetype(FONT, 46), ImageFont.truetype(FONT, 18)
    d.text((92, 135), f"{cpu:.0f}°", font=big, fill="white", anchor="mm")
    d.text((92, 176), "CPU", font=small, fill="#7ab8ff", anchor="mm")
    d.text((228, 135), f"{liquid:.0f}°", font=big, fill="white", anchor="mm")
    d.text((228, 176), "LIQUID", font=small, fill="#7affc0", anchor="mm")

    # memory: RAM on the left, graphics memory on the right, one bar each
    ram_text = f"RAM {used:.0f}/{total:.0f} GB"
    if gpu and gpu.get("total"):
        g_used = gpu["used"]
        gpu_text = f"GPU {g_used:.1f}/{gpu['total']:.0f} GB" if g_used < 10 else \
                   f"GPU {g_used:.0f}/{gpu['total']:.0f} GB"
        gpu_frac, gpu_color = g_used / gpu["total"], "white"
    else:
        gpu_text = "iGPU shared" if gpu and gpu["kind"] == "igpu" else "no GPU"
        gpu_frac, gpu_color = None, "#888888"
    for size in (16, 15, 14, 13, 12):                 # shrink until both fit side by side
        mem_font = ImageFont.truetype(FONT, size)
        if d.textlength(ram_text, font=mem_font) + d.textlength(gpu_text, font=mem_font) <= 228:
            break
    d.text((40, 204), ram_text, font=mem_font, fill="white", anchor="lm")
    d.text((280, 204), gpu_text, font=mem_font, fill=gpu_color, anchor="rm")

    def bar(y, frac, x0=60, x1=260, h=10):
        """Dark track, filled with the white -> red gradient up to frac; no border."""
        length = x1 - x0
        d.rounded_rectangle((x0, y, x1 - 1, y + h - 1), radius=h // 2, fill="#2a2a2a")
        if frac is None or frac <= 0:
            return
        w = max(int(length * min(frac, 1.0)), h)
        grad = Image.new("RGB", (w, h))
        gd = ImageDraw.Draw(grad)
        for x in range(w):
            gd.line((x, 0, x, h), fill=gauge_color(100.0 * x / (length - 1)))
        mask = Image.new("L", (w * 4, h * 4), 0)                 # smooth rounded ends
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius=h * 2, fill=255)
        img.paste(grad, (x0, y), mask.resize((w, h), Image.LANCZOS))

    bar(219, used / total if total else 0.0)
    bar(235, gpu_frac)
    if text:
        d.text((160, 272), text, font=ImageFont.truetype(FONT, 15), fill="#cccccc", anchor="mm")
    img.rotate(ROTATE).save(IMG)


def in_time(left):
    """45 -> '45 s', 170 -> '3 min'"""
    left = max(left, 0)
    return f"{left:.0f} s" if left < 60 else f"{math.ceil(left / 60)} min"


def bottom_texts(st, now):
    """The texts the bottom line cycles through, in the order of screen.texts."""
    out = []
    for item in TEXTS:
        if item == "fans":
            out.append(f"fans {st['duties'].get('kraken', 0)}%")
        elif item == "pump":
            out.append(f"pump {st['duties'].get('pump', 0):.0f}%")
        elif item == "time":
            out.append(time.strftime("%H:%M"))
        elif item == "next":
            out.extend(f"{what} in {in_time(when - now)}" for what, when in st["next"])
    return out


def write_status(s):
    used, total = ram_gb()
    k, h = s["k"], s["h"]
    lights = "off" if s["rgb"] is None else "#%02x%02x%02x" % s["rgb"]
    power = "--" if s["watts"] is None else f"{s['watts']:.0f}"
    rpm = {"kraken": k.get("Fan speed", 0)}
    for i in (1, 2, 3):
        rpm[f"fan{i}"] = h.get(f"Fan {i} speed", 0)
    lines = [
        time.strftime("%H:%M:%S") + f"   mode: {s['mode_text']}",
        f"CPU {s['cpu']:.0f} C (smoothed {s['cpu_smooth']:.0f})   "
        f"load {s['load']:.0f}% (smoothed {s['load_smooth']:.0f}%)   CPU power {power} W",
        f"liquid {s['liquid']:.1f} C   pump {s['duties'].get('pump', 0):.0f}% "
        f"({k.get('Pump speed', 0):.0f} rpm)   RAM {used:.1f}/{total:.0f} GB   "
        f"fan boost {s['boost']}%",
    ]
    gpu = s.get("gpu")
    gpu_load = "--" if not gpu or gpu.get("load") is None else f"{gpu['load']:.0f}%"
    if gpu and gpu.get("total"):
        lines.append(f"GPU ({gpu['kind']}) load {gpu_load}   memory "
                     f"{gpu['used']:.1f}/{gpu['total']:.1f} GB")
    elif gpu:
        lines.append(f"GPU (Intel built-in) load {gpu_load}   memory: shared with main RAM")
    else:
        lines.append("GPU: none found")
    for ch in CHANNELS:
        speed = f"{rpm[ch]:5.0f} rpm" if (ch == "kraken" or h) else "    ? rpm"
        lines.append(f"  {NAMES[ch]:<38} {s['duties'].get(ch, 0):3.0f}%  {speed}")
    lines.append(f"lights {lights} at {s['bright']:.0f}%   screen {s['screen']}%   "
                 f"RAM lights: {s['ram'].state()}")
    tmp = STATUS + ".new"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, STATUS)        # readers never see a half-written file


# ---- main loop ------------------------------------------------------------------------
def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # run cleanup on systemctl stop
    os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
    if os.path.islink(RUN_DIR) or os.stat(RUN_DIR).st_uid != os.geteuid():
        raise SystemExit(f"{RUN_DIR} must be a real directory owned by this user")
    dev = connect()
    hub, sent, hub_next = None, {}, 0.0
    ram = RamLights()
    duties, kraken_sent, pump_sent, screen_sent = {}, None, None, None
    cpu_smooth = load_smooth = None
    idle_since, busy_since, quiet, first = None, None, False, True
    modes = ModeTimer()
    st, text_i = None, 0
    black_sent = False
    cpu_load()            # start the load and power counters, then measure 5 s
    cpu_power()
    time.sleep(INTERVAL)  # so the very first reading already reflects the real load
    next_control = next_screen = time.monotonic()
    try:
        while True:
            try:
                now = time.monotonic()
                # ================= sensors, mode, fans, lights (every INTERVAL) ===========
                if now >= next_control:
                    next_control = now + INTERVAL
                    k = {key: v for key, v, _ in retry(dev.get_status)}
                    liquid = k["Liquid temperature"]
                    cpu, load, watts = cpu_temp(), cpu_load(), cpu_power()
                    has_cpu = cpu is not None
                    cpu = cpu if has_cpu else 0.0     # shown as 0; boost stays off
                    cpu_smooth = cpu if cpu_smooth is None else (
                        cpu_smooth + (cpu - cpu_smooth) * CPU_SMOOTHING)
                    load_smooth = load if load_smooth is None else (
                        load_smooth + (load - load_smooth) * LOAD_SMOOTHING)
                    mode_i = modes.update(load_smooth, load, now)
                    mode = MODES[mode_i]

                    # --- quiet mode bookkeeping --------------------------------------
                    if load < QUIET_LOAD:
                        busy_since = None
                        if idle_since is None:
                            # idle at start-up: count the wait as already done
                            idle_since = now - QUIET_AFTER_MIN * 60 if (first and QUIET_AT_START) else now
                    elif not quiet:
                        idle_since = None         # load while not quiet: restart the idle timer
                    else:
                        if busy_since is None:
                            busy_since = now
                        if now - busy_since >= QUIET_WAKE_MIN * 60:
                            idle_since = busy_since = None   # load lasted long enough: wake up
                    # without a CPU sensor the "wake when hot" check can't work: no quiet
                    cool = has_cpu and liquid < QUIET_MAX_LIQUID and cpu < QUIET_MAX_CPU
                    if quiet and not cool:
                        idle_since = busy_since = None   # got warm while quiet: wake now
                    idle_s = 0 if idle_since is None else now - idle_since
                    # quiet only from a low mode; after a step-down from a high mode, once the
                    # steps are done and the last mode has settled for after_min
                    from_high = modes.stepping_from()
                    settled = now - modes.changed_at >= QUIET_AFTER_MIN * 60
                    mode_ok = quiet or (mode_i <= QUIET_UP_TO and settled
                                        and (from_high is None or from_high <= QUIET_UP_TO))
                    quiet = (idle_since is not None and idle_s >= QUIET_AFTER_MIN * 60
                             and cool and mode_ok)
                    first = False

                    # --- fans ------------------------------------------------------------
                    if quiet:
                        targets = {ch: QUIET_DUTY for ch in CHANNELS}
                        targets["pump"], boost = max(QUIET_PUMP, PUMP_MIN), 0
                    else:
                        targets, boost = fan_targets(liquid, cpu_smooth, mode)
                    duties = {ch: ramp(duties.get(ch), targets[ch]) for ch in (*CHANNELS, "pump")}
                    if duties["kraken"] != kraken_sent:
                        retry(dev.set_fixed_speed, "fan", duties["kraken"])
                        kraken_sent = duties["kraken"]
                    if duties["pump"] != pump_sent:
                        retry(dev.set_fixed_speed, "pump", duties["pump"])
                        pump_sent = duties["pump"]

                    # --- what's coming next (for the screen and the status file) ----------
                    upcoming = []                     # (text, monotonic time it happens)
                    nxt = modes.pending_info(now)
                    if quiet and busy_since is not None:
                        upcoming.append(("waking", busy_since + QUIET_WAKE_MIN * 60))
                    elif not quiet and idle_since is not None:
                        when = max(idle_since, modes.changed_at) + QUIET_AFTER_MIN * 60
                        from_high = modes.stepping_from()
                        if nxt and from_high is not None and from_high > QUIET_UP_TO:
                            if modes.final() <= QUIET_UP_TO:      # after the last step
                                upcoming.append(("quiet", nxt[1] + QUIET_AFTER_MIN * 60))
                        elif mode_i <= QUIET_UP_TO:
                            upcoming.append(("quiet", when))
                    if nxt:
                        upcoming.append(nxt)
                    mode_text = f"quiet ({mode['name']})" if quiet else mode["name"]
                    if upcoming:
                        mode_text += "   (" + ", ".join(
                            f"{what} in {in_time(when - now)}" for what, when in upcoming) + ")"

                    # --- lights and screen brightness -----------------------------------------
                    was_dark = st is not None and st["dark"]
                    dark = mode_i < DARK_FROM
                    if was_dark and not dark:
                        next_screen = now                         # redraw right away
                    if dark:
                        mode_text += "   [dark]"
                    bright = QUIET_LIGHTS if quiet else mode["lights"]
                    rgb = scale(mode["color"], bright) if LIGHTS == "auto" and not dark else None
                    if rgb is not None and not any(rgb):
                        rgb = None                                # 0% brightness = off
                    screen = 0 if dark else int(
                        min(max(SCREEN_QUIET if quiet else mode["screen"], 0), 100))

                    h = {}
                    if hub is None and now >= hub_next:
                        try:
                            hub = connect_hub()
                        except Exception as e:
                            log(f"hub connect failed: {e}")
                        hub_next = now + RETRY_S
                    if hub is not None:
                        try:
                            h = update_hub(hub, duties, sent, rgb)
                        except Exception as e:
                            log(f"hub error, will reconnect: {e}")
                            try:
                                hub.disconnect()
                            except Exception:
                                pass
                            hub, sent = None, {}

                    ram.set(rgb)
                    if screen != screen_sent:
                        retry(dev.set_screen, "lcd", "brightness", str(screen))
                        screen_sent = screen
                    gpu = gpu_info()
                    st = dict(k=k, h=h, cpu=cpu, cpu_smooth=cpu_smooth, load=load,
                              load_smooth=load_smooth, watts=watts, liquid=liquid,
                              boost=boost, duties=duties, mode_text=mode_text, mode_i=mode_i,
                              quiet=quiet, next=upcoming, rgb=rgb, bright=bright,
                              screen=screen, ram=ram, gpu=gpu, dark=dark)
                    write_status(st)

                # ================= pump screen (every TEXT_SECONDS) ======================
                if st is not None and st["dark"]:
                    if not black_sent:
                        Image.new("RGB", (320, 320), "black").save(IMG)
                        retry(dev.set_screen, "lcd", "static", IMG)
                        black_sent = True
                elif st is not None and now >= next_screen:
                    next_screen = now + TEXT_SECONDS
                    black_sent = False
                    texts = bottom_texts(st, now)
                    text = texts[text_i % len(texts)] if texts else ""
                    text_i += 1
                    dot = ghost = None
                    heading = modes.progress(now)
                    if heading:                       # glide toward the next mode
                        to_i, done, final_i = heading
                        start = 0.0 if st["quiet"] else mode_pos(modes.current)
                        dot = start + (mode_pos(to_i) - start) * done
                        ghost = mode_pos(final_i)
                    render(st["cpu"], st["liquid"], st["watts"], st["load_smooth"], text,
                           st["gpu"], st["mode_i"], st["quiet"], dot, ghost)
                    retry(dev.set_screen, "lcd", "static", IMG)
            except Exception as e:
                log(f"error, reconnecting: {e}")
                try:
                    dev.disconnect()
                except Exception:
                    pass
                time.sleep(5)
                dev = connect()        # also restores the Kraken's own liquid curves
                kraken_sent = pump_sent = screen_sent = None   # resend everything
                black_sent = False
            time.sleep(max(0.1, min(next_control, next_screen) - time.monotonic()))
    finally:
        # Leave the Kraken running its own liquid curves when the service stops, so the
        # radiator fans and the pump never stay stuck at a fixed speed without the script.
        try:
            retry(dev.set_speed_profile, "fan", FAN, tries=2)
            retry(dev.set_speed_profile, "pump", PUMP, tries=2)
            log("stopped: Kraken fans and pump back on their own liquid curves")
        except Exception:
            pass
        # The Smart Device keeps its last fixed duty, which may be a quiet 10 %.
        if hub is not None:
            try:
                for ch in ("fan1", "fan2", "fan3"):
                    retry(hub.set_fixed_speed, ch, HUB_EXIT_DUTY, tries=2)
                log(f"stopped: hub fans set to {HUB_EXIT_DUTY}%")
            except Exception:
                pass


load_config(quiet=True)   # settings are needed on import too, e.g. by tests and tools

USAGE = """usage: kraken_display.py [--config PATH] [--print-config]

  --config PATH    settings file (default: $KRAKEN_DISPLAY_CONFIG or /etc/kraken-display.json)
  --print-config   print the settings in use (built-in defaults + the file) and exit
"""


def parse_args(argv):
    """Returns (config path, print_config)."""
    path, show = CONFIG, False
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            print(USAGE, end="")
            sys.exit(0)
        elif arg == "--print-config":
            show = True
        elif arg == "--config" and args:
            path = args.pop(0)
        elif arg.startswith("--config="):
            path = arg.split("=", 1)[1]
        else:
            sys.exit(f"unknown argument '{arg}'\n{USAGE}")
    return path, show


if __name__ == "__main__":
    path, show = parse_args(sys.argv[1:])
    if show:
        print(dump_config(load_config(path, quiet=True)), end="")
    else:
        load_config(path)     # again, this time logging any problems with the file
        main()
