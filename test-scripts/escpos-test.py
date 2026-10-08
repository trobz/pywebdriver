#!/usr/bin/env python3
# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""
Standalone ESC/POS printer connection test script.

Checks whether pywebdriver/plugins/escpos_driver.py would consider the
configured printer "connected", without starting the pywebdriver/Flask
server. Supports the same three device types as the real driver: usb,
serial, win32 — using the exact same underlying library calls.

Requires the same runtime dependencies as pywebdriver's ESC/POS support:
    pip install git+https://github.com/akretion/py-xml-escpos.git@py3
(pulls in python-escpos==3.0a8, which provides escpos.printer), plus
pyusb (usb) or pywin32 (win32, Windows only).

Usage:
    python3 escpos-test.py

Configuration (env vars, or a .env file next to this script):
    ESCPOS_DEVICE_TYPE          usb | serial | win32   (default: usb)

    # serial only:
    ESCPOS_SERIAL_DEVICE_NAME   e.g. /dev/ttyS1 (Linux) or COM1 (Windows)
    ESCPOS_SERIAL_BAUDRATE      default: 9600
    ESCPOS_SERIAL_BYTESIZE      default: 8
    ESCPOS_SERIAL_TIMEOUT       default: 1

    # win32 only:
    ESCPOS_PRINTER_NAME         exact Windows printer name
    ESCPOS_PRINTER_NAMES        comma-separated fnmatch patterns, tried in
                                 order (same fallback behavior as the driver)
"""
import fnmatch
import os
import sys


def _load_dotenv(path=None):
    """Minimal .env loader (no python-dotenv dependency needed for a
    single standalone test script). Existing environment variables are
    not overridden.
    """
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# Same default as config.ini.tmpl's [escpos_driver] section.
DEVICE_TYPE = os.environ.get("ESCPOS_DEVICE_TYPE", "usb").strip().lower()
print("ESCPOS_DEVICE_TYPE = %s" % DEVICE_TYPE)

# Same list as escpos_driver.py's SUPPORTED_DEVICES.
SUPPORTED_DEVICES = [
    # Don't add 2 entries with the same vendor and product IDs
    # Epson TM-T70, TM-T70II and Epson TM-P20 have the same vendor/product IDs
    {"vendor": 0x04B8, "product": 0x0E03, "name": "Epson TM-T20"},
    {"vendor": 0x04B8, "product": 0x0202, "name": "Epson TM-T70"},
    {"vendor": 0x04B8, "product": 0x0E15, "name": "Epson TM-T20II"},
    {"vendor": 0x04B8, "product": 0x0E28, "name": "Epson TM-T20III"},
    {"vendor": 0x0525, "product": 0xA700, "name": "Netchip POS Printer Aures ODP 333"},
]


def check_usb():
    import usb.core
    from escpos.printer import Usb

    connected = []
    for device in SUPPORTED_DEVICES:
        found = usb.core.find(idVendor=device["vendor"], idProduct=device["product"])
        if found is not None:
            connected.append(device)
            print(
                "Found supported USB printer: %s (vendor=0x%04x product=0x%04x)"
                % (device["name"], device["vendor"], device["product"])
            )
    if not connected:
        print("No supported USB printer found. Supported vendor/product IDs:")
        for device in SUPPORTED_DEVICES:
            print(
                "  - %s (vendor=0x%04x product=0x%04x)"
                % (device["name"], device["vendor"], device["product"])
            )
        return False

    printer = connected[0]
    print("Opening USB connection to %s ..." % printer["name"])
    # Usb.__init__() opens the device itself (calls self.open(usb_args)),
    # same as escpos_driver.py's POSDriver.__init__(idVendor, idProduct, **kwargs).
    printer_conn = Usb(
        printer["vendor"],
        printer["product"],
        in_ep=printer.get("in_ep", 0x82),
        out_ep=printer.get("out_ep", 0x01),
        timeout=0,
    )
    try:
        online = printer_conn.is_online()
        print("is_online() = %s" % online)
        return bool(online)
    finally:
        printer_conn.close()


def check_serial():
    from escpos.printer import Serial

    devfile = os.environ.get("ESCPOS_SERIAL_DEVICE_NAME")
    baudrate = int(os.environ.get("ESCPOS_SERIAL_BAUDRATE", "9600"))
    bytesize = int(os.environ.get("ESCPOS_SERIAL_BYTESIZE", "8"))
    timeout = int(os.environ.get("ESCPOS_SERIAL_TIMEOUT", "1"))
    if not devfile:
        print(
            "ESCPOS_SERIAL_DEVICE_NAME is not set "
            "(e.g. /dev/ttyS1 on Linux or COM1 on Windows)"
        )
        return False
    print(
        "Opening serial connection to %s (baudrate=%s bytesize=%s timeout=%s) ..."
        % (devfile, baudrate, bytesize, timeout)
    )
    # Serial.__init__() opens the port itself (calls self.open()), same as
    # escpos_driver.py's POSDriver.__init__(**kwargs).
    printer_conn = Serial(
        devfile=devfile, baudrate=baudrate, bytesize=bytesize, timeout=timeout
    )
    try:
        # Like escpos_driver.py's get_status(): serial has no real liveness
        # check beyond the port opening without raising.
        print("Serial port opened successfully.")
        return True
    finally:
        printer_conn.close()


def check_win32():
    import win32print
    from escpos.printer import Win32Raw

    # Same classification as escpos_driver.py's PRINTER_STATUS_DICT.
    status_titles = {
        0: "AVAILABLE",
        win32print.PRINTER_STATUS_PAUSED: "PAUSED",
        win32print.PRINTER_STATUS_ERROR: "ERROR",
        win32print.PRINTER_STATUS_PENDING_DELETION: "PENDING_DELETION",
        win32print.PRINTER_STATUS_PAPER_JAM: "PAPER_JAM",
        win32print.PRINTER_STATUS_PAPER_OUT: "PAPER_OUT",
        win32print.PRINTER_STATUS_MANUAL_FEED: "MANUAL_FEED",
        win32print.PRINTER_STATUS_PAPER_PROBLEM: "PAPER_PROBLEM",
        win32print.PRINTER_STATUS_OFFLINE: "OFFLINE",
        win32print.PRINTER_STATUS_IO_ACTIVE: "IO_ACTIVE",
        win32print.PRINTER_STATUS_BUSY: "BUSY",
        win32print.PRINTER_STATUS_PRINTING: "PRINTING",
        win32print.PRINTER_STATUS_OUTPUT_BIN_FULL: "OUTPUT_BIN_FULL",
        win32print.PRINTER_STATUS_NOT_AVAILABLE: "NOT_AVAILABLE",
        win32print.PRINTER_STATUS_WAITING: "WAITING",
        win32print.PRINTER_STATUS_PROCESSING: "PROCESSING",
        win32print.PRINTER_STATUS_INITIALIZING: "INITIALIZING",
        win32print.PRINTER_STATUS_WARMING_UP: "WARMING_UP",
        win32print.PRINTER_STATUS_TONER_LOW: "TONER_LOW",
        win32print.PRINTER_STATUS_NO_TONER: "NO_TONER",
        win32print.PRINTER_STATUS_PAGE_PUNT: "PAGE_PUNT",
        win32print.PRINTER_STATUS_USER_INTERVENTION: "USER_INTERVENTION",
        win32print.PRINTER_STATUS_OUT_OF_MEMORY: "OUT_OF_MEMORY",
        win32print.PRINTER_STATUS_DOOR_OPEN: "DOOR_OPEN",
        win32print.PRINTER_STATUS_SERVER_UNKNOWN: "SERVER_UNKNOWN",
        win32print.PRINTER_STATUS_POWER_SAVE: "POWER_SAVE",
    }
    # usable=False titles in escpos_driver.py's PRINTER_STATUS_DICT.
    unusable_titles = {
        "PAUSED",
        "ERROR",
        "PENDING_DELETION",
        "PAPER_JAM",
        "PAPER_OUT",
        "MANUAL_FEED",
        "PAPER_PROBLEM",
        "OFFLINE",
        "IO_ACTIVE",
        "BUSY",
        "OUTPUT_BIN_FULL",
        "NOT_AVAILABLE",
        "NO_TONER",
        "PAGE_PUNT",
        "USER_INTERVENTION",
        "OUT_OF_MEMORY",
        "SERVER_UNKNOWN",
    }

    printer_name = os.environ.get("ESCPOS_PRINTER_NAME")
    if not printer_name:
        patterns = [
            p.strip()
            for p in os.environ.get("ESCPOS_PRINTER_NAMES", "").split(",")
            if p.strip()
        ]
        if not patterns:
            print(
                "Set ESCPOS_PRINTER_NAME (exact name) or ESCPOS_PRINTER_NAMES "
                "(comma-separated fnmatch patterns) to pick a printer."
            )
            return False
        printers_list = win32print.EnumPrinters(win32print.PRINTER_ENUM_NAME, None, 2)
        printers_dict = {item["pPrinterName"]: item for item in printers_list}
        print("Printers found on this machine: %s" % (", ".join(printers_dict) or "(none)"))
        for pattern in patterns:
            for name, printer in printers_dict.items():
                if fnmatch.fnmatch(name, pattern):
                    title = status_titles.get(printer["Status"], "UNKNOWN")
                    print("Pattern %r matched %r (status=%s)" % (pattern, name, title))
                    if title not in unusable_titles:
                        printer_name = name
                        break
            if printer_name:
                break
        if not printer_name:
            print("No usable printer matched the configured pattern(s).")
            return False

    print("Opening Win32Raw connection to %r ..." % printer_name)
    printer_conn = Win32Raw(printer_name=printer_name)
    try:
        # Unlike Usb/Serial, Win32Raw.__init__() does NOT auto-open; this
        # actually starts a (near-empty) print job, same as
        # escpos_driver.py's open_printer() does on every status check.
        printer_conn.open()
        result = win32print.GetPrinter(printer_conn.hPrinter, 2)
        title = status_titles.get(result["Status"], "UNKNOWN (%s)" % result["Status"])
        print("Printer status = %s" % title)
        return title not in unusable_titles
    finally:
        printer_conn.close()


def main():
    checks = {"usb": check_usb, "serial": check_serial, "win32": check_win32}
    check = checks.get(DEVICE_TYPE)
    if not check:
        print(
            "Unknown ESCPOS_DEVICE_TYPE %r, expected one of %s"
            % (DEVICE_TYPE, list(checks))
        )
        sys.exit(2)
    try:
        connected = check()
    except Exception as e:
        print("Error: %s" % e)
        sys.exit(1)
    print("Connected = %s" % connected)
    sys.exit(0 if connected else 1)


if __name__ == "__main__":
    main()
