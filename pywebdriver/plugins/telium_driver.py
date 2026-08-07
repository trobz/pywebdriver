# Copyright (C) 2014-Today Akretion (http://www.akretion.com).
# @author Sébastien BEAU <sebastien.beau@akretion.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import pypostelium
import simplejson as json
from flask import jsonify, render_template, request
from serial import Serial

from pywebdriver import app, config, drivers

from .base_driver import ThreadDriver


class TeliumDriver(ThreadDriver, pypostelium.Driver):
    """Telium Driver class for pywebdriver"""

    def __init__(self, *args, **kwargs):
        ThreadDriver.__init__(self)
        pypostelium.Driver.__init__(self, *args, **kwargs)
        # TODO : FIXME : Remove once 'status-posdisplay' branch is merged
        self.vendor_product = None

    def get_payment_info_from_price(self, price, payment_mode):
        return {
            "amount": price,
            "payment_mode": payment_mode,
            "currency_iso": "EUR",
        }

    def transaction_start_with_return(self, payment_info):
        """Same as pypostelium.Driver.transaction_start(), but returns the
        full answer from the terminal instead of a plain True/False, for
        callers that need to wait for and read the payment result
        synchronously.
        """
        payment_info_dict = json.loads(payment_info)
        assert isinstance(payment_info_dict, dict), "payment_info_dict should be a dict"
        answer = {}
        try:
            app.logger.debug(
                "Telium: opening serial port %s for payment terminal "
                "with baudrate %d",
                self.device_name,
                self.device_rate,
            )
            # IMPORTANT: don't modify timeout=3 seconds. The Telium spec
            # says we have to wait up to 3 seconds to get the LRC.
            self.serial = Serial(self.device_name, self.device_rate, timeout=3)
            if self.initialize_msg():
                data = self.prepare_data_to_send(payment_info_dict)
                if not data:
                    return answer
                self.send_message(data)
                if self.get_one_byte_answer("ACK"):
                    self.send_one_byte_signal("EOT")
                    app.logger.info("Telium: now expecting answer from Terminal")
                    if self.get_one_byte_answer("ENQ"):
                        self.send_one_byte_signal("ACK")
                        answer_data = self.get_answer_from_terminal(data)
                        self.send_one_byte_signal("ACK")
                        if self.get_one_byte_answer("EOT"):
                            app.logger.info("Telium: answer received from Terminal")
                            answer = {
                                "pos_number": answer_data["pos_number"],
                                "transaction_result": int(
                                    answer_data["transaction_result"]
                                ),
                                "amount_msg": float(answer_data["amount_msg"]),
                                "payment_mode": answer_data["payment_mode"],
                                "payment_terminal_return_message": answer_data,
                            }
        except Exception as e:
            app.logger.error("Telium: exception in serial connection: %s", e)
        finally:
            if self.serial:
                app.logger.debug("Telium: closing serial port for payment terminal")
                self.serial.close()
        return answer


driver_config = {}
if config.get("telium_driver", "device_name"):
    driver_config["telium_terminal_device_name"] = config.get(
        "telium_driver", "device_name"
    )
if config.getint("telium_driver", "device_rate"):
    driver_config["telium_terminal_device_rate"] = config.getint(
        "telium_driver", "device_rate"
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
