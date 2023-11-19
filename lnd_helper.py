import json
import logging
import os
import threading
import time
from unittest import TestCase

import requests
import hashlib
import base64
from nostr_helper import NostrHelper


class LndHelper:
    SOCKS5H_PROXY = os.environ.get("SOCKS5H_PROXY", "socks5h://127.0.0.1:9152")
    LND_RESTADDR = os.environ.get("LND_RESTADDR",
                                  "please_set")
    INVOICE_MACAROON = os.environ.get("INVOICE_MACAROON",
                                      "please_set")

    def __init__(self, logger: logging.Logger, nostr_helper: NostrHelper):
        self._invoice_cache = {}
        self._nostr_helper = nostr_helper
        self._listener_running = False
        self._logger = logger

    def fetch_invoice(self, amount: int, nostr_event_9734: str):
        if not self._listener_running:
            self.start_invoice_listener()
        session = requests.session()
        session.proxies = {'http': self.SOCKS5H_PROXY, 'https': self.SOCKS5H_PROXY}
        description = nostr_event_9734
        d_hash = hashlib.sha256(description.encode('UTF-8'))
        b64_d_hash = base64.b64encode(d_hash.digest())
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Grpc-Metadata-macaroon": self.INVOICE_MACAROON}
        data = {"value_msat": amount,
                "description_hash": b64_d_hash.decode("UTF-8")}
        json_data = json.dumps(data)
        self._logger.debug("Sending to LND: ")
        self._logger.debug(json_data)
        response = session.post(self.LND_RESTADDR + "/v1/invoices", headers=headers, data=json_data,
                                verify="./tls.cert")
        self._logger.debug("LND response " + str(response.json()))
        if response.status_code != 200:
            self._logger.error("No 200 from lnd: ")
            self._logger.error(response.json())
            self._logger.error(response.headers)
            return ""

        return response.json()

    def cache_payment(self, idx, event_kind_9734_json):
        self._logger.info("caching open invoice" + idx)
        self._invoice_cache[idx] = {"timestamp": time.time(), "event": event_kind_9734_json}
        self._logger.info("Invoice cache length is " + str(len(self._invoice_cache)))

    def lnd_state(self):
        url = self.LND_RESTADDR + '/v1/state'
        session = requests.session()
        session.proxies = {'http': self.SOCKS5H_PROXY, 'https': self.SOCKS5H_PROXY}
        self._logger.debug("Requesting LND state")
        try:
            r = session.get(url, verify="./tls.cert")
            return r.json()
        except ConnectionRefusedError:
            return {"status": "ERROR", "reason": "LND unreachable"}, 500

    def _listen_for_invoices(self):
        url = self.LND_RESTADDR + '/v1/invoices/subscribe'
        session = requests.session()
        session.proxies = {'http': self.SOCKS5H_PROXY, 'https': self.SOCKS5H_PROXY}
        headers = {'Grpc-Metadata-macaroon': self.INVOICE_MACAROON}
        self._logger.info("Sending invoice subscribe to LND")
        r = session.get(url, headers=headers, stream=True, verify="./tls.cert")
        for raw_response in r.iter_lines():
            json_response = json.loads(raw_response)
            self._logger.debug(f"Got streamed from LND: {json_response}")
            self.post_process_payment(raw_response)

    def start_invoice_listener(self):
        self._logger.info("Starting LND invoice listener")
        listener = threading.Thread(target=self._listen_for_invoices)
        listener.start()
        self._listener_running = True

    def post_process_payment(self, raw_result: str) -> bool:
        self._logger.debug("Processing LND input")
        result: dict = json.loads(raw_result)
        if "result" not in result:
            self._logger.error("Got unexpected whatever from lnd: " + str(result))
            return False
        invoice = result["result"]
        if "settled" not in invoice:
            self._logger.error("No 'settled' in invoice from lnd: " + str(invoice))
            return False
        if not invoice["settled"]:
            self._logger.debug("Ignoring unsettled invoice from lnd: " + str(invoice))
            return False
        if "add_index" not in invoice:
            self._logger.error("No 'add_index' in invoice from lnd: " + str(invoice))
            return False
        idx = invoice["add_index"]
        self._logger.debug("Checking for invoice idx: " + str(idx))
        # improve: Thread lock this ops on _invoice_cache
        if idx not in self._invoice_cache:
            self._logger.error("uncached 'add_index' in invoice from lnd: " + str(invoice))
            return False
        event = self._invoice_cache[idx]
        del self._invoice_cache[idx]
        return self._nostr_helper.confirm_payment(idx, event['event'], json.dumps(invoice))


if __name__ == '__main__':
    testlogger = logging.getLogger("Testcases")
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    testlogger.setLevel(logging.DEBUG)
    testlogger.addHandler(ch)
    tc = TestCase()
    helper = LndHelper(testlogger, NostrHelper(testlogger))

    testlogger.info("Starting Tests")

    # Tests removed for github publishing