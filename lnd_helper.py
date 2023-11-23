import base64
import hashlib
import ipaddress
import json
import logging
import os
import threading
import time
from unittest import TestCase

import requests
from requests.exceptions import ChunkedEncodingError

from nostr_helper import NostrHelper


class LndHelper:
    SOCKS5H_PROXY = os.environ.get("SOCKS5H_PROXY", "socks5h://127.0.0.1:9152")
    LND_RESTADDR = os.environ.get("LND_RESTADDR",
                                  "please_set")
    INVOICE_MACAROON = os.environ.get("INVOICE_MACAROON",
                                      "please_set")
    DYNIP_SECRET = os.environ.get("DYNIP_SECRET", "")  # empty means function deactivated
    DYNIP_PORT = os.environ.get("DYNIP_PORT", "8080")
    TLS_VERIFY = os.environ.get("TLS_VERIFY", "./tls.cert")
    # secs until we remove a 9734 from mem cache
    CACHE_TIMEOUT = 120

    def __init__(self, logger: logging.Logger, nostr_helper: NostrHelper):
        self._invoice_cache = {}
        self._nostr_helper = nostr_helper
        self._listener_running = False
        self._logger = logger
        if self.TLS_VERIFY.lower() == "false":
            self.TLS_VERIFY = False

    def fetch_invoice(self, amount: int, nostr_event_9734: str):
        if not self._listener_running:
            self.start_invoice_listener()
        with requests.Session() as session:
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
                                    verify=self.TLS_VERIFY)
            self._logger.debug("LND response " + str(response.json()))
        if response.status_code != 200:
            self._logger.error("No 200 from lnd: ")
            self._logger.error(response.json())
            self._logger.error(response.headers)
            return ""

        return response.json()

    def cache_payment(self, idx, event_kind_9734_json):
        self._logger.info("caching open invoice" + idx)
        self._invoice_cache[idx] = {
            "timestamp": int(time.time()),
            "event": event_kind_9734_json,
            "idx": idx
        }
        self._logger.info("Invoice cache length is " + str(len(self._invoice_cache)))

    def lnd_state(self):
        url = self.LND_RESTADDR + '/v1/state'
        with requests.Session() as session:
            session.proxies = {'http': self.SOCKS5H_PROXY, 'https': self.SOCKS5H_PROXY}
            self._logger.debug("Requesting LND state")
            try:
                r = session.get(url, verify=self.TLS_VERIFY)
                return r.json()
            except requests.exceptions.ConnectionError:
                self._logger.error(f"LND connection error at {self.LND_RESTADDR}")
                return {"status": "ERROR", "reason": "LND unreachable"}, 500

    def _listen_for_invoices(self):
        url = self.LND_RESTADDR + '/v1/invoices/subscribe'
        session = requests.Session()
        session.proxies = {'http': self.SOCKS5H_PROXY, 'https': self.SOCKS5H_PROXY}
        headers = {'Grpc-Metadata-macaroon': self.INVOICE_MACAROON}
        self._logger.info("Sending invoice subscribe to LND")
        response = session.get(url, headers=headers, stream=True, verify=self.TLS_VERIFY)
        try:
            for raw_response in response.iter_lines():
                json_response = json.loads(raw_response)
                self._logger.debug(f"Got streamed from LND: {json_response}")
                if not self.post_process_payment(raw_response):
                    response.close()
                    self._listener_running = False
                    self._logger.info("LND invoice listener closed")
                    break
        except ChunkedEncodingError:
            self._logger.error("LND closed subscription")
            self._listener_running = False

    def start_invoice_listener(self):
        self._logger.info("Starting LND invoice listener")
        listener = threading.Thread(target=self._listen_for_invoices)
        listener.start()
        self._listener_running = True

    def post_process_payment(self, raw_result: str) -> bool:
        self._logger.debug("Processing LND input")
        if len(self._invoice_cache) == 0:
            self._logger.debug("No invoices in cache, closing subscription")
            return False
        result: dict = json.loads(raw_result)
        if "result" not in result:
            self._logger.error("Got unexpected whatever from lnd: " + str(result))
            return True
        invoice = result["result"]
        if "settled" not in invoice:
            self._logger.error("No 'settled' in invoice from lnd: " + str(invoice))
            return True
        if not invoice["settled"]:
            self._logger.debug("Ignoring unsettled invoice from lnd: " + str(invoice))
            return True
        if "add_index" not in invoice:
            self._logger.error("No 'add_index' in invoice from lnd: " + str(invoice))
            return True
        idx = invoice["add_index"]
        self._logger.debug("Checking for invoice idx: " + str(idx))
        # improve: Thread lock these ops on _invoice_cache
        if idx not in self._invoice_cache:
            self._logger.info("uncached 'add_index' in invoice from lnd: " + str(invoice))
            return True
        event = self._invoice_cache[idx]
        del self._invoice_cache[idx]
        self._nostr_helper.confirm_payment(idx, event['event'], json.dumps(invoice))
        if len(self._invoice_cache) == 0:
            return False
        return True

    def cleanup_invoice_cache(self):
        self._logger.debug(f"running cleanup_invoice_cache in thread {threading.get_native_id()}")
        self._logger.debug(f"{threading.active_count()} Threads active")
        self._logger.debug("Before: Invoice cache length is " + str(len(self._invoice_cache)))
        purge_time = int(time.time()) - self.CACHE_TIMEOUT
        drop_list = []
        for element in self._invoice_cache.values():
            if element['timestamp'] < purge_time:
                drop_list.append(element['idx'])
        for idx in drop_list:
            del self._invoice_cache[idx]
        self._logger.debug("After: Invoice cache length is " + str(len(self._invoice_cache)))

    def _validate_ip_address(self, ip: str) -> bool:
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    def set_clearnet(self, ipv4: str, secret: str, port: int, tls_verify):
        if self.DYNIP_SECRET == '':
            return {"status": "ERROR", "reason": "Feature not available"}, 403
        if self.DYNIP_SECRET != secret:
            return {"status": "ERROR", "reason": "Denied"}, 403
        if not self._validate_ip_address(ipv4):
            return {"status": "ERROR", "reason": "Denied"}, 403
        new_addr = f"https://{ipv4}:{port}"
        if new_addr != self.LND_RESTADDR:
            #  need a reconnect later
            self._listener_running = False

        self.LND_RESTADDR = new_addr
        self.TLS_VERIFY = tls_verify
        self.DYNIP_PORT = port
        self.SOCKS5H_PROXY = ""
        self._logger.info("LND Rest addr set to " + self.LND_RESTADDR)
        return {}, 204


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

    # Tests removed for GitHub publishing
