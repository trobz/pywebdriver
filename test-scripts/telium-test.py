#!/usr/bin/env python3
# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""
Standalone Telium payment terminal test script.

This is the pywebdriver/pypostelium equivalent of the legacy
oca/posbox-addons/hw_telium_payment_terminal/test-scripts/telium-test.py
script, which talked to the terminal directly over pyserial with
hand-rolled protocol E+ code (ENQ/ACK handshake, STX/ETX/LRC framing).

Here the same test is driven through `pypostelium.Driver` (the library
pywebdriver's telium_driver.py plugin is built on), so you can validate
the serial link and a full payment round-trip against real hardware
without starting the pywebdriver/Flask server at all.

Usage:
    python3 telium-test.py
"""
import curses.ascii
import os

import pypostelium
from serial import Serial

DEVICE = "/dev/ttyACM0"
DEVICE_RATE = 9600
PAYMENT_MODE = "card"  # 'card' or 'check'
CURRENCY_ISO = "EUR"
AMOUNT = 0.93


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


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_choice(name, choices, default):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value not in choices:
        raise ValueError(
            "Invalid value {!r} for {}, expected one of {}".format(value, name, choices)
        )
    return value


_load_dotenv()

# The legacy hw_telium_payment_terminal (OCA) controller (via the
# `telium`/pyTeliumManager library) asked the terminal to WAIT for the
# transaction to actually complete (answer_flag='1', delay='A010').
# pypostelium.Driver.transaction_start() defaults to the opposite (instant
# answer, delay='A011'), which is what it's used for pywebdriver's
# fire-and-forget transaction_start endpoint.
# Set WAIT_FOR_TRANSACTION=false in .env to test that pypostelium default
# instead.
WAIT_FOR_TRANSACTION = _env_bool("WAIT_FOR_TRANSACTION", True)
print("WAIT_FOR_TRANSACTION = %s" % WAIT_FOR_TRANSACTION)

# Whether to ask the terminal for the FULLSIZED answer (83 bytes, includes
# the 55-byte report field with card data, like the legacy controller) or
# pypostelium's default SMALLSIZED answer (28 bytes, no card data).
# This is independent from WAIT_FOR_TRANSACTION: it only controls the size
# of the answer frame, not whether the terminal waits for the transaction.
ANSWER_SIZE = _env_choice("ANSWER_SIZE", ("fullsize", "smallsize"), "fullsize")
print("ANSWER_SIZE = %s" % ANSWER_SIZE)

# pyTeliumManager's Telium.verify() temporarily raises the serial read
# timeout to this value (default 120s, its DELAY_TERMINAL_ANSWER_TRANSACTION
# constant) while waiting for the terminal's answer-ENQ, since a real card
# transaction (insert card, PIN, network authorization) can take much
# longer than the few seconds used for the rest of the protocol.
# pypostelium never does this -- it keeps the fixed 3s timeout throughout,
# so without this the host gives up long before the terminal is done.
WAITING_TIMEOUT = int(os.environ.get("WAITING_TIMEOUT", "120"))
print("WAITING_TIMEOUT = %s" % WAITING_TIMEOUT)


class TeliumTestDriver(pypostelium.Driver):
    """pypostelium.Driver, patched the same way as
    TeliumDriver.transaction_start_with_return() in
    pywebdriver/plugins/telium_driver.py, to wait for the transaction
    result and read the fullsized answer.
    """

    def get_fullsized_answer_from_terminal(self, data):
        # STX + pos_number(2) + transaction_result(1) + amount_msg(8)
        # + payment_mode(1) + report(55) + currency_numeric(3) + private(10)
        # + ETX + LRC
        full_msg_size = 1 + 2 + 1 + 8 + 1 + 55 + 3 + 10 + 1 + 1
        msg = self.serial.read(size=full_msg_size).decode("ascii")
        print("%d bytes read from terminal" % full_msg_size)
        assert len(msg) == full_msg_size, "Answer has a wrong size"
        if msg[0] != chr(curses.ascii.controlnames.index("STX")):
            print("The first byte of the answer from terminal should be STX")
        if msg[-2] != chr(curses.ascii.controlnames.index("ETX")):
            print("The byte before final of the answer from terminal should be ETX")
        lrc = msg[-1]
        computed_lrc = chr(self.generate_lrc(msg[1:-1]))
        if computed_lrc != lrc:
            print("The LRC of the answer from terminal is wrong")
        real_msg = msg[1:-2]
        print("Real answer received = %s" % real_msg)
        answer_data = {
            "pos_number": real_msg[0:2],
            "transaction_result": real_msg[2],
            "amount_msg": real_msg[3:11],
            "payment_mode": real_msg[11],
            "report": real_msg[12:67],
            "currency_numeric": real_msg[67:70],
            "private": real_msg[70:80],
        }
        print("answer_data = %s" % answer_data)
        self.compare_data_vs_answer(data, answer_data)
        return answer_data

    def transaction_start_with_return(self, payment_info_dict):
        answer = {}
        try:
            print(
                "Opening serial port %s for payment terminal with baudrate %d"
                % (self.device_name, self.device_rate)
            )
            # IMPORTANT: don't modify timeout=3 seconds. The Telium spec
            # says we have to wait up to 3 seconds to get the LRC.
            self.serial = Serial(self.device_name, self.device_rate, timeout=3)
            if self.initialize_msg():
                data = self.prepare_data_to_send(payment_info_dict)
                if not data:
                    return answer
                if WAIT_FOR_TRANSACTION:
                    data[
                        "delay"
                    ] = "A010"  # TERMINAL_REQUEST_ANSWER_WAIT_FOR_TRANSACTION
                if ANSWER_SIZE == "fullsize":
                    data["answer_flag"] = "1"  # TERMINAL_ANSWER_SET_FULLSIZED
                print("Data to send = %s" % data)
                self.send_message(data)
                print("Message sent to terminal")
                if self.get_one_byte_answer("ACK"):
                    self.send_one_byte_signal("EOT")
                    print("Now expecting answer from Terminal")
                    # Raise the read timeout only for this wait: the
                    # terminal can take much longer than 3s to actually
                    # complete the transaction before it signals us.
                    self.serial.timeout = WAITING_TIMEOUT
                    try:
                        got_enq = self.get_one_byte_answer("ENQ")
                    finally:
                        self.serial.timeout = 3
                    if got_enq:
                        self.send_one_byte_signal("ACK")
                        if ANSWER_SIZE == "fullsize":
                            answer_data = self.get_fullsized_answer_from_terminal(data)
                        else:
                            answer_data = self.get_answer_from_terminal(data)
                        self.send_one_byte_signal("ACK")
                        if self.get_one_byte_answer("EOT"):
                            print("Answer received from Terminal")
                            answer = answer_data
        except Exception as e:
            print("Exception in serial connection: %s" % str(e))
        finally:
            if self.serial:
                print("Closing serial port for payment terminal")
                self.serial.close()
        return answer


def main():
    driver = TeliumTestDriver(
        {
            "telium_terminal_device_name": DEVICE,
            "telium_terminal_device_rate": DEVICE_RATE,
        }
    )
    payment_info_dict = {
        "amount": AMOUNT,
        "payment_mode": PAYMENT_MODE,
        "currency_iso": CURRENCY_ISO,
    }
    answer = driver.transaction_start_with_return(payment_info_dict)
    print("Final answer = %s" % answer)


if __name__ == "__main__":
    main()
