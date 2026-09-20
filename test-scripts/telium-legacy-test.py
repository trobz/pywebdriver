#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pyTeliumManager>=2.4.2",
# ]
# ///

# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).
"""
Standalone Telium payment terminal test script, legacy `telium` library.

Unlike telium-test.py (which drives the terminal through
`pypostelium.Driver`, the library pywebdriver's telium_driver.py plugin
is built on), this script drives it through the actual `telium`
(pyTeliumManager) library -- the same library imported by the legacy
oca/posbox-addons/hw_telium_payment_terminal/controllers/main.py
`TeliumPaymentTerminalDriver.transaction_start()`.

It reproduces that legacy controller's call sequence and hardcoded
settings as closely as possible (EUR only, fullsized answer,
wait-for-transaction, debit mode, no forced authorization, default
Telium() baudrate/timeout), so its output can be compared against
telium-test.py's to validate that the pypostelium-based pywebdriver
plugin behaves the same as the library the terminal integration
originally shipped with.

Usage:
    python3 telium-legacy-test.py
"""
import os

import telium

DEVICE = "/dev/ttyACM0"
PAYMENT_MODE = "card"  # 'card' or 'check'
# pyTeliumManager.TeliumData.is_amount_valid() rejects anything below
# TERMINAL_MINIMAL_AMOUNT_REQUESTABLE (1.00), unlike pypostelium which
# doesn't validate the amount at all.
AMOUNT = 1.00


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

PAYMENT_MODE = _env_choice("PAYMENT_MODE", ("card", "check"), PAYMENT_MODE)
print("PAYMENT_MODE = %s" % PAYMENT_MODE)
AMOUNT = float(os.environ.get("AMOUNT", AMOUNT))
print("AMOUNT = %s" % AMOUNT)


def main():
    if PAYMENT_MODE == "check":
        payment_mode = telium.TERMINAL_TYPE_PAYMENT_CHECK
    else:
        payment_mode = telium.TERMINAL_TYPE_PAYMENT_CARD

    my_payment = telium.TeliumAsk(
        "1",  # Checkout ID 1
        telium.TERMINAL_ANSWER_SET_FULLSIZED,  # Ask for fullsized report
        telium.TERMINAL_MODE_PAYMENT_DEBIT,  # Ask for debit
        payment_mode,  # Using a card or a check
        telium.TERMINAL_NUMERIC_CURRENCY_EUR,
        telium.TERMINAL_REQUEST_ANSWER_WAIT_FOR_TRANSACTION,  # Wait for transaction to end
        telium.TERMINAL_FORCE_AUTHORIZATION_DISABLE,  # Let device choose
        # if authorization needed
        AMOUNT,
    )

    print("Opening serial port %s for payment terminal" % DEVICE)
    # Same as the legacy controller: no baudrate/timeout override, so
    # this uses telium.Telium's defaults (9600 bps, 1s read timeout)
    # instead of telium-test.py's/pypostelium's fixed 3s.
    my_device = telium.Telium(DEVICE)
    answer = {}
    try:
        try:
            if not my_device.ask(my_payment):
                print("Unable to init payment on device.")
                return
        except telium.TerminalInitializationFailedException as e:
            print("Terminal initialization failed: %s" % e)
            return

        print("Now expecting answer from Terminal")
        my_answer = my_device.verify(my_payment)

        if my_answer is not None:
            print("answer.__dict__ = %s" % my_answer.__dict__)
            answer = {
                "pos_number": my_answer.__dict__["_pos_number"],
                "transaction_result": my_answer.__dict__["_transaction_result"],
                "amount_msg": float(my_answer.__dict__["_amount"]) * 100,
                "payment_mode": my_answer.__dict__["_payment_mode"],
                "payment_terminal_return_message": my_answer.__dict__,
            }
        else:
            print("No answer received from Terminal (verify() returned None)")
    finally:
        print("Closing serial port for payment terminal")
        my_device.close()

    print("Final answer = %s" % answer)


if __name__ == "__main__":
    main()
