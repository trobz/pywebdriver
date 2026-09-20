#!/usr/bin/env python3
# Copyright (C) 2019 Druidoo (https://www.druidoo.io)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""
Standalone Cashlogy cashdrawer test script.

This talks directly to the CashlogyConnector middleware over the same TCP
protocol used by pywebdriver/plugins/cashlogy_cashdrawer_driver.py (a
minimal, dependency-free port of CashlogyDriver's connect/initialize/send
logic, with the Flask/ThreadDriver plumbing stripped out), so you can
validate the connection and a few commands against real hardware without
starting the pywebdriver/Flask server at all.

Runs: connect -> initialize -> get_inventory, printing each step.

Usage:
    python3 cashlogy-test.py
"""
import os
import socket

BUFFER_SIZE = 1024
SOCKET_TIMEOUT = 30
INITIALIZE_TIMEOUT = 240


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

# Same defaults as config.ini.tmpl's [cashlogy_cashdrawer_driver] section.
HOST = os.environ.get("CASHLOGY_HOST", "127.0.0.1")
PORT = int(os.environ.get("CASHLOGY_PORT", "8092"))
print("CASHLOGY_HOST = %s" % HOST)
print("CASHLOGY_PORT = %s" % PORT)


class CashlogyTestClient:
    """Minimal standalone port of CashlogyDriver: just the socket
    protocol (connect/initialize/send/get_inventory), no Flask or
    ThreadDriver/queue dependency.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.socket = None

    def connect(self):
        print("Connecting to {}:{} ...".format(self.host, self.port))
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(SOCKET_TIMEOUT)
        self.socket.connect((self.host, self.port))
        print("Connected.")

    def initialize(self):
        """Initialize the machine. Takes around 1 minute on first run.

        Returns the firmware version string.
        """
        print("Initializing (can take up to 1 minute on first run)...")
        self.socket.settimeout(INITIALIZE_TIMEOUT)
        try:
            res = self.send(["I"])
        finally:
            self.socket.settimeout(SOCKET_TIMEOUT)
        firmware_version = res and res[1]
        print("Firmware version = %s" % firmware_version)
        return firmware_version

    def disconnect(self):
        if self.socket:
            try:
                self.send(["E"])
            except Exception as e:
                print("Error sending disconnect signal: %s" % e)
            self.socket.close()
            self.socket = None
            print("Disconnected.")

    def _send(self, msg):
        self.socket.send(msg.encode() if isinstance(msg, str) else msg)
        res = self.socket.recv(BUFFER_SIZE)
        return res.decode()

    def send(self, msg, raw=False):
        """Send a message to CashlogyConnector and return the parsed
        response.

        Args:
            msg: either a raw string '#I#0#1#' or a list ['I', 0, 1]
            raw: if True, return the raw response string unparsed

        Returns a list split on '#' delimiter, e.g. ['I', '2.01']
        """
        if not isinstance(msg, str):
            msg = list(msg)
            for i, v in enumerate(msg):
                if isinstance(v, str):
                    continue
                elif isinstance(v, bool):
                    msg[i] = str(int(v))
                elif isinstance(v, int):
                    msg[i] = str(v)
                elif isinstance(v, float):
                    msg[i] = str(int(v * 100))
                else:
                    msg[i] = str(v)
            msg = "#%s#" % "#".join(msg)
        print("Sending: %s" % msg)
        res_raw = self._send(msg)
        print("Received: %s" % res_raw)
        res = res_raw.strip("#").split("#")
        if res and res[0].startswith("ER:"):
            raise Exception("Cashlogy error: {} (sent: {})".format(res_raw, msg))
        elif res and res[0].startswith("WR:"):
            print("Cashlogy warning: {} (sent: {})".format(res_raw, msg))
        if raw:
            return res_raw
        return res

    def value_float(self, value):
        """Convert Cashlogy integer-cents string to float euros."""
        if isinstance(value, str):
            return float(value) / 100
        elif isinstance(value, int):
            return float(value)
        elif isinstance(value, float):
            return value
        raise TypeError("Unrecognized type for value_float: %s" % type(value))

    def get_inventory(self):
        """Get the content of the cashdrawer by denomination.

        Returns dict with 'recycler', 'stacker', 'total' where each value
        is a dict of {denomination_float: count}.
        """
        res = self.send(["Y"])
        recycler = {
            self.value_float(i[0]): int(i[1])
            for i in [p.split(":") for p in res[1].replace(";", ",").split(",")]
        }
        stacker = {
            self.value_float(i[0]): int(i[1])
            for i in [p.split(":") for p in res[2].replace(";", ",").split(",")]
        }
        totals = {
            v: recycler.get(v, 0) + stacker.get(v, 0)
            for v in set(list(recycler.keys()) + list(stacker.keys()))
        }
        return {"recycler": recycler, "stacker": stacker, "total": totals}


def main():
    client = CashlogyTestClient(HOST, PORT)
    try:
        client.connect()
        client.initialize()
        inventory = client.get_inventory()
        print("Inventory = %s" % inventory)
    except Exception as e:
        print("Error: %s" % e)
        raise
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
