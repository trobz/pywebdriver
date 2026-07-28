# Copyright (C) 2024-Today Trobz.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import base64
import json
import logging
from typing import Any

import requests
from flask import jsonify, request

from pywebdriver import app, config, drivers

from .payment_base_driver import PaymentTerminalDriver

CONFIG_SECTION = "pax_driver"
_logger = logging.getLogger(__name__)

# PAX POS Link protocol special bytes
STX = 0x02
ETX = 0x03
FS = 0x1C  # Field Separator between top-level fields
US = 0x1F  # Unit Separator between sub-fields (unused in most commands)


def _compute_lrc(data: bytes) -> int:
    """XOR all bytes to produce the Longitudinal Redundancy Check byte."""
    lrc = 0
    for b in data:
        lrc ^= b
    return lrc


def build_pos_link_request(command: str, version: str = "1.28", *params) -> bytes:
    """
    Build a PAX POS Link binary frame.

    Frame layout:
        STX | Length(4 ASCII digits) | Command | FS | Version | FS | [Param FS]... | ETX | LRC
    """
    parts = [command.encode("ascii"), version.encode("ascii")]
    for p in params:
        if p is None:
            parts.append(b"")
        elif isinstance(p, bytes):
            parts.append(p)
        else:
            parts.append(str(p).encode("ascii", errors="replace"))

    body = bytes([FS]).join(parts)
    length_val = len(body) + 1  # +1 for ETX
    length_bytes = f"{length_val:04d}".encode()

    inner = length_bytes + body + bytes([ETX])
    lrc = _compute_lrc(inner)
    return bytes([STX]) + inner + bytes([lrc])


def pos_link_to_base64(frame: bytes) -> str:
    """Base64-encode a POS Link frame for use in the PAX HTTP GET query string."""
    return base64.b64encode(frame).decode("ascii")


def parse_pos_link_response(raw: bytes) -> dict:
    """
    Parse raw bytes returned by the PAX terminal HTTP endpoint.

    Response frame format:
        STX | Length(4) | Command | FS | Version | FS | RespCode | FS | RespMsg
        | FS | [fields...]  | ETX | LRC
    """
    if not raw or raw[0] != STX:
        return {"success": False, "error": "Invalid response: missing STX"}

    try:
        etx_pos = raw.index(ETX, 1)
    except ValueError:
        return {"success": False, "error": "Invalid response: missing ETX"}

    inner = raw[1:etx_pos]
    payload = inner[4:]  # skip 4-byte length prefix
    fields = payload.split(bytes([FS]))

    def _f(idx):
        return (
            fields[idx].decode("ascii", errors="replace") if len(fields) > idx else ""
        )

    result: dict[str, Any] = {
        "command": _f(0),
        "version": _f(1),
        "response_code": _f(2),
        "response_message": _f(3),
    }
    result["success"] = result["response_code"] == "000000"

    cmd = result["command"]
    if cmd in ("T00", "T02"):  # DoCredit / DoRefund
        extra_labels = [
            "host_response_code",
            "host_response_message",
            "auth_code",
            "reference_number",
            "transaction_id",
            "extra_data",
            "trace_number",
            "eod_record",
            "avn",
            "approved_amount",
            "balance",
            "card_holder_name",
            "expiry_date",
            "first_six",
            "last_four",
        ]
        for i, label in enumerate(extra_labels, start=4):
            result[label] = _f(i)

    return result


class PaxDriver(PaymentTerminalDriver):
    """
    PAX payment terminal driver for pywebdriver.

    Communicates with PAX terminals using the POS Link HTTP protocol:
    each command is encoded as a binary frame, base64-encoded, and sent
    as the query string of an HTTP GET request to the terminal's built-in
    HTTP server.

    Configuration (config.ini [pax_driver] section):
        terminal_ip     – default terminal IP address
        terminal_port   – default terminal port (default: 10009)
        use_https       – whether to use HTTPS (default: false)
        timeout         – request timeout in seconds (default: 120)
    """

    def __init__(self):
        super().__init__()
        self._default_ip = config.get(CONFIG_SECTION, "terminal_ip", fallback="")
        self._default_port = config.getint(
            CONFIG_SECTION, "terminal_port", fallback=10009
        )
        self._default_https = config.getboolean(
            CONFIG_SECTION, "use_https", fallback=False
        )
        self._default_timeout = config.getint(CONFIG_SECTION, "timeout", fallback=120)

    def _send_to_terminal(
        self,
        frame: bytes,
        terminal_ip: str = "",
        terminal_port: int = 0,
        use_https: bool = False,
        timeout: int = 0,
        terminal_id: str = "0",
    ) -> dict:
        ip = terminal_ip or self._default_ip
        port = terminal_port or self._default_port
        scheme = "https" if use_https else "http"
        effective_timeout = timeout or self._default_timeout

        if not ip:
            return {"success": False, "error": "No PAX terminal IP configured"}

        b64 = pos_link_to_base64(frame)
        url = f"{scheme}://{ip}:{port}?{b64}"
        app.logger.debug("PAX driver → %s", url)
        try:
            resp = requests.get(url, timeout=effective_timeout, verify=False)
            resp.raise_for_status()
            result = parse_pos_link_response(resp.content)
            self._set_terminal_status(terminal_id, "connected")
            return result
        except requests.exceptions.Timeout:
            self._set_terminal_status(terminal_id, "disconnected")
            return {"success": False, "error": "Terminal request timed out"}
        except requests.exceptions.ConnectionError as exc:
            self._set_terminal_status(terminal_id, "disconnected")
            return {"success": False, "error": f"Cannot connect to terminal: {exc}"}
        except Exception as exc:
            self._set_terminal_status(terminal_id, "error")
            _logger.exception("Unexpected error contacting PAX terminal at %s", url)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Task handlers (run in the driver thread)
    # ------------------------------------------------------------------

    def transaction_start(self, data):
        """
        Process a DoCredit (T00) payment transaction.

        Expected ``data`` keys (all optional, fall back to config.ini defaults):
            payment_info.terminal_ip       – terminal IP
            payment_info.terminal_port     – terminal port
            payment_info.use_https         – bool
            payment_info.amount            – float (e.g. 12.50)
            payment_info.currency_code     – ISO 4217 numeric string (e.g. "840")
            payment_info.order_id          – ECR reference / order name
            payment_info.transaction_type  – PAX code: "01" sale, "02" return,
                                             "04" void, "05" auth, "06" post-auth
            payment_info.timeout           – timeout in milliseconds
        """
        payment_info = data.get("payment_info", {})
        transaction_id = data.get("transaction_id")
        terminal_id = payment_info.get("terminal_id", "0")

        terminal_ip = payment_info.get("terminal_ip", "")
        terminal_port = int(payment_info.get("terminal_port", 0) or 0)
        use_https = bool(payment_info.get("use_https", self._default_https))
        amount = payment_info.get("amount", 0)
        amount_cents = int(round(float(amount) * 100))
        currency_code = payment_info.get("currency_code", "840")
        order_id = payment_info.get("order_id", "")
        transaction_type = payment_info.get("transaction_type", "01")
        timeout_ms = payment_info.get("timeout")
        timeout_s = int(timeout_ms) // 1000 if timeout_ms else self._default_timeout

        app.logger.info(
            "PAX driver transaction_start — terminal=%s amount_cents=%s order=%s",
            terminal_id,
            amount_cents,
            order_id,
        )

        frame = build_pos_link_request(
            "T00",
            "1.28",
            transaction_type,
            str(amount_cents),
            "",  # tip
            "",  # cash back
            "",  # merchant fee
            "",  # tax
            "",  # fuel
            "",  # service fee
            "",  # surcharge
            "",  # discount
            "",  # original amount
            "",  # invoice number
            order_id,
            "",  # offline auth code
            "",  # original transaction id (for void/post-auth)
            currency_code,
        )

        result = self._send_to_terminal(
            frame,
            terminal_ip=terminal_ip,
            terminal_port=terminal_port,
            use_https=use_https,
            timeout=timeout_s,
            terminal_id=terminal_id,
        )

        self.end_transaction(
            terminal_id,
            transaction_id,
            success=result.get("success", False),
            status=result.get("response_message", ""),
            status_details=result.get("response_code", ""),
            reference=result.get("transaction_id", ""),
        )

    def do_void(self, data):
        """
        Process a Void (T00 type=04) transaction.

        Expected ``data`` keys:
            terminal_ip, terminal_port, use_https, timeout,
            original_transaction_id, original_auth_code, amount_cents
        """
        terminal_ip = data.get("terminal_ip", "")
        terminal_port = int(data.get("terminal_port", 0) or 0)
        use_https = bool(data.get("use_https", self._default_https))
        timeout_s = data.get("timeout", self._default_timeout)
        amount_cents = int(data.get("amount_cents", 0))
        orig_txn = data.get("original_transaction_id", "")
        orig_auth = data.get("original_auth_code", "")

        frame = build_pos_link_request(
            "T00",
            "1.28",
            "04",
            str(amount_cents),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            orig_auth,
            orig_txn,
        )
        return self._send_to_terminal(
            frame,
            terminal_ip=terminal_ip,
            terminal_port=terminal_port,
            use_https=use_https,
            timeout=timeout_s,
        )

    def initialize(self, data):
        """Send A08 Initialize command — test connectivity and retrieve terminal info."""
        frame = build_pos_link_request("A08", "1.28")
        return self._send_to_terminal(
            frame,
            terminal_ip=data.get("terminal_ip", ""),
            terminal_port=int(data.get("terminal_port", 0) or 0),
            use_https=bool(data.get("use_https", self._default_https)),
            timeout=data.get("timeout", 30),
        )


# ------------------------------------------------------------------
# Flask routes
# ------------------------------------------------------------------

pax_driver = PaxDriver()
drivers["pax_driver"] = pax_driver


@app.route("/hw_proxy/pax/transaction_start", methods=["POST"])
def pax_transaction_start():
    """
    Start a PAX payment transaction (async — returns transaction_id immediately).

    Request body (JSON-RPC 2.0):
        { "params": { "payment_info": { ... } } }

    The caller should poll ``/hw_proxy/pax/transaction_get`` for the result.
    """
    body = request.json or {}
    payment_info = body.get("params", {}).get("payment_info", {})
    if isinstance(payment_info, str):
        payment_info = json.loads(payment_info)

    terminal_id = payment_info.get("terminal_id", "0")
    transaction = pax_driver.begin_transaction(terminal_id)
    pax_driver.push_task(
        "transaction_start",
        data=dict(
            payment_info=payment_info,
            transaction_id=transaction["transaction_id"],
        ),
    )
    return jsonify(jsonrpc="2.0", result=transaction)


@app.route("/hw_proxy/pax/transaction_get", methods=["POST", "GET"])
def pax_transaction_get():
    """
    Poll the result of a PAX transaction.

    Returns the transaction dict with ``success`` field (None = still in progress).
    """
    params = {}
    if request.json:
        params = request.json.get("params", {})
    elif request.args:
        params = request.args.to_dict()

    terminal_id = params.get("terminal_id", "0")
    transaction_id = params.get("transaction_id")

    with pax_driver.lock:
        if transaction_id:
            result = pax_driver._get_transaction(terminal_id, int(transaction_id))
        else:
            result = pax_driver._get_last_transaction(terminal_id)

    return jsonify(jsonrpc="2.0", result=result)


@app.route("/hw_proxy/pax/void", methods=["POST"])
def pax_void():
    """
    Void a previous PAX transaction (synchronous).

    Request body (JSON-RPC 2.0 params):
        terminal_ip, terminal_port, use_https, timeout,
        original_transaction_id, original_auth_code, amount_cents
    """
    body = request.json or {}
    params = body.get("params", {})
    result = pax_driver.do_void(params)
    return jsonify(jsonrpc="2.0", result=result)


@app.route("/hw_proxy/pax/initialize", methods=["POST"])
def pax_initialize():
    """
    Test PAX terminal connectivity (A08 Initialize — synchronous).

    Request body (JSON-RPC 2.0 params):
        terminal_ip, terminal_port, use_https
    """
    body = request.json or {}
    params = body.get("params", {})
    result = pax_driver.initialize(params)
    return jsonify(jsonrpc="2.0", result=result)


@app.route("/hw_proxy/pax/transaction_execute", methods=["POST"])
def pax_transaction_execute():
    """
    Synchronous payment transaction — blocks until the PAX terminal responds.

    Designed to be called from the Odoo backend (pos_payment_pax module) so that
    the Odoo server acts as a relay and the browser never contacts pywebdriver
    directly.  One HTTP call in, one parsed result out.

    Request body (JSON-RPC 2.0 params → payment_info):
        terminal_ip, terminal_port, use_https, amount (float),
        currency_code, order_id, transaction_type, timeout (ms), terminal_id
    """
    body = request.json or {}
    payment_info = body.get("params", {}).get("payment_info", {})
    if isinstance(payment_info, str):
        payment_info = json.loads(payment_info)

    terminal_ip = payment_info.get("terminal_ip", "")
    terminal_port = int(payment_info.get("terminal_port") or 0)
    use_https = bool(payment_info.get("use_https", pax_driver._default_https))
    amount_cents = int(round(float(payment_info.get("amount", 0)) * 100))
    currency_code = payment_info.get("currency_code", "840")
    order_id = payment_info.get("order_id", "")
    transaction_type = payment_info.get("transaction_type", "01")
    timeout_ms = payment_info.get("timeout")
    timeout_s = int(timeout_ms) // 1000 if timeout_ms else pax_driver._default_timeout
    terminal_id = payment_info.get("terminal_id", "0")

    frame = build_pos_link_request(
        "T00",
        "1.28",
        transaction_type,
        str(amount_cents),
        "",  # tip
        "",  # cash back
        "",  # merchant fee
        "",  # tax
        "",  # fuel
        "",  # service fee
        "",  # surcharge
        "",  # discount
        "",  # original amount
        "",  # invoice number
        order_id,
        "",  # offline auth code
        "",  # original transaction id
        currency_code,
    )

    result = pax_driver._send_to_terminal(
        frame,
        terminal_ip=terminal_ip,
        terminal_port=terminal_port,
        use_https=use_https,
        timeout=timeout_s,
        terminal_id=terminal_id,
    )
    return jsonify(jsonrpc="2.0", result=result)


@app.route("/hw_proxy/pax/status", methods=["GET"])
def pax_status():
    """Return current PAX driver / terminal status."""
    terminal_id = request.args.get("terminal_id", "0")
    return jsonify(jsonrpc="2.0", result=pax_driver.get_status(terminal_id))
