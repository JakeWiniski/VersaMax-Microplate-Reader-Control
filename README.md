# VersaMax Control (unofficial)

A single-file Python application for running a **Molecular Devices VERSAmax Tunable Microplate Reader** from a Mac (or any computer with Python), with a full graphical interface.

The operator sets up and runs every measurement from a window, sees the results as a plate heat map and graphs, and every run is saved automatically as CSV files ready for Excel, R, Python or Prism.

- One file, one dependency (`pyserial`). No installer, no licence server.
- Endpoint, kinetic and spectrum-scan reads at 1–8 wavelengths (340–850 nm), on the whole plate or selected columns.
- Live data during kinetic runs, automatic saving, and a built-in terminal for sending commands directly to the reader.
- A built-in simulator, so you can learn and test the software without the instrument.

> **Status:** tested end-to-end on a VersaMax with firmware **3.13 (31 Oct 1998)**, connected through a CH340 USB-serial adapter, on an Intel MacBook Pro running macOS 26 with Python 3.13. See [Compatibility](#compatibility).

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Connecting the reader](#connecting-the-reader)
- [Installation](#installation)
- [Starting the program](#starting-the-program)
- [Using the program](#using-the-program)
- [Output files](#output-files)
- [Simulator](#simulator)
- [Troubleshooting](#troubleshooting)
- [Compatibility](#compatibility)
- [Known limitations](#known-limitations)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Features

| Area | What you can do |
|---|---|
| **Instrument** | Open and close the drawer, turn the incubator on or off and set its temperature, shake the plate on demand. Drawer state, reader state and chamber temperature are shown live. |
| **Read modes** | **Endpoint** (one read), **Kinetic** (repeated reads at a set interval) and **Spectrum scan** (start / stop / step in nm). |
| **Wavelengths** | 1–8 wavelengths per read, any value from 340 to 850 nm in 1 nm steps. |
| **Plate area** | The whole plate or any contiguous range of columns (strips) from 1 to 12. |
| **AutoMix** | Shake before the first read, or before the first read and between kinetic reads, with set durations. |
| **Calibration** | Air calibration once per run, before every read, or off. Optional speed read. |
| **Protocols** | Save and load complete read setups as `.json` files, or pull the reader's current settings into the form. |
| **Results** | 8 × 12 heat-map plate view, OD-vs-time and OD-vs-wavelength graphs for selected wells, one-click copy of a plate to paste into Excel. |
| **Data safety** | Each run is saved to its own time-stamped folder as data arrives, so an interrupted run keeps everything already measured. Data left in the reader's memory from an earlier session can be recovered. |
| **Traceability** | Every results file records the reader model and firmware version that produced it. |
| **Terminal** | A built-in console (line ending, local echo, timestamps, hex view, history) for sending commands directly to the reader. |
| **Safety checks** | Inputs are validated before anything is sent, settings are read back from the reader before each run to confirm they were accepted, and reader errors are shown in plain English. |

---

## Requirements

**Software**

- Python **3.8 or newer** with **Tkinter** (included with the python.org and Anaconda installers for macOS).
- **pyserial**: `python3 -m pip install pyserial`

**Hardware**

- A VersaMax Tunable Microplate Reader.
- A serial cable from the reader's **8-pin mini-DIN** RS-232 port to a **DB9** connector.
- A **USB-to-serial (DB9) adapter**. Adapters based on the **WCH CH340/CH341** chip (as supplied with many used readers) and **FTDI** chips work with macOS's built-in drivers. Some Prolific (PL2303) adapters need a vendor driver, and the CH340K variant is not supported on macOS.

---

## Connecting the reader

1. Connect the reader's rear **Computer (RS-232)** port to the USB adapter using the mini-DIN-to-DB9 cable, and plug the adapter into the computer.
2. Switch the reader on and let it finish its power-up self-test and calibration.
3. The adapter appears as a serial port, typically `/dev/cu.usbserial-XXXX` on macOS (for example `/dev/cu.usbserial-1410`). You can list ports with:

   ```bash
   python3 -m serial.tools.list_ports
   ```

No driver installation is needed: macOS (10.14 and later) includes a driver for this chip. If an old third-party CH34x driver was installed in the past, remove it, because it can conflict with Apple's.

---

## Installation

1. Download this repository: on GitHub, use **Code → Download ZIP** and unzip it, or clone it with the URL shown under **Code**.
2. Install the one dependency:

   ```bash
   python3 -m pip install pyserial
   ```

That is all. The application is the single file `versamax_reader.py`; keep `Launch VersaMax Reader.command` in the same folder if you want the double-click launcher.

---

## Starting the program

**Double-click** `Launch VersaMax Reader.command` in Finder. The first time, macOS may ask you to confirm opening a downloaded script: right-click it, choose **Open**, then **Open** again. If it does not run after cloning, make it executable:

```bash
chmod +x "Launch VersaMax Reader.command"
```

**From a terminal:**

```bash
python3 versamax_reader.py
```

Command-line options:

| Option | Effect |
|---|---|
| `--simulate` | Start with the built-in simulated reader (no hardware needed) and connect to it automatically. |
| `--port /dev/cu.usbserial-1410` | Connect to this port automatically on start. |

The launcher uses Anaconda's Python (`/opt/anaconda3/bin/python3`) when it is present, and otherwise the first `python3` on your `PATH`. Edit the `PY=` line in `Launch VersaMax Reader.command` if your Python lives elsewhere.

---

## Using the program

The window has a control column on the left and four tabs on the right: **Plate**, **Graph**, **Terminal** and **Log**. A status bar runs along the bottom.

### 1. Connect

1. Choose the reader's port in **Port** (USB-serial adapters are listed first; press **↻** to refresh) and press **Connect**.
2. The top-right corner shows the reader's model, firmware and port, and the status light turns **green**. The *Read setup* form is filled in with the reader's current settings.
3. If the reader is still holding data from an earlier session, you are told so. Use **Retrieve & save** to keep it.

Status light: grey = not connected, green = idle, orange = busy or reading, red = error.

### 2. Instrument controls

| Control | What it does |
|---|---|
| **Drawer: Open / Close** | Moves the plate drawer. Takes about 2 s. |
| **Incubator: On + setpoint + Apply** | Sets the chamber temperature (15–45 °C). Untick **On** and press **Apply** to turn heating off. The reader can only heat, and is accurate only when the setpoint is at least 4 °C above room temperature. Allow 15–30 minutes to reach temperature. |
| **Shake: Shake now / Stop** | Starts and stops shaking immediately. |

### 3. Read setup

| Setting | Notes |
|---|---|
| **Name** | Becomes part of the results folder name. |
| **Mode** | *Endpoint*, *Kinetic* or *Spectrum scan*. The fields below change to match the mode. |
| **Number of wavelengths + boxes** | Endpoint and kinetic: 1–8 wavelengths, 340–850 nm, all different. |
| **Interval (s), Number of reads** | Kinetic only. The total run time is shown underneath. The reader needs roughly 9 s per wavelength to read a full plate (about 2 s for a single column); if the interval is shorter, the app warns you, the reader lengthens the interval automatically, and the actual read times are recorded in the data. |
| **Start, Stop, Step (nm)** | Spectrum only. The number of wavelengths that will be read is shown underneath. |
| **Columns** | First and last column to read (contiguous). Reading fewer columns makes reads faster. |
| **AutoMix** | *Off*, *Before first read*, or *Before first read + between reads* (kinetic), with durations of 1–999 s. |
| **Calibration** | *Once per run (first read)* (the default), *Before every read*, or *Off*. |
| **Speed read** | Uses the air calibration stored in the reader instead of calibrating each read. |
| **Load protocol… / Save protocol…** | Save or restore the whole setup as a `.json` file. |
| **Get from reader** | Fill the form with the settings currently stored in the reader. |

### 4. Run

1. Choose where results go with **Save to: Change…** (default `~/Documents/VersaMax Data`).
2. Press **▶ Start read**. The program checks the setup and warns about anything unusual. If the reader still holds data from an earlier read, it offers to save that to a separate `_recovered` folder first.
3. The settings are sent to the reader, read back to confirm, and the read starts. Progress shows the reader's state (air calibration, measuring), datasets received and elapsed time. A full-plate read at one wavelength takes about 10 s. Kinetic and spectrum data appear in the Plate and Graph tabs as each read completes.
4. **■ Cancel** stops the read. Data already received is kept; the measurement in progress at that moment is discarded by the reader.

### 5. Results

**Plate tab**

- **Showing:** choose which read, wavelength and time point to display.
- Each well shows its OD, coloured from light (lowest) to dark blue (highest) for that dataset. Wells that were not read are grey.
- **Click wells** to select them for the Graph tab. Selected wells get an **orange border** and bold text. Use **Select all** / **Clear selection** to change many at once.
- **Copy plate (for Excel)** copies the displayed plate as an 8 × 12 grid that pastes directly into a spreadsheet.
- **Open results folder** opens the run's folder in Finder. **Open previous run…** reloads any earlier results folder for viewing.

**Graph tab**

- Kinetic runs: OD against time for the chosen wavelength.
- Endpoint and spectrum runs: OD against wavelength.
- Plots the selected wells, or every well read if none are selected. A colour legend is shown for up to 12 wells.

### 6. Terminal tab

Type a reader command, for example `! status`, `! help mode` or `! temp 37`, and press Return. Options:

- **Line ending:** LF (default), CR, CR+LF or None.
- **Local echo**, **Timestamps**, **Hex view**.
- **Show background traffic:** also shows the status polling and the commands sent by the buttons and by runs.
- **↑ / ↓** recall earlier commands. **Save log…** writes the terminal contents to a text file.

Commands sent without the leading `!` are echoed back by the reader.

> ⚠️ **Caution: service commands.** The Terminal sends whatever you type straight to the reader, and the reader also accepts factory service and calibration commands. Some of these, for example `init`, `air_cal`, `set_f900`, `set_f1000`, `caldac`, `calign` and `set_calflag`, can overwrite the calibration stored in the reader. The reader could then measure inaccurately until it is properly re-calibrated. The buttons in this program never send these commands. In the Terminal, use only the commands described in this README and the ones you understand; if unsure, check first with `! help <command>`.

### 7. Log tab and reader memory

The **Log** tab keeps a time-stamped record of everything the program did in the session. In the Run panel, **Reader memory** shows how many finished datasets are waiting in the reader. **Retrieve & save** downloads them into a `retrieved` folder; **Clear memory** deletes them, after asking you to confirm.

---

## Output files

Each run creates a folder named `YYYY-MM-DD_HHMMSS_<Name>` inside the chosen save folder:

| File | Contents |
|---|---|
| `settings.json` | The complete read setup, plus an `instrument` section (model, firmware, port). |
| `data_long.csv` | One row per well, read and wavelength. Best for analysis software. |
| `plate_layout.csv` | 8 × 12 grids laid out like the plate, one per read and wavelength. Easiest to read in Excel. |
| `kinetic_<wl>nm.csv` | Kinetic runs only: one row per time point, one column per well, one file per wavelength. |
| `spectrum.csv` | Spectrum runs only: one row per wavelength, one column per well. |
| `raw_transfer.txt` | Every dataset exactly as the reader sent it. This is the source of truth; the CSVs are generated from it. |

Columns of `data_long.csv`:

```
Read, Time_s, Temperature_C, Wavelength_nm, Well, Row, Column, OD, Instrument, Firmware
```

- `Time_s` is measured from the first read of the run, using the reader's own clock.
- `Temperature_C` is the chamber temperature at the time of each read.
- `Instrument` and `Firmware` identify the reader. They are the last two columns of every CSV, so the earlier columns keep fixed positions.

Files are rewritten each time a dataset arrives, so a run that is interrupted (cancelled, cable pulled, computer asleep) still leaves everything measured up to that point.

---

## Simulator

```bash
python3 versamax_reader.py --simulate
```

The simulator behaves like a real VersaMax (its replies are modelled on recordings from the actual instrument). Use it to train operators or try out protocols without the instrument. Reads are faster than on the real instrument and the OD values are synthetic. You can also choose **SIMULATOR (no reader)** from the port list at any time.

---

## Troubleshooting

| Problem | What to check |
|---|---|
| No `usbserial` port in the list | Adapter unplugged, or a charge-only USB cable/hub. Check **System Information → USB**. Remove any old third-party CH34x driver. CH340K adapters do not work on macOS; use an FTDI-based adapter instead. |
| "Could not talk to the reader" | Reader switched off or still starting up; wrong port selected; the port is already in use (for example by a second copy of this program). |
| "Lost contact with the reader" | Cable unplugged or the reader was switched off. Reconnect, then use **Retrieve & save** to collect any data the reader finished in the meantime. |
| Kinetic reads further apart than requested | The interval is shorter than the reader needs for that many columns and wavelengths; it stretches the interval automatically. The actual times are in the data. |
| Incubator does not reach the setpoint | The reader only heats; the setpoint must be above room temperature (ideally 4 °C or more). 45 °C needs a room above 20 °C. |
| "The reader did not accept the settings" | The reader's read-back differed from what was sent. Check the values, or try the command in the Terminal tab to see the reader's response. |
| A Terminal command returns `FAIL` with a code | The code is the reader's error number. Buttons and runs show errors in plain English automatically; in the Terminal, `! error list` shows what each code means and `! error last` shows the most recent error. |
| Need to see exactly what is sent | Terminal tab → tick **Show background traffic** (and **Timestamps**). |

---

## Compatibility

- **Tested:** VersaMax Tunable Microplate Reader, firmware 3.13 (31 Oct 1998); CH340 USB-serial adapter (USB ID 1A86:7523); macOS 26 (Intel); Python 3.13 with Tk 8.6 and pyserial 3.5.
- **Other VersaMax units:** expected to work, since nothing in the program is specific to one unit and it reads the reader's model, firmware and settings on every connection. Units with different firmware may differ in details (supported modes, maximum wavelengths, reply format). A quick check on a new unit: connect and look at the model and firmware shown top-right, run `! help mode` and `! help wavelength` in the Terminal tab, then do one endpoint read on an empty plate.
- **Baud rate:** fixed at 9600, the factory setting. A reader whose baud rate has been changed will not connect.
- **VersaMax PLUS** (with cuvette port): not tested; cuvette functions are not supported.
- **Other operating systems:** the code is plain Python and Tkinter and should also run on Windows and Linux (choose the COM or `/dev/tty…` port), but only macOS has been tested.

---

## Known limitations

- **Shaking between kinetic reads** cannot be verified electronically: the reader reports no shaking state and keeps the same timing whether it is on or off. Watch the plate during a kinetic run with *Before first read + between reads* selected to confirm it on your unit.
- The **save folder** resets to the default each time the program starts.
- Results folder and file names are generated automatically (only the run *Name* is chosen by the operator).
- No built-in data reduction (blank subtraction, standard curves, Vmax): use the CSV files in your analysis software of choice.
- Only 96-well plates are supported (the VersaMax is a 96-well reader).

---

## Disclaimer

**Unofficial software.** This is an independent project. It is not affiliated with, endorsed by, sponsored by or supported by Molecular Devices, LLC or its predecessors. Product names are used only to describe which instrument the software works with. VERSAmax is a trademark of its respective owner.

**Non-commercial.** The project is shared free of charge for research and educational purposes. It is not sold, and no paid support, service or warranty is offered.

**Independent implementation.** The program contains no Molecular Devices software or code. It communicates with the reader only through the reader's standard RS-232 serial port, using the plain-text commands the instrument itself documents through its built-in `help` command.

**No warranty; use at your own risk.** The software is provided as is, without warranty of any kind (see the MIT License). It has not been validated for regulated (GLP/GMP or clinical) use. Verify results against a known reference before relying on them.

## License

Released under the [MIT License](LICENSE).
