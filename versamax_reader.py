#!/usr/bin/env python3
"""
versamax_reader.py - Control a Molecular Devices VERSAmax Tunable Microplate Reader from a Mac.

A single-file application. Everything the reader can do is
available from a GUI: drawer, incubator, shaking, endpoint / kinetic / spectrum reads,
wavelengths, column (strip) selection, AutoMix, calibration and speed read. Data is
collected automatically, shown as a plate heat map and graphs, and saved as CSV files.
A terminal tab is included for sending commands directly to the reader.

Requirements: Python 3.8+ with Tkinter, and pyserial  (pip install pyserial)

Usage:
    python3 versamax_reader.py              # open the GUI
    python3 versamax_reader.py --simulate   # practice / test without the reader attached

Serial protocol (verified against reader firmware 3.13, 31Oct98):
    9600 baud, 8 data bits, no parity, 1 stop bit, no flow control.
    Commands are sent as "! <command>\\n". The "!" puts the reader in machine mode (no echo).
    Replies:  "OK\\t\\r\\n>"  [+ "\\r\\n<body>\\r\\n>"]      or      "FAIL\\t <code>\\t\\r\\n>"
"""

# =================================================================================================
# UNOFFICIAL, NON-COMMERCIAL PROJECT
#
#   This is an independent, community-made tool shared free of charge for research and educational
#   use. It is NOT a Molecular Devices product and is not affiliated with, endorsed by or supported
#   by Molecular Devices. VERSAmax is a trademark of its respective owner and is used here only to
#   identify the compatible instrument.
#
#   It contains no Molecular Devices software or code: it talks to the reader only through the
#   reader's standard RS-232 serial port, using the plain-text commands the instrument documents
#   through its own built-in "help" command.
#
#   Provided as is, without warranty of any kind, under the MIT License (see the LICENSE file).
#   Not validated for regulated (GLP/GMP or clinical) use.
# =================================================================================================

# =================================================================================================
# HOW THIS FILE IS ORGANISED
#
#   1. Constants ............... plate geometry, instrument limits, the reader's error table
#   2. Data model & parsing .... Dataset (one transfer), ReadSettings (what to measure, validation,
#                                command generation), RunData (one run's results + CSV files)
#   3. Serial driver ........... VersaMax: sends "! command" lines and decodes the replies
#   4. Simulator ............... SimulatedReader: a stand-in instrument for --simulate and testing
#   5. Worker thread ........... the only code that talks to the serial port
#   6. GUI ..................... App: Tkinter window (controls, plate view, graph, terminal, log)
#
# THREADING MODEL (read this before changing anything):
#   * Tkinter is not thread-safe, so widgets are only ever touched from the main (GUI) thread.
#   * All serial I/O runs on the single Worker thread, one job at a time, so commands from buttons,
#     background polling, runs and the terminal can never interleave on the wire.
#   * Worker jobs report back by putting tuples on App.events; App._process_events() drains that
#     queue every 100 ms on the GUI thread and App._handle() updates the screen.
# =================================================================================================
import argparse
import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # the simulator still works without pyserial
    serial = None

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------
APP_NAME = "VersaMax Control"
# Pseudo port shown in the port list; choosing it uses SimulatedReader instead of hardware.
SIM_PORT = "SIMULATOR (no reader)"
# Serial speed: the reader's factory setting.
BAUD = 9600
# 96-well plate geometry: rows A-H x columns 1-12. WELLS = A1, A2 ... H12 in row order.
ROWS = "ABCDEFGH"
COLS = range(1, 13)
WELLS = [f"{r}{c}" for r in ROWS for c in COLS]
# Instrument limits as reported by '! option': monochromator range (nm), incubator range (°C).
WL_MIN, WL_MAX = 340, 850
TEMP_MIN, TEMP_MAX = 15.0, 45.0
# Each run is saved in its own time-stamped subfolder here (changeable via 'Save to: Change…').
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "VersaMax Data")

# Error table as reported by the reader itself ("! error list", firmware 3.13).
# NOTE: these codes differ from the table printed in the operator's manual; the firmware wins.
ERROR_TEXT = {
    100: "command not found", 101: "invalid argument", 102: "invalid data",
    103: "too many arguments", 104: "not enough arguments", 105: "input line too long",
    106: "system busy", 107: "measurement in progress", 108: "no data available",
    109: "data buffer full", 110: "error buffer overflow", 111: "stray light cuvette, door open?",
    200: "attempt to free invalid addr", 201: "NVRAM structure error", 202: "write of NVRAM failed",
    203: "read of NVRAM failed", 204: "invalid NVRAM address", 205: "user generated bus error",
    206: "invalid baud rate", 207: "bad error number", 208: "unexpected interrupt",
    209: "receive queue overflow", 210: "serial port parity error", 211: "serial port overrun error",
    212: "illegal state", 213: "cmd generated too much output", 214: "fatal trap",
    215: "returned from MAIN", 216: "memory allocation failure", 217: "power up diag failed",
    218: "gain calibration failed", 219: "cuvette gain check fail", 220: "reference gain check fail",
    221: "DMA timeout", 222: "DMA busy", 223: "IPC drawer error", 224: "IPC filter error",
    300: "temp sensor faulty", 301: "safe temperature limit exceeded", 302: "low light measurement",
    303: "unable to cal dark current", 304: "mono photo switch failure",
    305: "carriage motion time-out", 306: "monochromator motion time-out",
    307: "filter wheel motion time-out", 308: "plate air calibration failed",
    309: "measurement aborted", 310: "signal level saturation",
    311: "plate air cal fail, low light", 312: "can't find zero order",
    313: "wavelength out of cal", 314: "cuvette air calibration fail",
    315: "can't find home on filter wheel", 316: "stray light plate", 317: "LCD time-out",
    400: "NVRAM pathlength checksum corrupt", 401: "NVRAM air cal checksum corrupt",
    402: "mono cal NVRAM data error", 403: "cuvette NVRAM air cal data error",
    404: "plate NVRAM air cal data error", 999: "miscellaneous runtime error",
}


# Driver exceptions. The Worker catches both and the GUI shows them to the operator.
class ReaderError(Exception):
    """The reader answered FAIL <code>."""

    def __init__(self, code, command):
        self.code, self.command = code, command
        super().__init__(f"Reader rejected '{command}': error {code} ({ERROR_TEXT.get(code, 'unknown error')})")


# Raised when a reply does not complete in time (reader off, cable unplugged, wrong port).
class ReaderTimeout(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Data model and parsing
# ---------------------------------------------------------------------------------------------

# Convert one OD value from the reader's text ('0.1232', '.0978') to float; None if unreadable.
def parse_od(token):
    try:
        return float(token)
    except ValueError:
        return None  # e.g. an over-range marker; the raw text is kept in the dataset


@dataclass
class Dataset:
    """One block returned by '! transfer'."""
    reader_time: float               # reader clock, seconds since power-on
    temperature: float               # chamber temperature at read time
    kind: str                        # 'plate'
    wavelengths: list                # all wavelengths in the set
    blocks: dict                     # {wavelength: {column: [8 OD values, rows A-H]}}
    # raw: the exact text received, kept so the data can always be re-parsed or audited.
    # checksum: trailing number the reader appends to each transfer (undocumented; stored, not checked).
    raw: str
    checksum: str = ""


# Parse the body of a '! transfer' reply into a Dataset. Example (endpoint, 2 wavelengths, cols 2-4):
#
#     687.7    22.4   plate            <- reader clock (s), chamber temperature (°C), read type
#     L:   450   620                   <- the full wavelength set
#     L:   450                         <- start of the 450 nm block
#      2:    0.1000    .0978 ...       <- column 2: eight OD values for rows A..H (tab separated)
#      3:    ...
#     L:   620                         <- start of the 620 nm block ...
#     134                              <- trailing checksum
#
# Kinetic and spectrum reads arrive as one such dataset per time point / per wavelength.
def parse_dataset(body):
    lines = body.split("\n")
    header = None
    wavelengths, blocks, current, checksum = [], {}, None, ""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        # the first non-blank line is the header
        if header is None:
            header = [p.strip() for p in line.split("\t")]
            continue
        # the first 'L:' line lists every wavelength; each later 'L:' line opens a new block
        if s.startswith("L:"):
            nums = [int(x) for x in re.findall(r"\d+", s[2:])]
            if not wavelengths:
                wavelengths = nums
            else:
                current = nums[0]
                blocks[current] = {}
            continue
        # column line '<n>:' + 8 values; padded/trimmed to exactly 8 (rows A-H)
        m = re.match(r"^(\d+):\s*(.*)$", s)
        if m and current is not None:
            vals = [parse_od(v) for v in m.group(2).split("\t") if v.strip()]
            blocks[current][int(m.group(1))] = (vals + [None] * 8)[:8]
            continue
        # a bare number at the end is the checksum
        if re.fullmatch(r"\d+", s):
            checksum = s
    # refuse to guess: an unexpected format raises an error instead of saving wrong numbers
    if not header or len(header) < 2 or not blocks:
        raise ValueError("Unrecognised transfer data:\n" + body)
    return Dataset(float(header[0]), float(header[1]), header[2] if len(header) > 2 else "",
                   wavelengths, blocks, body, checksum)


# Everything the operator chooses under "Read setup". One instance fully describes a run; it is saved
# as settings.json in every results folder, and as a protocol file (.json) when the operator asks.
@dataclass
class ReadSettings:
    name: str = "experiment"
    mode: str = "endpoint"                   # endpoint | kinetic | spectrum
    wavelengths: list = field(default_factory=lambda: [405])
    # columns to read (1-12, contiguous); sent as 'strip <first> <count>'
    first_col: int = 1
    last_col: int = 12
    kin_interval: float = 30.0               # seconds between kinetic reads
    kin_reads: int = 10
    # spectrum scan in nm (the reader itself takes start, step and NUMBER of wavelengths)
    spec_start: int = 400
    spec_stop: int = 700
    spec_step: int = 10
    automix: str = "off"                     # off | once | on  (on = before first read and between reads)
    # AutoMix durations in seconds; 'between' only applies to kinetic reads
    mix_before: float = 5.0
    mix_between: float = 3.0
    calibrate: str = "once"                  # once | on | off
    # speed read: use the stored air calibration instead of calibrating each read
    speed: bool = False

    @property
    def n_cols(self):
        return self.last_col - self.first_col + 1

    @property
    # number of wavelengths in a spectrum scan (stop is rounded down onto the step grid)
    def spec_count(self):
        return (self.spec_stop - self.spec_start) // self.spec_step + 1

    def min_kinetic_interval(self):
        """Rough minimum interval (s). Manual: 9 s full plate / 2 s one column per wavelength."""
        return len(self.wavelengths) * max(2.0, 0.75 * self.n_cols)

    # How many '! transfer' datasets a complete run produces (drives progress and completion).
    def expected_datasets(self):
        return {"endpoint": 1, "kinetic": self.kin_reads, "spectrum": self.spec_count}[self.mode]

    # Check settings before anything is sent. Returns (errors, warnings): errors block the run,
    # warnings are shown to the operator, who can continue anyway.
    def validate(self):
        errors, warnings = [], []
        if self.mode not in ("endpoint", "kinetic", "spectrum"):
            errors.append("Choose a read mode.")
        if not (1 <= self.first_col <= self.last_col <= 12):
            errors.append("Columns must satisfy 1 <= first <= last <= 12.")
        if self.mode in ("endpoint", "kinetic"):
            if not 1 <= len(self.wavelengths) <= 8:
                errors.append("Enter between 1 and 8 wavelengths.")
            if len(set(self.wavelengths)) != len(self.wavelengths):
                errors.append("Wavelengths must be different from each other.")
            for w in self.wavelengths:
                if not WL_MIN <= w <= WL_MAX:
                    errors.append(f"Wavelength {w} nm is outside {WL_MIN}-{WL_MAX} nm.")
        if self.mode == "kinetic":
            if self.kin_interval < 1:
                errors.append("Kinetic interval must be at least 1 s.")
            if not 2 <= self.kin_reads <= 9999:
                errors.append("Number of kinetic reads must be 2-9999.")
            if self.kin_interval < self.min_kinetic_interval():
                warnings.append(
                    f"The interval ({self.kin_interval:g} s) is probably shorter than this reader needs "
                    f"for {self.n_cols} column(s) x {len(self.wavelengths)} wavelength(s) "
                    f"(about {self.min_kinetic_interval():.0f} s). The reader will stretch it; the "
                    f"actual read times are recorded in the data.")
        if self.mode == "spectrum":
            if not (WL_MIN <= self.spec_start < self.spec_stop <= WL_MAX):
                errors.append(f"Spectrum start/stop must be within {WL_MIN}-{WL_MAX} nm and start < stop.")
            if self.spec_step < 1:
                errors.append("Spectrum step must be at least 1 nm.")
            elif self.spec_count < 2:
                errors.append("Spectrum must contain at least 2 wavelengths.")
        if self.automix in ("once", "on") and not 1 <= self.mix_before <= 999:
            errors.append("AutoMix 'before' duration must be 1-999 s.")
        if self.automix == "on" and self.mode == "kinetic" and not 1 <= self.mix_between <= 999:
            errors.append("AutoMix 'between reads' duration must be 1-999 s.")
        return errors, warnings

    # Translate the settings into reader commands, in the order they are sent. Example (kinetic):
    #   mode kinetic 30 10 / wavelength 450 620 / strip 1 12 / shake 5 3 0 0 0 / shake on /
    #   calibrate once / speed off
    def commands(self):
        if self.mode == "endpoint":
            mode = "mode endpoint"
        elif self.mode == "kinetic":
            mode = f"mode kinetic {self.kin_interval:g} {self.kin_reads}"
        else:
            mode = f"mode spectrum {self.spec_start} {self.spec_step} {self.spec_count}"
        cmds = [mode]
        if self.mode != "spectrum":
            cmds.append("wavelength " + " ".join(str(w) for w in self.wavelengths))
        # note: 'strip' takes a start column and a COUNT of columns, not an end column
        cmds.append(f"strip {self.first_col} {self.n_cols}")
        # 'shake <before> <between> 0 0 0' sets the durations; 'shake on|once|off' chooses when they apply.
        # The last three numbers are further firmware parameters that this program leaves at 0.
        between = self.mix_between if (self.automix == "on" and self.mode == "kinetic") else 0
        cmds.append(f"shake {self.mix_before:g} {between:g} 0 0 0")
        cmds.append(f"shake {self.automix}")
        cmds.append(f"calibrate {self.calibrate}")
        cmds.append("speed on" if self.speed else "speed off")
        return cmds

    # One-line human-readable summary used in the log, the Plate tab and status messages.
    def description(self):
        cols = f"columns {self.first_col}-{self.last_col}"
        if self.mode == "spectrum":
            return f"Spectrum {self.spec_start}-{self.spec_stop} nm step {self.spec_step} ({self.spec_count} wl), {cols}"
        wl = ", ".join(f"{w}" for w in self.wavelengths) + " nm"
        if self.mode == "kinetic":
            return f"Kinetic {self.kin_reads} reads every {self.kin_interval:g} s at {wl}, {cols}"
        return f"Endpoint at {wl}, {cols}"


# One run's results plus its folder on disk. Files are rewritten after every dataset arrives, so an
# interrupted run still leaves usable data behind:
#   settings.json       the ReadSettings used, plus an "instrument" section (model, firmware, port)
#   raw_transfer.txt    every reply exactly as received (the source of truth; used by "Open previous run")
#   data_long.csv       one row per well x read x wavelength (best for R / Python / Prism)
#   plate_layout.csv    8 x 12 grids laid out like the plate (easy to read in Excel)
#   kinetic_<wl>nm.csv  kinetic only: one row per time point, one column per well
#   spectrum.csv        spectrum only: one row per wavelength, one column per well
# Every file records which reader and firmware produced the data: the CSVs end with Instrument and
# Firmware columns, plate_layout.csv and raw_transfer.txt carry it in their header lines.
class RunData:
    """All datasets of one run, plus automatic saving to a folder."""

    def __init__(self, settings, folder, instrument=None):
        self.settings = settings
        self.folder = folder
        self.datasets = []
        # {"model": ..., "firmware": ..., "port": ...} as reported by the reader on connect
        self.instrument = dict(instrument or {})
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "settings.json"), "w") as f:
            json.dump({**asdict(settings), "instrument": self.instrument}, f, indent=2)

    # e.g. "VERSAmax firmware 3.13 31Oct98" (empty for runs saved before this was recorded)
    @property
    def instrument_text(self):
        model, fw = self.instrument.get("model", ""), self.instrument.get("firmware", "")
        return f"{model} firmware {fw}".strip() if (model or fw) else ""

    def add(self, ds):
        self.datasets.append(ds)
        with open(os.path.join(self.folder, "raw_transfer.txt"), "a") as f:
            f.write(f"### dataset {len(self.datasets)}  received {datetime.now().isoformat(timespec='seconds')}"
                    f"  {self.instrument_text}".rstrip() + "\n")
            f.write(ds.raw.rstrip() + "\n\n")
        self.save()

    def points(self):
        """Flatten to one entry per (dataset, wavelength)."""
        if not self.datasets:
            return []
        # times are relative to the run's first dataset (the reader clock counts seconds since power-on)
        t0 = self.datasets[0].reader_time
        out = []
        for i, ds in enumerate(self.datasets):
            for wl, cols in ds.blocks.items():
                values = {}
                for col, vals in cols.items():
                    for r, v in enumerate(vals):
                        values[f"{ROWS[r]}{col}"] = v
                out.append({"read": i + 1, "time": round(ds.reader_time - t0, 1),
                            "temp": ds.temperature, "wl": wl, "values": values})
        return out

    # Wells that actually contain data (honours the 'Columns' setting), in A1..H12 order.
    def wells_read(self):
        wells = set()
        for p in self.points():
            wells.update(p["values"])
        return [w for w in WELLS if w in wells]

    def save(self):
        pts = self.points()
        wells = self.wells_read()
        # OD written with 4 decimals; blank where there is no value
        fmt = lambda v: "" if v is None else f"{v:.4f}"
        # instrument identity, appended as the last two columns of every CSV row
        inst_hdr = ["Instrument", "Firmware"]
        inst = [self.instrument.get("model", ""), self.instrument.get("firmware", "")]
        with open(os.path.join(self.folder, "data_long.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Read", "Time_s", "Temperature_C", "Wavelength_nm", "Well", "Row", "Column", "OD"] + inst_hdr)
            for p in pts:
                for well in wells:
                    if well in p["values"]:
                        w.writerow([p["read"], p["time"], p["temp"], p["wl"], well, well[0], well[1:],
                                    fmt(p["values"][well])] + inst)
        # mode-specific 'wide' tables
        mode = self.settings.mode
        if mode == "kinetic":
            for wl in sorted({p["wl"] for p in pts}):
                with open(os.path.join(self.folder, f"kinetic_{wl}nm.csv"), "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["Read", "Time_s", "Temperature_C"] + wells + inst_hdr)
                    for p in pts:
                        if p["wl"] == wl:
                            w.writerow([p["read"], p["time"], p["temp"]]
                                       + [fmt(p["values"].get(x)) for x in wells] + inst)
        elif mode == "spectrum":
            with open(os.path.join(self.folder, "spectrum.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Wavelength_nm", "Time_s", "Temperature_C"] + wells + inst_hdr)
                for p in sorted(pts, key=lambda p: p["wl"]):
                    w.writerow([p["wl"], p["time"], p["temp"]] + [fmt(p["values"].get(x)) for x in wells] + inst)
        with open(os.path.join(self.folder, "plate_layout.csv"), "w", newline="") as f:
            w = csv.writer(f)
            for p in pts:
                w.writerow([f"Read {p['read']}", f"{p['wl']} nm", f"t={p['time']} s", f"{p['temp']} C",
                            self.instrument_text])
                w.writerow([""] + list(COLS))
                for r in ROWS:
                    w.writerow([r] + [fmt(p["values"].get(f"{r}{c}")) for c in COLS])
                w.writerow([])


# ---------------------------------------------------------------------------------------------
# Serial driver
# ---------------------------------------------------------------------------------------------

# Talks to the reader over the serial port (or to SimulatedReader). Every exchange looks like:
#     send    "! <command>\n"              "!" = machine mode: the reader does not echo the command
#     reply   "OK\t\r\n>"                  acknowledgement + prompt (commands that just do something)
#        or   "OK\t\r\n>\r\n<body>\r\n>"   queries such as status, temp, transfer
#        or   "FAIL\t <code>\t\r\n>"        rejected; <code> is explained in ERROR_TEXT
# 'traffic' is an optional callback(direction, bytes, context) that mirrors everything to the Terminal
# tab; 'context' records whether the exchange came from the user, background polling or a run.
class VersaMax:
    ACK_QUIET = 0.5   # after a bare "OK>", wait this long for a possible body before returning

    def __init__(self, port, traffic=None):
        if port == SIM_PORT:
            self.ser = SimulatedReader()
        else:
            if serial is None:
                raise RuntimeError("pyserial is not installed. Run:  python3 -m pip install pyserial")
            self.ser = serial.Serial(port, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.05,
                                     xonxoff=False, rtscts=False, dsrdtr=False)
        self.port = port
        self.traffic = traffic or (lambda direction, data, context: None)
        self.context = "user"
        # one exchange at a time, whoever calls
        self.lock = threading.RLock()
        # give the USB adapter a moment after opening, then discard anything already buffered
        time.sleep(0.3)
        self._drain()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # Read and discard bytes already waiting (late replies, stray prompts) so the next reply starts clean.
    def _drain(self):
        stale = b""
        while True:
            b = self.ser.read(4096)
            if not b:
                break
            stale += b
        if stale:
            self.traffic("<<", stale, "stray")
        return stale

    def command(self, cmd, timeout=15.0, expect_body=False):
        """Send '! cmd' and return the reply body (str, may be empty). Raises ReaderError on FAIL."""
        with self.lock:
            self._drain()
            out = ("! " + cmd + "\n").encode("ascii")
            self.ser.write(out)
            self.traffic(">>", out, self.context)
            buf, t0 = b"", time.monotonic()
            last = t0
            body = None
            while body is None:
                chunk = self.ser.read(4096)
                now = time.monotonic()
                if chunk:
                    buf += chunk
                    last = now
                # discard anything before the reply start
                starts = [i for i in (buf.find(b"OK"), buf.find(b"FAIL")) if i >= 0]
                if starts and min(starts) > 0:
                    buf = buf[min(starts):]
                # complete FAIL reply -> raise with its error code
                if buf.startswith(b"FAIL") and buf.endswith(b">"):
                    self.traffic("<<", buf, self.context)
                    m = re.search(rb"FAIL\s+(\d+)", buf)
                    raise ReaderError(int(m.group(1)) if m else 999, cmd)
                # OK reply: the first '>' ends the acknowledgement. A body, if any,
                # follows and ends with a second '>'. Commands without a body count as
                # finished once the line has been quiet for ACK_QUIET seconds; queries
                # (expect_body=True) always wait for their body.
                if buf.startswith(b"OK"):
                    i = buf.find(b">")
                    if i >= 0:
                        rest = buf[i + 1:]
                        if rest.endswith(b">"):
                            body = rest[:-1]
                        elif not rest and not expect_body and now - last > self.ACK_QUIET:
                            body = b""
                if now - t0 > timeout:
                    self.traffic("<<", buf, self.context)
                    raise ReaderTimeout(f"No complete reply to '{cmd}' within {timeout:g} s "
                                        f"(received {buf!r}). Is the reader on and connected?")
            self.traffic("<<", buf, self.context)
            # normalise line endings to \n for the parsers
            text = body.decode("ascii", "replace").replace("\r\n", "\n").replace("\r", "\n")
            return text.strip("\n")

    def raw(self, data, quiet=0.7, maxwait=30.0):
        """Terminal mode: send bytes exactly as given, return whatever comes back."""
        with self.lock:
            self._drain()
            self.ser.write(data)
            self.traffic(">>", data, "terminal")
            buf, t0 = b"", time.monotonic()
            last = t0
            while True:
                chunk = self.ser.read(4096)
                now = time.monotonic()
                if chunk:
                    buf += chunk
                    last = now
                if buf and now - last > quiet:
                    break
                if not buf and now - t0 > 3.0:
                    break
                if now - t0 > maxwait:
                    break
            self.traffic("<<", buf, "terminal")
            return buf

    # ---- typed helpers ------------------------------------------------------------------
    # '! status' -> (drawer 'OPEN'|'CLOSED', state 'IDLE'|'MEASURING'|'AIR CAL'|'FATAL…', detail)
    def status(self):
        lines = [l.split("\t") for l in self.command("status", expect_body=True).split("\n")]
        drawer = lines[0][0].strip() if lines else "?"
        state = lines[1][0].strip() if len(lines) > 1 else "?"
        detail = " ".join(p.strip() for p in lines[1][1:] if p.strip()) if len(lines) > 1 else ""
        return drawer, state, detail

    # Number of finished datasets waiting in the reader's memory.
    def queue(self):
        return int(self.command("queue", expect_body=True).split()[0])

    # (setpoint, chamber) in °C; a setpoint of 0 means the incubator is off.
    def temperature(self):
        parts = self.command("temp", expect_body=True).split()
        return float(parts[0]), float(parts[1])

    # Fetch - and remove from the reader's memory - the oldest waiting dataset.
    def transfer(self):
        return parse_dataset(self.command("transfer", timeout=60, expect_body=True))

    # ('VERSAmax', '3.13 31Oct98'): model from the first line of '! option', firmware from '! ver'.
    def identify(self):
        option = self.command("option", expect_body=True).split("\n")
        ver = self.command("ver", expect_body=True).strip()
        return option[0].strip(), " ".join(ver.split())

    def current_settings(self):
        """Read the reader's current configuration into a ReadSettings."""
        s = ReadSettings()
        # reply examples: 'ENDPOINT', 'KINETIC 30.0 10', 'SPECTRUM 400 10 31'
        mode = self.command("mode", expect_body=True).split()
        s.mode = mode[0].lower() if mode else "endpoint"
        if s.mode == "kinetic" and len(mode) >= 3:
            s.kin_interval, s.kin_reads = float(mode[1]), int(mode[2])
        if s.mode == "spectrum" and len(mode) >= 4:
            s.spec_start, s.spec_step = int(mode[1]), int(mode[2])
            s.spec_stop = s.spec_start + s.spec_step * (int(mode[3]) - 1)
        s.wavelengths = [int(x) for x in re.findall(r"(\d+)\s*nm", self.command("wavelength", expect_body=True))] or [405]
        # the query returns first and LAST column (whereas setting it takes first and count)
        strip = [int(x) for x in self.command("strip", expect_body=True).split()]
        if len(strip) >= 2:
            s.first_col, s.last_col = strip[0], strip[1]
        # e.g. 'OFF:  5.0  3.0  0.0  0.0  0.0' -> state, before (s), between (s), unused parameters
        shake = self.command("shake", expect_body=True).replace(":", " ").split()
        if shake:
            s.automix = shake[0].lower() if shake[0].lower() in ("on", "off", "once") else "off"
            nums = [float(x) for x in shake[1:]]
            if len(nums) >= 2:
                s.mix_before = nums[0] or 5.0
                s.mix_between = nums[1] or 3.0
        cal = self.command("calibrate", expect_body=True).strip().lower()
        s.calibrate = cal if cal in ("on", "off", "once") else "once"
        # reply is 'Normal mode' or 'Fast  mode'
        s.speed = "fast" in self.command("speed", expect_body=True).lower()
        return s


# ---------------------------------------------------------------------------------------------
# Simulator (mimics the byte-level behaviour recorded from the real reader)
# ---------------------------------------------------------------------------------------------

# A software stand-in for the instrument, used with --simulate and for testing without hardware.
# It reproduces the reply formats recorded from a real VersaMax (firmware 3.13), including quirks
# such as the extra '\r' in the queue reply and the missing leading zero on values below 0.1.
# Timing is shortened and OD values are synthetic. It implements just enough of pyserial's Serial
# interface for the driver: write(), read(), in_waiting and close().
class SimulatedReader:
    def __init__(self):
        # reader clock starts at ~600 s, as if switched on 10 minutes ago
        self.t0 = time.monotonic() - 600
        self.inbuf = b""
        self.out = b""
        # instrument state, mirroring what the real reader remembers between commands
        self.mode = ["ENDPOINT"]
        self.wl = [405]
        self.strip = [1, 12]
        self.shake = ["OFF", 5.0, 0.0, 0.0, 0.0, 0.0]
        self.cal = "ONCE"
        self.speed = False
        self.setpoint = 0.0
        self.chamber = 22.4
        self.drawer = "CLOSED"
        self.drawer_due = None
        self.data = []          # ready datasets (bytes bodies)
        self.pending = []       # (due_time, body_builder)
        self.errors = []
        self.last_temp_t = time.monotonic()

    # serial-like interface
    def close(self):
        pass

    # Commands may arrive in pieces; handle each complete line (ended by CR or LF).
    def write(self, data):
        self.inbuf += data
        while True:
            m = re.search(rb"[\r\n]", self.inbuf)
            if not m:
                break
            line, self.inbuf = self.inbuf[:m.start()], self.inbuf[m.end():]
            line = line.decode("ascii", "replace").strip()
            if line:
                self._handle(line)

    # Advance simulated time, then hand out up to n bytes of reply.
    def read(self, n=1):
        self._tick()
        time.sleep(0.01)
        b, self.out = self.out[:n], self.out[n:]
        return b

    @property
    def in_waiting(self):
        return len(self.out)

    # internals
    def now(self):
        return time.monotonic() - self.t0

    # Simulated physics: chamber temperature drifts toward the setpoint, drawer movements complete,
    # and scheduled measurements turn into datasets waiting to be transferred.
    def _tick(self):
        t = time.monotonic()
        dt, self.last_temp_t = t - self.last_temp_t, t
        target = max(self.setpoint, 22.4) if self.setpoint else 22.4
        self.chamber += (target - self.chamber) * min(1.0, dt / 60.0)
        if self.drawer_due and t >= self.drawer_due[0]:
            self.drawer, self.drawer_due = self.drawer_due[1], None
        while self.pending and self.pending[0][0] <= t:
            _, builder = self.pending.pop(0)
            self.data.append(builder())

    # Queue a reply in exactly the real reader's format (see the VersaMax class comment).
    def _reply(self, body=None, fail=None):
        if fail:
            self.errors.append(fail)
            self.out += f"FAIL\t {fail}\t\r\n>".encode()
        elif body is None:
            self.out += b"OK\t\r\n>"
        else:
            self.out += b"OK\t\r\n>\r\n" + body.encode() + b"\r\n>"

    # Synthetic but plausible OD: plastic baseline + a peak near 450 nm in some rows + slow growth in
    # kinetic mode + higher absorbance in the UV.
    def _od(self, well_r, col, wl, t):
        base = 0.09 + 0.004 * well_r + 0.002 * col
        peak = 0.25 * (well_r % 4) * pow(2.718, -((wl - 450) / 60.0) ** 2)
        growth = 0.0004 * t * (1 + well_r) if self.mode[0] == "KINETIC" else 0
        uv = 0.35 if wl < 360 else 0
        return base + peak + growth + uv

    @staticmethod
    # Real reader formatting: 8 wide, 4 decimals, no leading zero below 0.1 (e.g. '   .0978').
    def _fmt(v):
        s = f"{v:.4f}"
        if v < 0.1 and s.startswith("0."):
            s = s[1:]
        return s.rjust(8)

    # Build one transfer body for the current columns and the given wavelengths.
    def _dataset(self, wls):
        t = self.now()
        lines = [f"{t:.1f}\t {self.chamber:.1f}\tplate", "L:\t " + "\t ".join(str(w) for w in wls)]
        for wl in wls:
            lines.append(f"L:\t{wl}\t")
            for col in range(self.strip[0], self.strip[1] + 1):
                vals = [self._fmt(self._od(r, col, wl, t)) for r in range(8)]
                lines.append(f"{col:2d}:\t" + "\t".join(vals) + "\t")
            lines.append("")
        lines.append(str(100 + int(t) % 150))
        return "\r\n".join(lines)

    def _measuring(self):
        return bool(self.pending)

    # Interpret one command line and queue the reply. Without the '!' prefix the real reader echoes the
    # command back, so the simulator does too.
    def _handle(self, line):
        machine = line.startswith("!")
        if not machine:
            self.out += line.encode() + b"\r\n"
        line = line.lstrip("!").strip()
        parts = line.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        # the real reader refuses to be reconfigured while measuring (error 107)
        busy_cmds = ("mode", "wavelength", "strip", "read", "open", "close")
        if self._measuring() and cmd in busy_cmds:
            return self._reply(fail=107)
        if cmd == "status":
            if self._measuring():
                state = "AIR CAL\t" + f"{self.wl[0]} \t" if self.pending[0][0] - time.monotonic() > 2.5 \
                    else "MEASURING\t"
            else:
                state = "IDLE\t"
            return self._reply(f"{self.drawer}\t\r\n{state}")
        if cmd == "queue":
            return self._reply(f"\r{len(self.data)}")
        if cmd == "transfer":
            if not self.data:
                return self._reply(fail=108)
            return self._reply(self.data.pop(0))
        if cmd == "temp":
            if args:
                try:
                    self.setpoint = float(args[0])
                except ValueError:
                    return self._reply(fail=101)
                return self._reply()
            sp = "0" if not self.setpoint else f"{self.setpoint:.1f}"
            return self._reply(f"{sp}\t {self.chamber:.1f}\t ")
        if cmd == "mode":
            if not args:
                return self._reply(" ".join(str(x) for x in self.mode))
            m = args[0].upper()
            if m.startswith("END"):
                self.mode = ["ENDPOINT"]
            elif m.startswith("KIN") and len(args) == 3:
                self.mode = ["KINETIC", f"{float(args[1]):.1f}", int(args[2])]
            elif m.startswith("SPEC") and len(args) == 4:
                self.mode = ["SPECTRUM", int(args[1]), int(args[2]), int(args[3])]
            else:
                return self._reply(fail=104)
            return self._reply()
        if cmd == "wavelength":
            if not args:
                return self._reply("".join(f"{w}nm \t" for w in self.wl))
            # like the real reader: >8 wavelengths gives error 103, yet the first 8 are still applied
            self.wl = [int(a) for a in args[:8]]
            return self._reply(fail=103) if len(args) > 8 else self._reply()
        if cmd == "strip":
            if not args:
                return self._reply(f"{self.strip[0]}\t {self.strip[1]}\t")
            first, count = int(args[0]), int(args[1])
            self.strip = [first, first + count - 1]
            return self._reply()
        if cmd == "shake":
            if not args:
                return self._reply(f"{self.shake[0]}:\t" + "\t".join(f" {v:.1f}" for v in self.shake[1:]) + "\t")
            if args[0].lower() in ("on", "off", "once"):
                self.shake[0] = args[0].upper()
            elif args[0].lower() not in ("now", "stop"):
                self.shake[1:] = [float(a) for a in (args + ["0"] * 5)[:5]]
            return self._reply()
        if cmd == "calibrate":
            if not args:
                return self._reply(self.cal)
            self.cal = args[0].upper()
            return self._reply()
        if cmd == "speed":
            if args:
                self.speed = args[0].lower() == "on"
            return self._reply("Fast  mode\t" if self.speed else "Normal mode\t")
        if cmd in ("open", "close"):
            self.drawer_due = (time.monotonic() + 1.5, "OPEN" if cmd == "open" else "CLOSED")
            return self._reply()
        # schedule datasets: endpoint = one; kinetic = one per interval (never faster than a read takes);
        # spectrum = one per wavelength
        if cmd == "read":
            start = time.monotonic() + (3.0 if self.cal != "OFF" else 0.5)
            ncols = self.strip[1] - self.strip[0] + 1
            per = max(1.0, 0.25 * ncols)
            if self.mode[0] == "ENDPOINT":
                self.pending = [(start + per * len(self.wl), lambda: self._dataset(self.wl))]
            elif self.mode[0] == "KINETIC":
                iv = max(float(self.mode[1]), per * len(self.wl))
                self.pending = [(start + per + i * iv, lambda: self._dataset(self.wl)) for i in range(int(self.mode[2]))]
            else:
                s0, step, n = self.mode[1:]
                self.pending = [(start + per * (i + 1), (lambda w: lambda: self._dataset([w]))(s0 + i * step))
                                for i in range(n)]
            return self._reply()
        if cmd == "cancel":
            self.pending = []
            return self._reply()
        if cmd == "clear":
            if args and args[0].lower() == "data":
                self.data = []
            elif args and args[0].lower() == "error":
                self.errors = []
            return self._reply()
        if cmd == "option":
            return self._reply("VERSAmax (simulated)\r\n340 \t850 \t1\r\n15 \t45")
        if cmd == "ver":
            return self._reply("3.13     31Oct98 (simulator)")
        if cmd == "error":
            if args and args[0] == "count":
                return self._reply(str(len(self.errors)))
            if args and args[0] == "list":
                return self._reply("\r\n".join(f"{k} {v}" for k, v in ERROR_TEXT.items()))
            return self._reply(str(self.errors[-1]) if self.errors else "0")
        if cmd == "help":
            return self._reply("Simulator: status queue transfer temp mode wavelength strip shake\r\n"
                               "calibrate speed open close read cancel clear option ver error help")
        if cmd in ("beep", "reset", "remote"):
            return self._reply()
        # anything else: 'command not found'
        return self._reply(fail=100)


# ---------------------------------------------------------------------------------------------
# Background worker: owns the serial port, runs one job at a time
# ---------------------------------------------------------------------------------------------

# The single background thread that owns the serial port. GUI code calls submit(name, fn); jobs run
# strictly one after another, so serial exchanges never overlap. Jobs must not touch Tk widgets -
# they report back through App.post(...). Exceptions become 'job_error' events for the GUI.
class Worker(threading.Thread):
    def __init__(self, events):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.events = events
        self.busy = False

    def submit(self, name, fn):
        self.jobs.put((name, fn))

    def idle(self):
        return not self.busy and self.jobs.empty()

    def run(self):
        while True:
            name, fn = self.jobs.get()
            # submit(name, None) is the shutdown signal
            if fn is None:
                return
            self.busy = True
            try:
                fn()
            except Exception as e:  # reported to the GUI, never crashes the worker
                self.events.put(("job_error", name, e))
            finally:
                self.busy = False


# ---------------------------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------------------------

# Line colours for up to 12 wells on the Graph tab.
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b",
           "#e377c2", "#17becf", "#bcbd22", "#7f7f7f", "#393b79", "#637939"]
# Internal setting values -> wording shown in the GUI (inverted again when the form is read back).
MODE_LABELS = {"endpoint": "Endpoint", "kinetic": "Kinetic", "spectrum": "Spectrum scan"}
AUTOMIX_LABELS = {"off": "Off", "once": "Before first read", "on": "Before first read + between reads"}
CAL_LABELS = {"once": "Once per run (first read)", "on": "Before every read", "off": "Off"}


# Map an OD value onto a light-to-dark blue scale between lo and hi. Returns (background, text colour),
# using white text on dark cells so values stay readable.
def heat_color(v, lo, hi):
    if v is None:
        return "#eeeeee", "#888888"
    f = 0.0 if hi <= lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
    c0, c1 = (247, 251, 255), (33, 102, 172)
    rgb = tuple(int(a + (b - a) * f) for a, b in zip(c0, c1))
    return "#%02x%02x%02x" % rgb, ("#ffffff" if f > 0.55 else "#111111")


# The main window. Layout (built in _build_ui):
#   top bar       port selector, Connect, status light, reader model / firmware
#   left column   Instrument (drawer, incubator, shake), Read setup, Run (start / cancel, progress,
#                 save folder, reader memory)
#   right tabs    Plate (heat map + well selection), Graph, Terminal (direct reader commands), Log
#   bottom        one-line status message
#
# Naming: _job_* methods (and the job() closures) run on the Worker thread and do serial I/O only;
# everything else runs on the GUI thread. self.post(...) is the bridge from worker to GUI.
class App:
    def __init__(self, root, args):
        self.root = root
        self.events = queue.Queue()
        self.worker = Worker(self.events)
        self.worker.start()
        self.reader = None                 # touched only inside worker jobs
        self.connected = False
        self.running = False
        # set by the Cancel button, checked by the run loop
        self.cancel_flag = threading.Event()
        self.runs = {}                     # id -> RunData
        self.view_run = None
        self.selected = set()
        self.last = {}                     # last polled status
        self.reader_info = {}              # model / firmware / port of the connected reader (saved with runs)
        self.history, self.hist_pos = [], 0
        self.data_dir = tk.StringVar(value=DEFAULT_DATA_DIR)
        self.run_started_at = None
        self.run_progress = (0, 0)

        # size the window to the screen (the layout needs roughly 1400 x 880 points)
        root.title(APP_NAME)
        w = min(1480, root.winfo_screenwidth() - 60)
        h = min(960, root.winfo_screenheight() - 90)
        root.geometry(f"{w}x{h}+20+20")
        root.minsize(1150, 760)
        self._build_ui()
        self._refresh_ports(select=args.port or (SIM_PORT if args.simulate else None))
        self._apply_settings_to_form(ReadSettings())
        # start the worker->GUI event pump and the 2-second background status poll
        root.after(100, self._process_events)
        root.after(2000, self._poll_tick)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if args.simulate or args.port:
            root.after(300, self._toggle_connect)

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        style = ttk.Style()
        style.configure("Big.TButton", font=("Helvetica", 14, "bold"), padding=8)
        style.configure("Head.TLabel", font=("Helvetica", 12, "bold"))

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=30, state="readonly")
        self.port_box.pack(side="left", padx=4)
        ttk.Button(top, text="↻", width=3, command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=8)
        self.dot = tk.Canvas(top, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 4))
        self._set_dot("#999999")
        self.status_lbl = ttk.Label(top, text="Not connected", style="Head.TLabel")
        self.status_lbl.pack(side="left")
        self.ident_lbl = ttk.Label(top, text="", foreground="#666666")
        self.ident_lbl.pack(side="right")

        # main area: control column on the left, tabbed views on the right
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        left = ttk.Frame(body, padding=4)
        right = ttk.Frame(body, padding=4)
        body.add(left, weight=0)
        body.add(right, weight=1)
        self._build_controls(left)
        self._build_tabs(right)

        # status bar along the bottom
        self.msg_var = tk.StringVar(value="Select the reader's port (usually /dev/cu.usbserial-…) and press Connect.")
        ttk.Label(self.root, textvariable=self.msg_var, relief="sunken", anchor="w",
                  padding=(8, 3)).pack(fill="x", side="bottom")

    def _build_controls(self, parent):
        self.controls = []   # widgets disabled while disconnected / running

        # --- Instrument: actions that are sent to the reader immediately ---
        inst = ttk.LabelFrame(parent, text="Instrument", padding=8)
        inst.pack(fill="x", pady=(0, 6))
        r = ttk.Frame(inst)
        r.pack(fill="x")
        ttk.Label(r, text="Drawer:", width=11).pack(side="left")
        for txt, cmd in (("Open", "open"), ("Close", "close")):
            b = ttk.Button(r, text=txt, width=7, command=lambda c=cmd: self._simple(c, f"Drawer: {c}"))
            b.pack(side="left", padx=2)
            self.controls.append(b)
        self.drawer_lbl = ttk.Label(r, text="", foreground="#555555")
        self.drawer_lbl.pack(side="left", padx=6)

        r = ttk.Frame(inst)
        r.pack(fill="x", pady=4)
        ttk.Label(r, text="Incubator:", width=11).pack(side="left")
        self.inc_on = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(r, text="On", variable=self.inc_on)
        cb.pack(side="left")
        self.inc_sp = tk.StringVar(value="37.0")
        sp = ttk.Spinbox(r, from_=TEMP_MIN, to=TEMP_MAX, increment=0.5, width=6, textvariable=self.inc_sp)
        sp.pack(side="left", padx=4)
        ttk.Label(r, text="°C").pack(side="left")
        b = ttk.Button(r, text="Apply", width=6, command=self._apply_temp)
        b.pack(side="left", padx=4)
        self.controls += [cb, sp, b]
        self.temp_lbl = ttk.Label(inst, text="Chamber: –", foreground="#555555")
        self.temp_lbl.pack(anchor="w", padx=(88, 0))

        r = ttk.Frame(inst)
        r.pack(fill="x", pady=(4, 0))
        ttk.Label(r, text="Shake:", width=11).pack(side="left")
        for txt, cmd in (("Shake now", "shake now"), ("Stop", "shake stop")):
            b = ttk.Button(r, text=txt, width=9, command=lambda c=cmd: self._simple(c, c.capitalize()))
            b.pack(side="left", padx=2)
            self.controls.append(b)

        # --- Read setup: gathered into a ReadSettings when 'Start read' is pressed ---
        setup = ttk.LabelFrame(parent, text="Read setup", padding=8)
        setup.pack(fill="x", pady=(0, 6))
        g = ttk.Frame(setup)
        g.pack(fill="x")
        ttk.Label(g, text="Name:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value="experiment")
        ttk.Entry(g, textvariable=self.name_var, width=28).grid(row=0, column=1, columnspan=3, sticky="we", pady=2)
        ttk.Label(g, text="Mode:").grid(row=1, column=0, sticky="w")
        self.mode_var = tk.StringVar(value="endpoint")
        mf = ttk.Frame(g)
        mf.grid(row=1, column=1, columnspan=3, sticky="w")
        for m, lbl in MODE_LABELS.items():
            ttk.Radiobutton(mf, text=lbl, value=m, variable=self.mode_var,
                            command=self._mode_changed).pack(side="left", padx=(0, 8))

        # mode-specific panels live inside mode_slot so they are drawn above it
        self.mode_slot = ttk.Frame(setup)
        self.mode_slot.pack(fill="x", pady=4)

        # wavelengths (endpoint / kinetic)
        self.wl_frame = ttk.Frame(self.mode_slot)
        ttk.Label(self.wl_frame, text="Number of wavelengths:").grid(row=0, column=0, columnspan=4, sticky="w")
        self.n_wl = tk.IntVar(value=1)
        ttk.Spinbox(self.wl_frame, from_=1, to=8, width=4, textvariable=self.n_wl,
                    command=self._wl_count_changed).grid(row=0, column=4, sticky="w")
        self.wl_vars, self.wl_entries = [], []
        for i in range(8):
            v = tk.StringVar(value="")
            e = ttk.Entry(self.wl_frame, textvariable=v, width=6)
            e.grid(row=1 + i // 4, column=i % 4, padx=2, pady=2, sticky="w")
            self.wl_vars.append(v)
            self.wl_entries.append(e)
        ttk.Label(self.wl_frame, text=f"nm ({WL_MIN}–{WL_MAX})", foreground="#666666").grid(row=1, column=4, sticky="w")

        # kinetic
        self.kin_frame = ttk.Frame(self.mode_slot)
        ttk.Label(self.kin_frame, text="Interval (s):").grid(row=0, column=0, sticky="w")
        self.kin_iv = tk.StringVar(value="30")
        ttk.Entry(self.kin_frame, textvariable=self.kin_iv, width=8).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(self.kin_frame, text="Number of reads:").grid(row=1, column=0, sticky="w")
        self.kin_n = tk.StringVar(value="10")
        ttk.Entry(self.kin_frame, textvariable=self.kin_n, width=8).grid(row=1, column=1, sticky="w", padx=4)
        self.kin_info = ttk.Label(self.kin_frame, text="", foreground="#555555", wraplength=330, justify="left")
        self.kin_info.grid(row=2, column=0, columnspan=3, sticky="w")

        # spectrum
        self.spec_frame = ttk.Frame(self.mode_slot)
        self.sp_start, self.sp_stop, self.sp_step = tk.StringVar(value="400"), tk.StringVar(value="700"), tk.StringVar(value="10")
        for i, (lbl, var) in enumerate((("Start (nm):", self.sp_start), ("Stop (nm):", self.sp_stop), ("Step (nm):", self.sp_step))):
            ttk.Label(self.spec_frame, text=lbl).grid(row=0, column=2 * i, sticky="w")
            ttk.Entry(self.spec_frame, textvariable=var, width=6).grid(row=0, column=2 * i + 1, sticky="w", padx=(2, 8))
        self.spec_info = ttk.Label(self.spec_frame, text="", foreground="#555555")
        self.spec_info.grid(row=1, column=0, columnspan=6, sticky="w")

        # settings shared by all modes
        g2 = ttk.Frame(setup)
        g2.pack(fill="x")
        ttk.Label(g2, text="Columns:").grid(row=0, column=0, sticky="w")
        self.col_a, self.col_b = tk.StringVar(value="1"), tk.StringVar(value="12")
        cf = ttk.Frame(g2)
        cf.grid(row=0, column=1, columnspan=3, sticky="w")
        ttk.Spinbox(cf, from_=1, to=12, width=4, textvariable=self.col_a).pack(side="left")
        ttk.Label(cf, text=" to ").pack(side="left")
        ttk.Spinbox(cf, from_=1, to=12, width=4, textvariable=self.col_b).pack(side="left")
        ttk.Label(g2, text="AutoMix:").grid(row=1, column=0, sticky="w", pady=2)
        self.mix_var = tk.StringVar(value=AUTOMIX_LABELS["off"])
        ttk.Combobox(g2, textvariable=self.mix_var, values=list(AUTOMIX_LABELS.values()), state="readonly",
                     width=30).grid(row=1, column=1, columnspan=3, sticky="w")
        mf2 = ttk.Frame(g2)
        mf2.grid(row=2, column=1, columnspan=3, sticky="w")
        self.mix_before, self.mix_between = tk.StringVar(value="5"), tk.StringVar(value="3")
        ttk.Label(mf2, text="before (s)").pack(side="left")
        ttk.Entry(mf2, textvariable=self.mix_before, width=5).pack(side="left", padx=(2, 10))
        ttk.Label(mf2, text="between (s)").pack(side="left")
        ttk.Entry(mf2, textvariable=self.mix_between, width=5).pack(side="left", padx=2)
        ttk.Label(g2, text="Calibration:").grid(row=3, column=0, sticky="w", pady=2)
        self.cal_var = tk.StringVar(value=CAL_LABELS["once"])
        ttk.Combobox(g2, textvariable=self.cal_var, values=list(CAL_LABELS.values()), state="readonly",
                     width=30).grid(row=3, column=1, columnspan=3, sticky="w")
        self.speed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(g2, text="Speed read (uses stored air calibration)", variable=self.speed_var).grid(
            row=4, column=1, columnspan=3, sticky="w")
        pf = ttk.Frame(setup)
        pf.pack(fill="x", pady=(6, 0))
        ttk.Button(pf, text="Load protocol…", command=self._load_protocol).pack(side="left")
        ttk.Button(pf, text="Save protocol…", command=self._save_protocol).pack(side="left", padx=4)
        b = ttk.Button(pf, text="Get from reader", command=self._fetch_reader_settings)
        b.pack(side="left")
        self.controls.append(b)

        # refresh the run-time / spectrum summaries live while the operator types
        for v in (self.kin_iv, self.kin_n, self.col_a, self.col_b, self.n_wl, self.sp_start, self.sp_stop, self.sp_step):
            v.trace_add("write", lambda *a: self._update_info())
        for v in self.wl_vars:
            v.trace_add("write", lambda *a: self._update_info())

        # --- Run: start / cancel, progress, where data is saved, data held in the reader ---
        run = ttk.LabelFrame(parent, text="Run", padding=8)
        run.pack(fill="x", pady=(0, 6))
        r = ttk.Frame(run)
        r.pack(fill="x")
        self.start_btn = ttk.Button(r, text="▶  Start read", style="Big.TButton", command=self._start_run)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(r, text="■  Cancel", style="Big.TButton", command=self._cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(fill="x", pady=(6, 2))
        self.progress_lbl = ttk.Label(run, text="Idle", foreground="#555555")
        self.progress_lbl.pack(anchor="w")
        r = ttk.Frame(run)
        r.pack(fill="x", pady=(6, 0))
        ttk.Label(r, text="Save to:").pack(side="left")
        ttk.Label(r, textvariable=self.data_dir, foreground="#555555", width=30).pack(side="left", padx=4)
        ttk.Button(r, text="Change…", command=self._choose_dir).pack(side="left")
        r = ttk.Frame(run)
        r.pack(fill="x", pady=(6, 0))
        self.queue_lbl = ttk.Label(r, text="Reader memory: –")
        self.queue_lbl.pack(side="left")
        b1 = ttk.Button(r, text="Retrieve & save", command=self._retrieve_stored)
        b1.pack(side="right")
        b2 = ttk.Button(r, text="Clear memory", command=self._clear_stored)
        b2.pack(side="right", padx=4)
        self.controls += [b1, b2, self.start_btn]
        self._set_controls_state()

    def _build_tabs(self, parent):
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill="both", expand=True)

        # Plate tab
        plate = ttk.Frame(self.nb, padding=8)
        self.nb.add(plate, text="Plate")
        bar = ttk.Frame(plate)
        bar.pack(fill="x")
        ttk.Label(bar, text="Showing:").pack(side="left")
        self.point_var = tk.StringVar()
        self.point_box = ttk.Combobox(bar, textvariable=self.point_var, state="readonly", width=44)
        self.point_box.pack(side="left", padx=4)
        self.point_box.bind("<<ComboboxSelected>>", lambda e: self._draw_plate())
        bar2 = ttk.Frame(plate)
        bar2.pack(fill="x", pady=(4, 0))
        ttk.Button(bar2, text="Copy plate (for Excel)", command=self._copy_plate).pack(side="left")
        ttk.Button(bar2, text="Open results folder", command=self._open_folder).pack(side="left", padx=4)
        ttk.Button(bar2, text="Open previous run…", command=self._open_run).pack(side="left")
        self.run_lbl = ttk.Label(plate, text="No data yet.", foreground="#555555")
        self.run_lbl.pack(anchor="w", pady=(6, 4))
        # 8 x 12 well grid. Each well is a Label inside a small Frame; the Frame's colour is the selection
        # border (macOS does not draw highlight rings around Labels). Clicking either toggles selection.
        grid = tk.Frame(plate)
        grid.pack(anchor="nw")
        self.cells = {}
        self.cell_borders = {}   # frame around each well; its colour shows selection (macOS ignores label highlights)
        self.grid_bg = grid.cget("bg")
        tk.Label(grid, text="", width=3).grid(row=0, column=0)
        for c in COLS:
            tk.Label(grid, text=str(c), font=("Helvetica", 12, "bold")).grid(row=0, column=c)
        for ri, r in enumerate(ROWS):
            tk.Label(grid, text=r, font=("Helvetica", 12, "bold"), width=3).grid(row=ri + 1, column=0)
            for c in COLS:
                w = f"{r}{c}"
                border = tk.Frame(grid, bg=self.grid_bg, padx=3, pady=3)
                border.grid(row=ri + 1, column=c, padx=0, pady=0)
                lbl = tk.Label(border, text="", width=7, height=2, font=("Menlo", 12), bg="#eeeeee",
                               relief="flat", bd=0)
                lbl.pack()
                for widget in (border, lbl):
                    widget.bind("<Button-1>", lambda e, well=w: self._toggle_well(well))
                self.cells[w] = lbl
                self.cell_borders[w] = border
        sel = ttk.Frame(plate)
        sel.pack(fill="x", pady=6)
        ttk.Label(sel, text="Click wells to select them for the Graph tab (selected = orange border).", foreground="#555555").pack(side="left")
        ttk.Button(sel, text="Select all", command=self._select_all).pack(side="left", padx=6)
        ttk.Button(sel, text="Clear selection", command=self._clear_selection).pack(side="left")
        self.scale_lbl = ttk.Label(plate, text="", foreground="#555555")
        self.scale_lbl.pack(anchor="w")

        # Graph tab
        graph = ttk.Frame(self.nb, padding=8)
        self.nb.add(graph, text="Graph")
        gbar = ttk.Frame(graph)
        gbar.pack(fill="x")
        self.graph_mode_lbl = ttk.Label(gbar, text="")
        self.graph_mode_lbl.pack(side="left")
        ttk.Label(gbar, text="   Wavelength:").pack(side="left")
        self.graph_wl = tk.StringVar()
        self.graph_wl_box = ttk.Combobox(gbar, textvariable=self.graph_wl, state="readonly", width=10)
        self.graph_wl_box.pack(side="left")
        self.graph_wl_box.bind("<<ComboboxSelected>>", lambda e: self._draw_graph())
        self.canvas = tk.Canvas(graph, bg="#ffffff", highlightthickness=1, highlightbackground="#cccccc")
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda e: self._draw_graph())

        # Terminal tab
        term = ttk.Frame(self.nb, padding=8)
        self.nb.add(term, text="Terminal")
        tbar = ttk.Frame(term)
        tbar.pack(fill="x")
        ttk.Label(tbar, text="Line ending:").pack(side="left")
        # terminal options: line ending (LF by default), local echo, timestamps, hex view, background traffic
        self.eol_var = tk.StringVar(value="LF")
        ttk.Combobox(tbar, textvariable=self.eol_var, values=["LF", "CR", "CR+LF", "None"], width=7,
                     state="readonly").pack(side="left", padx=(2, 10))
        self.echo_var = tk.BooleanVar(value=True)
        self.ts_var = tk.BooleanVar(value=False)
        self.hex_var = tk.BooleanVar(value=False)
        self.poll_var = tk.BooleanVar(value=False)
        for txt, var in (("Local echo", self.echo_var), ("Timestamps", self.ts_var), ("Hex view", self.hex_var),
                         ("Show background traffic", self.poll_var)):
            ttk.Checkbutton(tbar, text=txt, variable=var).pack(side="left", padx=4)
        ttk.Button(tbar, text="Save log…", command=self._save_term).pack(side="right")
        ttk.Button(tbar, text="Clear", command=lambda: self.term.delete("1.0", "end")).pack(side="right", padx=4)
        tf = ttk.Frame(term)
        tf.pack(fill="both", expand=True, pady=6)
        self.term = tk.Text(tf, font=("Menlo", 12), wrap="none", bg="#1e1e1e", fg="#e6e6e6",
                            insertbackground="#e6e6e6")
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.term.yview)
        self.term.configure(yscrollcommand=ys.set)
        self.term.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        # colours: sent = blue, received = white, background traffic = grey, timestamps = light grey
        self.term.tag_configure("tx", foreground="#7fb8ff")
        self.term.tag_configure("rx", foreground="#e6e6e6")
        self.term.tag_configure("bg", foreground="#808080")
        self.term.tag_configure("ts", foreground="#a0a0a0")
        ebar = ttk.Frame(term)
        ebar.pack(fill="x")
        self.term_entry = ttk.Entry(ebar, font=("Menlo", 13))
        self.term_entry.pack(side="left", fill="x", expand=True)
        self.term_entry.insert(0, "! help")
        self.term_entry.bind("<Return>", lambda e: self._term_send())
        self.term_entry.bind("<Up>", lambda e: self._term_hist(-1))
        self.term_entry.bind("<Down>", lambda e: self._term_hist(1))
        ttk.Button(ebar, text="Send", command=self._term_send).pack(side="left", padx=4)
        ttk.Label(term, text="Type a reader command and press Return, e.g.  ! status   ! help mode   ! temp 37   "
                            "(↑/↓ = history)", foreground="#666666").pack(anchor="w", pady=(4, 0))
        # Caution about the reader's factory service commands, which the Terminal passes through unfiltered
        # and which can overwrite the calibration stored in the reader. The GUI buttons never send them.
        ttk.Label(term, text="⚠ Caution: the reader also accepts factory service commands (e.g. init, air_cal, "
                             "set_f900, set_f1000, caldac, calign, set_calflag) that can overwrite its stored "
                             "calibration. Only send commands you understand; check with  ! help <command>  first.",
                  foreground="#b45309", wraplength=820, justify="left").pack(anchor="w", pady=(4, 0))

        # Log tab
        logf = ttk.Frame(self.nb, padding=8)
        self.nb.add(logf, text="Log")
        self.log = tk.Text(logf, font=("Menlo", 11), wrap="word", height=10)
        self.log.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- helpers
    # Queue an event for the GUI thread. Safe to call from any thread.
    def post(self, *event):
        self.events.put(event)

    # Show a message in the status bar and append it to the Log tab.
    def say(self, msg, log=True):
        self.msg_var.set(msg)
        if log:
            self.log.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
            self.log.see("end")

    # Status light: grey = not connected, green = idle, orange = busy / reading, red = error.
    def _set_dot(self, color):
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 14, 14, fill=color, outline=color)

    # Instrument and setup buttons work only when connected and no read is running; Cancel only during a read.
    def _set_controls_state(self):
        enabled = self.connected and not self.running
        for w in self.controls:
            try:
                w.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        self.cancel_btn.configure(state="normal" if self.running else "disabled")

    # Driver traffic callback (runs on the worker) -> forwarded to the Terminal tab via the event queue.
    def _traffic(self, direction, data, context):
        self.post("traffic", direction, data, context)

    # List serial ports with USB-serial adapters first ('usbserial' names, or the CH340 chip, USB vendor
    # 0x1A86); the simulator is always last.
    def _refresh_ports(self, select=None):
        ports = []
        if serial is not None:
            found = list(serial.tools.list_ports.comports())
            found.sort(key=lambda p: (0 if ("usbserial" in p.device or (p.vid == 0x1A86)) else 1, p.device))
            ports = [p.device for p in found if p.device.startswith("/dev/cu.") or os.name == "nt"]
        ports.append(SIM_PORT)
        self.port_box["values"] = ports
        if select:
            self.port_var.set(select)
        elif not self.port_var.get() or self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    # ---------------------------------------------------------------- connection
    def _toggle_connect(self):
        if self.connected:
            if self.running:
                messagebox.showwarning(APP_NAME, "Cancel the running read before disconnecting.")
                return
            self.worker.submit("disconnect", self._job_disconnect)
            return
        port = self.port_var.get()
        if not port:
            return
        self.connect_btn.configure(state="disabled")
        self.say(f"Connecting to {port} …")
        self.worker.submit("connect", lambda: self._job_connect(port))

    # Worker: open the port, identify the reader, read its current settings and memory count.
    def _job_connect(self, port):
        try:
            r = VersaMax(port, traffic=self._traffic)
            r.context = "user"
            model, ver = r.identify()
            settings = r.current_settings()
            q = r.queue()
        except Exception as e:
            try:
                r.close()
            except Exception:
                pass
            self.post("connect_failed", e)
            return
        self.reader = r
        self.post("connected", port, model, ver, settings, q)

    def _job_disconnect(self):
        if self.reader:
            self.reader.close()
        self.reader = None
        self.post("disconnected")

    # Every 2 s refresh status / temperature / memory count. Skipped during a run (the run loop polls
    # itself) and while other jobs are queued, so polling never delays an operator action.
    def _poll_tick(self):
        if self.connected and not self.running and self.worker.idle():
            self.worker.submit("poll", self._job_poll)
        self.root.after(2000, self._poll_tick)

    def _job_poll(self):
        r = self.reader
        if not r:
            return
        r.context = "poll"
        try:
            st = r.status()
            temp = r.temperature()
            q = r.queue()
            self.post("status", st, temp, q)
        finally:
            r.context = "user"

    # Send one command that needs no reply handling (drawer, shake, incubator, clear memory), then
    # refresh the status display.
    def _simple(self, cmd, label):
        def job():
            self.reader.context = "user"
            self.reader.command(cmd)
            self.post("say", f"{label}: OK")
            self._job_poll()
        self.worker.submit(cmd, job)

    # Incubator: 'temp <setpoint>' starts heating, 'temp 0' turns it off. The reader cannot cool.
    def _apply_temp(self):
        if self.inc_on.get():
            try:
                sp = float(self.inc_sp.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Enter a temperature setpoint in °C.")
                return
            if not TEMP_MIN <= sp <= TEMP_MAX:
                messagebox.showerror(APP_NAME, f"Setpoint must be {TEMP_MIN:g}–{TEMP_MAX:g} °C.")
                return
            chamber = self.last.get("temp", (0, 0))[1]
            if chamber and sp < chamber + 4:
                if not messagebox.askokcancel(APP_NAME, "The reader can only heat. Regulation is accurate only "
                                              "when the setpoint is at least 4 °C above room temperature.\n\n"
                                              f"Set {sp:g} °C anyway?"):
                    return
            self._simple(f"temp {sp:g}", f"Incubator set to {sp:g} °C")
        else:
            self._simple("temp 0", "Incubator off")

    # ---------------------------------------------------------------- settings form
    # Show only the panels for the chosen mode: wavelengths for endpoint and kinetic, plus interval /
    # number of reads for kinetic, or start / stop / step for spectrum.
    def _mode_changed(self):
        for f in (self.wl_frame, self.kin_frame, self.spec_frame):
            f.pack_forget()
        m = self.mode_var.get()
        if m in ("endpoint", "kinetic"):
            self.wl_frame.pack(fill="x")
        if m == "kinetic":
            self.kin_frame.pack(fill="x", pady=(4, 0))
        if m == "spectrum":
            self.spec_frame.pack(fill="x")
        self._update_info()

    # Enable as many wavelength boxes as 'Number of wavelengths' asks for.
    def _wl_count_changed(self):
        try:
            n = max(1, min(8, int(self.n_wl.get())))
        except (ValueError, tk.TclError):
            n = 1
        for i, e in enumerate(self.wl_entries):
            e.configure(state="normal" if i < n else "disabled")
        self._update_info()

    # Recompute the helper text under the kinetic / spectrum fields (runs on every keystroke).
    def _update_info(self):
        try:
            s = self._form_settings(strict=False)
        except Exception:
            return
        if s.mode == "kinetic":
            total = s.kin_interval * (s.kin_reads - 1)
            txt = f"Total run time ≈ {total // 3600:.0f} h {(total % 3600) // 60:.0f} min {total % 60:.0f} s."
            if s.kin_interval < s.min_kinetic_interval():
                txt += f"  ⚠ Shorter than the ~{s.min_kinetic_interval():.0f} s the reader needs; it will stretch the interval."
            self.kin_info.configure(text=txt)
        if s.mode == "spectrum":
            try:
                self.spec_info.configure(text=f"{s.spec_count} wavelengths: {s.spec_start} → "
                                              f"{s.spec_start + s.spec_step * (s.spec_count - 1)} nm")
            except ZeroDivisionError:
                pass

    # Read the form into a ReadSettings. strict=True (before a run or saving a protocol) raises ValueError
    # with a readable message for any unusable entry; strict=False (live previews) substitutes 0.
    def _form_settings(self, strict=True):
        def num(var, typ, label):
            try:
                return typ(float(var.get()))
            except (ValueError, tk.TclError):
                if strict:
                    raise ValueError(f"'{var.get()}' is not a valid number for {label}.")
                return typ(0)
        try:
            n = max(1, min(8, int(self.n_wl.get())))
        except (ValueError, tk.TclError):
            n = 1
        wls = []
        for v in self.wl_vars[:n]:
            if v.get().strip():
                wls.append(num(v, int, "wavelength"))
            elif strict and self.mode_var.get() != "spectrum":
                raise ValueError("Fill in every wavelength box (or reduce the number of wavelengths).")
        inv_mix = {v: k for k, v in AUTOMIX_LABELS.items()}
        inv_cal = {v: k for k, v in CAL_LABELS.items()}
        return ReadSettings(
            # the run name becomes part of a folder name, so characters unsafe in file names are replaced
            name=re.sub(r"[^\w\- ]", "_", self.name_var.get().strip() or "experiment"),
            mode=self.mode_var.get(), wavelengths=wls,
            first_col=num(self.col_a, int, "first column"), last_col=num(self.col_b, int, "last column"),
            kin_interval=num(self.kin_iv, float, "kinetic interval"), kin_reads=num(self.kin_n, int, "number of reads"),
            spec_start=num(self.sp_start, int, "spectrum start"), spec_stop=num(self.sp_stop, int, "spectrum stop"),
            spec_step=num(self.sp_step, int, "spectrum step"),
            automix=inv_mix.get(self.mix_var.get(), "off"),
            mix_before=num(self.mix_before, float, "AutoMix before"),
            mix_between=num(self.mix_between, float, "AutoMix between"),
            calibrate=inv_cal.get(self.cal_var.get(), "once"), speed=bool(self.speed_var.get()))

    # The reverse: show a ReadSettings in the form (on connect, 'Get from reader', or loading a protocol).
    def _apply_settings_to_form(self, s):
        self.name_var.set(s.name)
        self.mode_var.set(s.mode)
        self.n_wl.set(max(1, min(8, len(s.wavelengths))))
        for i, v in enumerate(self.wl_vars):
            v.set(str(s.wavelengths[i]) if i < len(s.wavelengths) else "")
        self.col_a.set(str(s.first_col))
        self.col_b.set(str(s.last_col))
        self.kin_iv.set(f"{s.kin_interval:g}")
        self.kin_n.set(str(s.kin_reads))
        self.sp_start.set(str(s.spec_start))
        self.sp_stop.set(str(s.spec_stop))
        self.sp_step.set(str(s.spec_step))
        self.mix_var.set(AUTOMIX_LABELS.get(s.automix, "Off"))
        self.mix_before.set(f"{s.mix_before:g}")
        self.mix_between.set(f"{s.mix_between:g}")
        self.cal_var.set(CAL_LABELS.get(s.calibrate, CAL_LABELS["once"]))
        self.speed_var.set(s.speed)
        self._wl_count_changed()
        self._mode_changed()

    def _fetch_reader_settings(self):
        def job():
            self.reader.context = "user"
            s = self.reader.current_settings()
            self.post("form", s)
            self.post("say", "Loaded the reader's current settings into the form.")
        self.worker.submit("fetch", job)

    # Protocols are ReadSettings stored as JSON, with the reader they were saved from (if connected)
    # recorded under "instrument" for reference; that section is ignored when loading.
    def _save_protocol(self):
        try:
            s = self._form_settings()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"{s.name}.json",
                                            filetypes=[("Protocol", "*.json")])
        if path:
            with open(path, "w") as f:
                json.dump({**asdict(s), "instrument": self.reader_info}, f, indent=2)
            self.say(f"Protocol saved: {path}")

    # Unknown keys are ignored so protocol files from other versions still load.
    def _load_protocol(self):
        path = filedialog.askopenfilename(filetypes=[("Protocol", "*.json")])
        if not path:
            return
        try:
            with open(path) as f:
                d = json.load(f)
            known = ReadSettings.__dataclass_fields__
            self._apply_settings_to_form(ReadSettings(**{k: v for k, v in d.items() if k in known}))
            self.say(f"Protocol loaded: {path}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not load protocol:\n{e}")

    # The chosen folder applies until the program is closed.
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.data_dir.get())
        if d:
            self.data_dir.set(d)

    # ---------------------------------------------------------------- runs
    def _new_run(self, settings, suffix="", data_dir=None):
        """Create and register a RunData. Pass data_dir when calling from the worker thread."""
        base = data_dir or self.data_dir.get()
        folder = os.path.join(base, f"{datetime.now():%Y-%m-%d_%H%M%S}_{settings.name}{suffix}")
        # reader_info is a plain dict set on connect, so reading it from the worker thread is safe
        run = RunData(settings, folder, instrument=self.reader_info)
        self.runs[id(run)] = run
        return run

    # GUI side of 'Start read': validate the form, decide what to do with data left in the reader,
    # create the results folder, then hand the run itself to the worker (_job_run).
    def _start_run(self):
        try:
            s = self._form_settings()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        errors, warnings = s.validate()
        if errors:
            messagebox.showerror(APP_NAME, "Please fix the read setup:\n\n• " + "\n• ".join(errors))
            return
        if warnings and not messagebox.askokcancel(APP_NAME, "\n\n".join(warnings) + "\n\nStart anyway?"):
            return
        if self.last.get("queue", 0) > 0:
            ans = messagebox.askyesnocancel(
                APP_NAME, f"The reader still holds {self.last['queue']} dataset(s) from an earlier read.\n\n"
                          "Yes = save them to a separate folder first (recommended)\n"
                          "No = discard them\nCancel = don't start")
            if ans is None:
                return
            pre = "save" if ans else "discard"
        else:
            pre = "save"
        try:
            run = self._new_run(s)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Cannot create the results folder:\n{e}")
            return
        self.view_run = run
        self.selected = set()
        self._refresh_views(follow=True)
        self.running = True
        self.cancel_flag.clear()
        self.run_started_at = time.monotonic()
        self.progress.configure(maximum=s.expected_datasets(), value=0)
        self.run_progress = (0, s.expected_datasets())
        self._set_controls_state()
        self._set_dot("#f0a020")
        self.say(f"Starting: {s.description()}  →  {run.folder}")
        data_dir = self.data_dir.get()
        self.worker.submit("run", lambda: self._job_run(s, run, pre, data_dir))

    # Worker side of a run:
    #   1. empty the reader's memory first (saved to a '_recovered' folder, or discarded)
    #   2. send the settings, then read them back to confirm the reader accepted them exactly
    #      (for example it silently keeps only 8 of 9 wavelengths)
    #   3. '! read', then poll status / temperature / queue about every 1-2 s, transferring each
    #      dataset as soon as it is ready so kinetic data appears live
    #   4. stop when all expected datasets have arrived, the reader goes idle with nothing left,
    #      the operator cancels, or the reader reports a fatal error
    def _job_run(self, s, run, pre, data_dir):
        r = self.reader
        r.context = "run"
        try:
            q = r.queue()
            if q:
                if pre == "save":
                    recovered = self._new_run(ReadSettings(name=s.name), "_recovered", data_dir)
                    for _ in range(q):
                        self.post("dataset", id(recovered), r.transfer())
                    self.post("say", f"Saved {q} earlier dataset(s) to {recovered.folder}")
                else:
                    r.command("clear data")
                    self.post("say", f"Discarded {q} earlier dataset(s).")
            for c in s.commands():
                r.command(c)
            # verify the reader accepted the configuration exactly
            check = r.current_settings()
            problems = []
            if check.mode != s.mode:
                problems.append(f"mode is {check.mode}")
            if s.mode != "spectrum" and check.wavelengths != s.wavelengths:
                problems.append(f"wavelengths are {check.wavelengths}")
            if (check.first_col, check.last_col) != (s.first_col, s.last_col):
                problems.append(f"columns are {check.first_col}-{check.last_col}")
            if problems:
                raise RuntimeError("The reader did not accept the settings: " + "; ".join(problems))
            r.command("read")
            expected = s.expected_datasets()
            got, seen_active, t0 = 0, False, time.monotonic()
            while True:
                # Cancel: abort the measurement; datasets already transferred are kept
                if self.cancel_flag.is_set():
                    r.command("cancel")
                    self.post("run_end", id(run), "cancelled", got, expected)
                    return
                st = r.status()
                temp = r.temperature()
                q = r.queue()
                self.post("status", st, temp, q)
                if st[1].startswith("FATAL"):
                    raise RuntimeError(f"Reader reported {st[1]} {st[2]}".strip())
                if st[1] != "IDLE":
                    seen_active = True
                for _ in range(q):
                    self.post("dataset", id(run), r.transfer())
                    got += 1
                    self.post("progress", got, expected)
                if got >= expected:
                    break
                # safety net: the reader finished but delivered fewer datasets than expected. 'seen_active'
                # prevents stopping in the moment before the read has actually started.
                if st[1] == "IDLE" and q == 0 and (seen_active or time.monotonic() - t0 > 20):
                    if r.queue() == 0:
                        break
                time.sleep(0.5)
            self.post("run_end", id(run), "complete" if got >= expected else "ended early", got, expected)
        except Exception as e:
            self.post("run_end", id(run), "failed", None, None, e)
        finally:
            r.context = "user"

    def _cancel_run(self):
        if self.running and messagebox.askyesno(APP_NAME, "Cancel the read? Data already received is kept."):
            self.cancel_flag.set()
            self.say("Cancelling …")

    # Save whatever is in the reader's memory (e.g. after an interrupted session) to a 'retrieved' folder.
    def _retrieve_stored(self):
        run = self._new_run(ReadSettings(name="retrieved"))
        self.view_run = run
        self.selected = set()

        def job():
            r = self.reader
            r.context = "user"
            q = r.queue()
            for _ in range(q):
                self.post("dataset", id(run), r.transfer())
            self.post("say", f"Retrieved {q} dataset(s) → {run.folder}" if q else "The reader memory is empty.")
            self._job_poll()
        self.worker.submit("retrieve", job)

    def _clear_stored(self):
        if messagebox.askyesno(APP_NAME, "Permanently delete all datasets stored in the reader's memory?"):
            self._simple("clear data", "Reader memory cleared")

    # ---------------------------------------------------------------- event pump
    # GUI thread, every 100 ms: apply up to 200 queued events from the worker.
    def _process_events(self):
        try:
            for _ in range(200):
                self._handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    # Apply one worker event to the screen. Kinds: traffic, say, connected, connect_failed, disconnected,
    # status, form, dataset, progress, run_end, job_error.
    def _handle(self, kind, *a):
        if kind == "traffic":
            self._term_show(*a)
        elif kind == "say":
            self.say(a[0])
        elif kind == "connected":
            port, model, ver, settings, q = a
            self.connected = True
            self.connect_btn.configure(text="Disconnect", state="normal")
            self.ident_lbl.configure(text=f"{model} · firmware {ver} · {port}")
            self.reader_info = {"model": model, "firmware": ver, "port": port}
            self._set_dot("#2ca02c")
            self._apply_settings_to_form(settings)
            self.last["queue"] = q
            self.queue_lbl.configure(text=f"Reader memory: {q} dataset(s)")
            self._set_controls_state()
            self.say(f"Connected to {model} (firmware {ver}). Form shows the reader's current settings.")
            if q:
                messagebox.showinfo(APP_NAME, f"The reader holds {q} dataset(s) from an earlier read. "
                                              "Use 'Retrieve & save' to keep them.")
            self.worker.submit("poll", self._job_poll)
        elif kind == "connect_failed":
            self.connect_btn.configure(state="normal")
            self._set_dot("#d62728")
            self.status_lbl.configure(text="Not connected")
            self.say(f"Connection failed: {a[0]}")
            messagebox.showerror(APP_NAME, f"Could not talk to the reader:\n\n{a[0]}\n\n"
                                           "Check that it is switched on, the cable is plugged in and the "
                                           "right port is selected.")
        elif kind == "disconnected":
            self.connected = False
            self.connect_btn.configure(text="Connect")
            self.ident_lbl.configure(text="")
            self.reader_info = {}
            self.status_lbl.configure(text="Not connected")
            self._set_dot("#999999")
            self._set_controls_state()
            self.say("Disconnected.")
        elif kind == "status":
            (drawer, state, detail), (sp, chamber), q = a
            self.last.update(status=state, drawer=drawer, temp=(sp, chamber), queue=q)
            self.status_lbl.configure(text=f"{state.title()} {detail}".strip())
            self.drawer_lbl.configure(text=drawer.title())
            self.temp_lbl.configure(text=f"Chamber: {chamber:.1f} °C   ·   Setpoint: "
                                         f"{'off' if not sp else f'{sp:.1f} °C'}")
            self.queue_lbl.configure(text=f"Reader memory: {q} dataset(s)")
            if self.running:
                got, expected = self.run_progress
                el = time.monotonic() - (self.run_started_at or time.monotonic())
                self.progress_lbl.configure(text=f"{state.title()} {detail} · received {got} of {expected} · "
                                                 f"elapsed {int(el // 60):02d}:{int(el % 60):02d}")
            else:
                self._set_dot("#d62728" if state.startswith("FATAL") else
                              "#2ca02c" if state == "IDLE" else "#f0a020")
        elif kind == "form":
            self._apply_settings_to_form(a[0])
        elif kind == "dataset":
            run = self.runs.get(a[0])
            if run:
                try:
                    run.add(a[1])
                except OSError as e:
                    self.say(f"Could not save data: {e}")
                if run is self.view_run:
                    self._refresh_views(follow=True)
        elif kind == "progress":
            self.run_progress = a
            self.progress.configure(value=a[0])
        elif kind == "run_end":
            run_id, outcome, got, expected = a[:4]
            err = a[4] if len(a) > 4 else None
            self.running = False
            self._set_controls_state()
            self._set_dot("#2ca02c")
            run = self.runs.get(run_id)
            folder = run.folder if run else ""
            if outcome == "failed":
                self._set_dot("#d62728")
                self.progress_lbl.configure(text="Failed")
                self.say(f"Read failed: {err}")
                messagebox.showerror(APP_NAME, f"The read failed:\n\n{err}\n\nAny data received is in:\n{folder}")
            else:
                self.progress_lbl.configure(text=f"{outcome.capitalize()}: {got} of {expected} dataset(s) received")
                self.say(f"Read {outcome}: {got}/{expected} dataset(s) saved to {folder}")
            self.worker.submit("poll", self._job_poll)
        elif kind == "job_error":
            name, e = a
            if name == "poll" and isinstance(e, (ReaderTimeout, OSError)):
                self.say(f"Lost contact with the reader: {e}")
                self._set_dot("#d62728")
            else:
                self.say(f"{name}: {e}")
                if name not in ("poll",):
                    messagebox.showerror(APP_NAME, str(e))

    # ---------------------------------------------------------------- terminal
    # Send the text exactly as typed plus the chosen line ending. Without a leading '!'
    # the reader echoes the command back.
    def _term_send(self):
        text = self.term_entry.get()
        if not self.connected:
            messagebox.showinfo(APP_NAME, "Connect to the reader first.")
            return
        if self.running and not messagebox.askokcancel(APP_NAME, "A read is running. Send this command anyway?"):
            return
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.hist_pos = len(self.history)
        eol = {"LF": "\n", "CR": "\r", "CR+LF": "\r\n", "None": ""}[self.eol_var.get()]
        self.term_entry.delete(0, "end")
        data = (text + eol).encode("ascii", "replace")
        self.worker.submit("terminal", lambda: self.reader.raw(data))

    # Up/Down arrow history; returning 'break' stops Tk's default key handling.
    def _term_hist(self, step):
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        self.term_entry.delete(0, "end")
        if self.hist_pos < len(self.history):
            self.term_entry.insert(0, self.history[self.hist_pos])
        return "break"

    # Append traffic to the terminal. Background traffic (polling, runs) is hidden unless that box is ticked.
    def _term_show(self, direction, data, context):
        background = context in ("poll", "run", "stray")
        if background and not self.poll_var.get():
            return
        if direction == ">>" and not self.echo_var.get():
            return
        if self.ts_var.get():
            self.term.insert("end", f"[{datetime.now():%H:%M:%S.%f}"[:-3] + "] ", "ts")
        if self.hex_var.get():
            text = " ".join(f"{b:02X}" for b in data) + "\n"
        else:
            text = data.decode("ascii", "replace").replace("\r\n", "\n").replace("\r", "\n")
            if not text.endswith("\n"):
                text += "\n"
        tag = "bg" if background else ("tx" if direction == ">>" else "rx")
        self.term.insert("end", text, tag)
        self.term.see("end")

    def _save_term(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="versamax_terminal.txt")
        if path:
            with open(path, "w") as f:
                f.write(self.term.get("1.0", "end"))

    # ---------------------------------------------------------------- views
    def _points(self):
        return self.view_run.points() if self.view_run else []

    # Rebuild the 'Showing:' list and redraw plate and graph. follow=True jumps to the newest dataset
    # (used while a run is in progress).
    def _refresh_views(self, follow=False):
        pts = self._points()
        labels = [f"Read {p['read']} · {p['wl']} nm · t = {p['time']:g} s · {p['temp']:.1f} °C" for p in pts]
        cur = self.point_box.current()
        self.point_box["values"] = labels
        if labels:
            self.point_box.current(len(labels) - 1 if (follow or cur < 0 or cur >= len(labels)) else cur)
        else:
            self.point_var.set("")
        run = self.view_run
        if run:
            inst = f"{run.instrument_text}   ·   " if run.instrument_text else ""
            self.run_lbl.configure(text=f"{run.settings.description()}   ·   {len(run.datasets)} dataset(s)   ·   "
                                        f"{inst}saved in {run.folder}")
        wls = sorted({p["wl"] for p in pts})
        self.graph_wl_box["values"] = [str(w) for w in wls]
        if wls and self.graph_wl.get() not in [str(w) for w in wls]:
            self.graph_wl.set(str(wls[0]))
        self._draw_plate()
        self._draw_graph()

    def _current_point(self):
        pts = self._points()
        i = self.point_box.current()
        return pts[i] if pts and 0 <= i < len(pts) else None

    # Colour each well by its OD relative to the min / max of the dataset shown; wells not read stay grey;
    # selected wells get an orange border and bold text.
    def _draw_plate(self):
        p = self._current_point()
        vals = p["values"] if p else {}
        known = [v for v in vals.values() if v is not None]
        lo, hi = (min(known), max(known)) if known else (0, 1)
        for w, lbl in self.cells.items():
            v = vals.get(w)
            bg, fg = heat_color(v, lo, hi) if w in vals else ("#eeeeee", "#999999")
            selected = w in self.selected
            lbl.configure(text=("" if w not in vals else "—" if v is None else f"{v:.4f}"), bg=bg, fg=fg,
                          font=("Menlo", 12, "bold") if selected else ("Menlo", 12))
            self.cell_borders[w].configure(bg="#ff7f0e" if selected else self.grid_bg)
        self.scale_lbl.configure(text=f"Colour scale: {lo:.4f} (light) → {hi:.4f} (dark) OD" if known else "")

    def _toggle_well(self, w):
        self.selected.symmetric_difference_update({w})
        self._draw_plate()
        self._draw_graph()

    def _select_all(self):
        self.selected = set(self.view_run.wells_read()) if self.view_run else set()
        self._draw_plate()
        self._draw_graph()

    def _clear_selection(self):
        self.selected = set()
        self._draw_plate()
        self._draw_graph()

    # Tab-separated text pastes straight into Excel as an 8 x 12 grid.
    def _copy_plate(self):
        p = self._current_point()
        if not p:
            return
        lines = [f"Read {p['read']}\t{p['wl']} nm\tt={p['time']} s\t{p['temp']} C",
                 "\t" + "\t".join(str(c) for c in COLS)]
        for r in ROWS:
            row = [p["values"].get(f"{r}{c}") for c in COLS]
            lines.append(r + "\t" + "\t".join("" if v is None else f"{v:.4f}" for v in row))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.say("Plate copied – paste into Excel.")

    def _open_folder(self):
        folder = self.view_run.folder if self.view_run else self.data_dir.get()
        os.makedirs(folder, exist_ok=True)
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])

    # Rebuild a past run from its raw_transfer.txt (the CSVs are derived from it, so the raw file is the
    # source of truth).
    def _open_run(self):
        folder = filedialog.askdirectory(initialdir=self.data_dir.get(), title="Choose a results folder")
        if not folder:
            return
        try:
            with open(os.path.join(folder, "settings.json")) as f:
                d = json.load(f)
            s = ReadSettings(**{k: v for k, v in d.items() if k in ReadSettings.__dataclass_fields__})
            # bypass __init__ so the folder's existing settings.json is not rewritten
            run = RunData.__new__(RunData)
            run.settings, run.folder, run.datasets = s, folder, []
            run.instrument = d.get("instrument") or {}   # absent in runs saved before it was recorded
            with open(os.path.join(folder, "raw_transfer.txt")) as f:
                chunks = re.split(r"^### dataset.*$", f.read(), flags=re.M)
            run.datasets = [parse_dataset(c.strip()) for c in chunks if c.strip()]
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not open that run:\n{e}")
            return
        self.runs[id(run)] = run
        self.view_run = run
        self.selected = set()
        self._refresh_views(follow=True)
        self.say(f"Opened {folder}")

    # Simple line chart drawn on a Tk Canvas (no plotting library needed). Kinetic: OD vs time at the chosen
    # wavelength. Endpoint / spectrum: OD vs wavelength. Plots the selected wells, or every well read if none
    # are selected; a legend is shown for up to 12 wells.
    def _draw_graph(self):
        c = self.canvas
        c.delete("all")
        W, H = max(c.winfo_width(), 200), max(c.winfo_height(), 150)
        pts = self._points()
        if not pts:
            c.create_text(W / 2, H / 2, text="No data yet", fill="#888888", font=("Helvetica", 14))
            return
        kinetic = self.view_run.settings.mode == "kinetic"
        wells = [w for w in (self.view_run.wells_read()) if w in self.selected] or self.view_run.wells_read()
        series = {}
        if kinetic:
            wl = int(self.graph_wl.get() or pts[0]["wl"])
            for p in pts:
                if p["wl"] == wl:
                    for w in wells:
                        if p["values"].get(w) is not None:
                            series.setdefault(w, []).append((p["time"], p["values"][w]))
            xlabel = "Time (s)"
            self.graph_mode_lbl.configure(text="Kinetic: OD vs time")
            self.graph_wl_box.configure(state="readonly")
        else:
            for p in pts:
                for w in wells:
                    if p["values"].get(w) is not None:
                        series.setdefault(w, []).append((p["wl"], p["values"][w]))
            xlabel = "Wavelength (nm)"
            self.graph_mode_lbl.configure(text="OD vs wavelength")
            self.graph_wl_box.configure(state="disabled")
        for w in series:
            series[w].sort()
        allp = [xy for s in series.values() for xy in s]
        if not allp:
            return
        x0, x1 = min(x for x, _ in allp), max(x for x, _ in allp)
        y0, y1 = min(y for _, y in allp), max(y for _, y in allp)
        if x1 == x0:
            x0, x1 = x0 - 1, x1 + 1
        pad = (y1 - y0) * 0.08 or 0.01
        y0, y1 = y0 - pad, y1 + pad
        legend = len(series) <= 12
        # plot margins in pixels, then data -> pixel transforms
        L, R, T, B = 70, (130 if legend else 20), 20, 50
        sx = lambda x: L + (x - x0) / (x1 - x0) * (W - L - R)
        sy = lambda y: H - B - (y - y0) / (y1 - y0) * (H - T - B)
        c.create_rectangle(L, T, W - R, H - B, outline="#bbbbbb")
        for i in range(6):
            xv = x0 + (x1 - x0) * i / 5
            yv = y0 + (y1 - y0) * i / 5
            c.create_line(sx(xv), H - B, sx(xv), H - B + 5, fill="#666666")
            c.create_text(sx(xv), H - B + 16, text=f"{xv:.0f}" if x1 - x0 > 10 else f"{xv:.1f}", fill="#333333")
            c.create_line(L - 5, sy(yv), W - R, sy(yv), fill="#eeeeee")
            c.create_text(L - 8, sy(yv), text=f"{yv:.3f}", anchor="e", fill="#333333")
        c.create_text((L + W - R) / 2, H - 12, text=xlabel, fill="#333333")
        c.create_text(14, (T + H - B) / 2, text="OD", angle=90, fill="#333333")
        for i, (w, s) in enumerate(series.items()):
            col = PALETTE[i % len(PALETTE)] if legend else "#4a7fd4"
            coords = [v for x, y in s for v in (sx(x), sy(y))]
            if len(coords) >= 4:
                c.create_line(*coords, fill=col, width=2 if legend else 1)
            for x, y in s:
                c.create_oval(sx(x) - 2.5, sy(y) - 2.5, sx(x) + 2.5, sy(y) + 2.5, fill=col, outline=col)
            if legend:
                ly = T + 8 + i * 18
                c.create_line(W - R + 12, ly, W - R + 32, ly, fill=col, width=3)
                c.create_text(W - R + 38, ly, text=w, anchor="w", fill="#333333")
        if not legend:
            c.create_text(W - 10, T + 8, anchor="ne", fill="#666666",
                          text=f"{len(series)} wells – select up to 12 on the Plate tab for a legend")

    # ---------------------------------------------------------------- shutdown
    # Closing the window: confirm if a read is running, cancel it, release the serial port, stop the worker.
    def _on_close(self):
        if self.running and not messagebox.askyesno(APP_NAME, "A read is running. Cancel it and quit?"):
            return
        if self.running:
            self.cancel_flag.set()
            time.sleep(0.2)
        if self.connected:
            self.worker.submit("disconnect", self._job_disconnect)
        self.worker.submit("stop", None)
        self.root.after(400, self.root.destroy)


# Entry point: parse command-line options and start the GUI.
def main():
    ap = argparse.ArgumentParser(description="VersaMax microplate reader control")
    ap.add_argument("--simulate", action="store_true", help="use a simulated reader (no hardware needed)")
    ap.add_argument("--port", help="serial port to connect to on start, e.g. /dev/cu.usbserial-1410")
    args = ap.parse_args()
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
