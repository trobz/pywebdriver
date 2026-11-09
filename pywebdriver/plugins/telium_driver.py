# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# @author Sébastien BEAU <sebastien.beau@akretion.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import curses.ascii

import pypostelium
import simplejson as json
from flask import jsonify, render_template, request
from serial import Serial

from pywebdriver import app, config, drivers

from .base_driver import ThreadDriver


def failure_answer(error_code, message, result_unknown=False, **extra):
    """Answer of transaction_start_with_return() when there's no payment
    result: transaction_result None tells the POS the payment didn't go
    through, payment_terminal_return_message says why in English, and
    error_code lets the POS show its own translated message. With result_unknown,
    the terminal took the amount but its answer was lost, so the bank may
    have approved the payment: a POS that knows the flag can ask the cashier
    to check the terminal, others still show the message as a failure.
    """
    if result_unknown:
        message = message.rstrip(". ") + (
            ". The payment may have been approved: check the merchant receipt"
            " on the terminal before retrying."
        )
    answer = {
        "transaction_result": None,
        "error_code": error_code,
        "payment_terminal_return_message": message,
        **extra,
    }
    if result_unknown:
        answer["result_unknown"] = True
    return answer


class TeliumDriver(ThreadDriver, pypostelium.Driver):
    """Telium Driver class for pywebdriver"""

    def __init__(self, *args, **kwargs):
        ThreadDriver.__init__(self)
        pypostelium.Driver.__init__(self, *args, **kwargs)
        # TODO : FIXME : Remove once 'status-posdisplay' branch is merged
        self.vendor_product = None
        driver_config = args[0] if args else {}
        # pyTeliumManager.Telium.verify() temporarily raises the serial
        # read timeout to this value (its DELAY_TERMINAL_ANSWER_TRANSACTION
        # constant, 120s) while waiting for the terminal's answer-ENQ: a
        # real card transaction (insert card, PIN, network authorization)
        # can take much longer than the fixed 3s timeout used for the rest
        # of the protocol. pypostelium itself never raises it.
        self.waiting_timeout = int(
            driver_config.get("telium_terminal_waiting_timeout", 120)
        )

    def get_payment_info_from_price(self, price, payment_mode):
        return {
            "amount": price,
            "payment_mode": payment_mode,
            "currency_iso": "EUR",
        }

    def _get_fullsized_answer_from_terminal(self, data):
        """Same as pypostelium.Driver.get_answer_from_terminal(), but reads
        the FULLSIZED answer (answer_flag='1'): 83 bytes including the
        55-byte report field (card data), like the legacy
        hw_telium_payment_terminal (OCA) controller did via the `telium`/
        pyTeliumManager library, instead of pypostelium's hardcoded 28-byte
        smallsized answer.
        """
        # STX + pos_number(2) + transaction_result(1) + amount_msg(8)
        # + payment_mode(1) + report(55) + currency_numeric(3) + private(10)
        # + ETX + LRC
        full_msg_size = 1 + 2 + 1 + 8 + 1 + 55 + 3 + 10 + 1 + 1
        msg = self.serial.read(size=full_msg_size).decode("ascii")
        app.logger.debug("Telium: %d bytes read from terminal", full_msg_size)
        assert len(msg) == full_msg_size, "Answer has a wrong size"
        if msg[0] != chr(curses.ascii.controlnames.index("STX")):
            app.logger.error(
                "Telium: the first byte of the answer from terminal should be STX"
            )
        if msg[-2] != chr(curses.ascii.controlnames.index("ETX")):
            app.logger.error(
                "Telium: the byte before final of the answer from terminal "
                "should be ETX"
            )
        lrc = msg[-1]
        computed_lrc = chr(self.generate_lrc(msg[1:-1]))
        if computed_lrc != lrc:
            app.logger.error("Telium: the LRC of the answer from terminal is wrong")
        real_msg = msg[1:-2]
        app.logger.debug("Telium: real answer received = %s", real_msg)
        answer_data = {
            "pos_number": real_msg[0:2],
            "transaction_result": real_msg[2],
            "amount_msg": real_msg[3:11],
            "payment_mode": real_msg[11],
            "report": real_msg[12:67],
            "currency_numeric": real_msg[67:70],
            "private": real_msg[70:80],
        }
        self.compare_data_vs_answer(data, answer_data)
        return answer_data

    def transaction_start_with_return(self, payment_info):
        """Same as pypostelium.Driver.transaction_start(), but returns the
        full answer from the terminal instead of a plain True/False, for
        callers that need to wait for and read the payment result
        synchronously.

        Like the legacy hw_telium_payment_terminal (OCA) controller (which
        used the `telium`/pyTeliumManager library), this asks the terminal
        to WAIT for the transaction to complete and to send back the
        FULLSIZED report, instead of pypostelium's defaults (instant
        answer, smallsized report) which are meant for the fire-and-forget
        transaction_start() flow.

        When there's no payment result, returns a failure_answer() saying
        why, flagged result_unknown once the terminal took the amount.
        """
        payment_info_dict = json.loads(payment_info)
        assert isinstance(payment_info_dict, dict), "payment_info_dict should be a dict"
        answer = {}
        # Why there's no answer, for the cashier: (error_code, message).
        failure = None
        # Set once the terminal ACKs the amount: from then on, it can
        # complete the payment with the bank even if we never get its answer
        # (e.g. it comes after waiting_timeout, or can't be read).
        amount_accepted = False
        try:
            app.logger.debug(
                "Telium: opening serial port %s for payment terminal "
                "with baudrate %d",
                self.device_name,
                self.device_rate,
            )
            # IMPORTANT: don't modify timeout=3 seconds. The Telium spec
            # says we have to wait up to 3 seconds to get the LRC.
            try:
                self.serial = Serial(self.device_name, self.device_rate, timeout=3)
            except Exception as e:
                self.serial = False
                failure = (
                    "port_error",
                    "Cannot open the payment terminal port %s: %s"
                    % (self.device_name, e),
                )
                raise
            if not self.initialize_msg():
                return failure_answer(
                    "no_answer",
                    "The payment terminal on %s did not answer: check that it "
                    "is switched on, connected and on its idle screen, then "
                    "try again." % self.device_name,
                )
            data = self.prepare_data_to_send(payment_info_dict)
            if not data:
                return failure_answer(
                    "unsupported",
                    "The payment mode or currency is not supported by the "
                    "payment terminal: %s %s"
                    % (
                        payment_info_dict.get("payment_mode"),
                        payment_info_dict.get("currency_iso"),
                    ),
                )
            data["answer_flag"] = "1"  # TERMINAL_ANSWER_SET_FULLSIZED
            data["delay"] = "A010"  # TERMINAL_REQUEST_ANSWER_WAIT_FOR_TRANSACTION
            self.send_message(data)
            if not self.get_one_byte_answer("ACK"):
                return failure_answer(
                    "amount_refused",
                    "The payment terminal did not accept the amount: it may "
                    "be busy with a previous transaction. Press the red button "
                    "on the terminal, then try again.",
                )
            amount_accepted = True
            self.send_one_byte_signal("EOT")
            app.logger.info("Telium: now expecting answer from Terminal")
            # Raise the read timeout only for this wait: the
            # terminal can take much longer than 3s to actually
            # complete the transaction before it signals us.
            self.serial.timeout = self.waiting_timeout
            try:
                got_enq = self.get_one_byte_answer("ENQ")
            finally:
                self.serial.timeout = 3
            if not got_enq:
                return failure_answer(
                    "timeout",
                    "No answer from the payment terminal within %d s after it "
                    "took the amount." % self.waiting_timeout,
                    result_unknown=True,
                    waiting_timeout=self.waiting_timeout,
                )
            self.send_one_byte_signal("ACK")
            answer_data = self._get_fullsized_answer_from_terminal(data)
            # Keep the result as soon as it's read: a missing EOT
            # after it doesn't change what the terminal reported.
            answer = {
                "pos_number": answer_data["pos_number"],
                "transaction_result": int(answer_data["transaction_result"]),
                "amount_msg": float(answer_data["amount_msg"]),
                "payment_mode": answer_data["payment_mode"],
                "payment_terminal_return_message": answer_data,
            }
            self.send_one_byte_signal("ACK")
            if self.get_one_byte_answer("EOT"):
                app.logger.info("Telium: answer received from Terminal")
            else:
                app.logger.warning(
                    "Telium: no EOT after the answer from Terminal, "
                    "using the answer anyway"
                )
            return answer
        except Exception as e:
            app.logger.error("Telium: exception in serial connection: %s", e)
            if answer:
                return answer
            if not failure:
                failure = (
                    "error",
                    "Error while talking to the payment terminal: %s"
                    % (str(e) or type(e).__name__),
                )
            return failure_answer(*failure, result_unknown=amount_accepted)
        finally:
            if self.serial:
                app.logger.debug("Telium: closing serial port for payment terminal")
                self.serial.close()


driver_config = {}
if config.get("telium_driver", "device_name"):
    driver_config["telium_terminal_device_name"] = config.get(
        "telium_driver", "device_name"
    )
if config.getint("telium_driver", "device_rate"):
    driver_config["telium_terminal_device_rate"] = config.getint(
        "telium_driver", "device_rate"
    )
if config.getint("telium_driver", "waiting_timeout", fallback=0):
    driver_config["telium_terminal_waiting_timeout"] = config.getint(
        "telium_driver", "waiting_timeout", fallback=0
    )

telium_driver = TeliumDriver(driver_config)
drivers["telium"] = telium_driver


@app.route(
    "/hw_proxy/payment_terminal_transaction_start", methods=["POST", "GET", "PUT"]
)
def payment_terminal_transaction_start():
    app.logger.debug("Telium: Call payment_terminal_transaction_start")
    payment_info = request.json["params"]["payment_info"]
    app.logger.debug("Telium: payment_info=%s", payment_info)
    result = telium_driver.transaction_start(payment_info)
    app.logger.debug("Telium: result of transation_start=%s", result)
    return jsonify(jsonrpc="2.0", result=result)


@app.route(
    "/hw_proxy/payment_terminal_transaction_start_with_return",
    methods=["POST", "GET", "PUT"],
)
def payment_terminal_transaction_start_with_return():
    app.logger.debug("Telium: Call payment_terminal_transaction_start_with_return")
    payment_info = request.json["params"]["payment_info"]
    app.logger.debug("Telium: payment_info=%s", payment_info)
    result = telium_driver.transaction_start_with_return(payment_info)
    app.logger.debug("Telium: result of transaction_start_with_return=%s", result)
    return jsonify(jsonrpc="2.0", result=result)


@app.route("/telium_status.html", methods=["POST"])
def telium_status():
    values = request.form.to_dict()
    info = telium_driver.get_payment_info_from_price(
        float(values.get("price") or 0.00), request.values["payment_mode"]
    )
    app.logger.debug("Telium status info=%s", info)
    telium_driver.push_task("transaction_start", json.dumps(info, sort_keys=True))
    return render_template("telium_status.html")
