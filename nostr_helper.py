import json
import logging
import os
import ssl
import urllib.parse
from unittest import TestCase

from nostr.event import Event
from nostr.key import PublicKey, PrivateKey
from nostr.relay import Relay
from nostr.relay_manager import RelayManager, RelayPolicy


class XRelay(Relay):
    _event: Event = None
    _logger: logging.Logger = None

    def set_on_open_event(self, event: Event, logger: logging.Logger):
        self._event = event
        self._logger = logger

    def _on_open(self, class_obj):
        self.connected = True
        if self._event is not None:
            msg = self._event.to_message()
            self._logger.debug("Publishing on " + self.url)
            self.publish(msg)
            self._logger.debug("Closing " + self.url)
            self.close()


class XRelayManager(RelayManager):
    def add_x_relay(self, url: str, event: Event, logger: logging.Logger):
        policy = RelayPolicy(True, True)
        relay = XRelay(url, policy, self.message_pool, {})
        relay.set_on_open_event(event, logger)
        self.relays[url] = relay


class NostrHelper:
    ZAPPER_KEY = os.environ.get("ZAPPER_KEY", "please set")
    DEFAULT_RELAYS = ["wss://nostr.mom/", "wss://nostr-pub.wellorder.net/", "wss://relay.damus.io/", "wss://nos.lol/"]

    _private_key: PrivateKey = PrivateKey(bytes.fromhex(ZAPPER_KEY))
    _public_key: PublicKey = _private_key.public_key

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _count_tag(self, tags: list[list[str]], tag: str) -> int:
        n = 0
        for inner_tags in tags:
            if inner_tags[0] == tag:
                n += 1
        return n

    def _get_tag(self, tags: list[list[str]], tag: str) -> list[str]:
        for inner_tags in tags:
            if inner_tags[0] == tag:
                return inner_tags
        return []

    def check_9734_event(self, nostr_json_encoded: str, amount: int) -> bool:
        """
        Check event for https://github.com/nostr-protocol/nips/blob/master/57.md App D
        :param amount: amount in msat
        :param nostr_json_encoded: Urlencoded kind 9734 event
        :return: true if event is valid, else false
        """
        try:
            nostr_json = urllib.parse.unquote_plus(nostr_json_encoded)
            nostr = json.loads(nostr_json)
        except ValueError:
            return False
        if (("kind" not in nostr) or ("tags" not in nostr) or ("sig" not in nostr)
                or ("pubkey" not in nostr) or ("id" not in nostr)):
            return False
        if nostr["kind"] != 9734:
            return False
        if self._count_tag(nostr["tags"], "p") != 1:
            return False
        if self._count_tag(nostr["tags"], "e") > 1:
            return False
        if self._count_tag(nostr["tags"], "amount") == 1:
            tag = self._get_tag(nostr["tags"], "amount")
            if int(tag[1]) != amount:
                return False
        pub_key = PublicKey(bytes.fromhex(nostr["pubkey"]))
        verified = pub_key.verify_signed_message_hash(nostr["id"], nostr["sig"])
        if not verified:
            return False

        return True

    def get_relays_from_9734(self, event_9734_json) -> list[str]:
        nostr_9734 = json.loads(event_9734_json)
        if self._count_tag(nostr_9734["tags"], "relays") != 1:
            return []
        relay_tag = self._get_tag(nostr_9734["tags"], "relays")
        del relay_tag[0]
        return relay_tag

    def add_default_relays(self, relays: list[str]):
        for r in self.DEFAULT_RELAYS:
            if r not in relays:
                relays.append(r)
        return relays

    def confirm_payment(self, idx, event_9734_json, lnd_invoice_json) -> bool:
        self._logger.info(f"Creating event kind 9735 for idx {idx}")
        self._logger.debug(f"Have 9734 Event: {event_9734_json}")
        self._logger.debug(f"Have LND invoice: {lnd_invoice_json}")
        nostr_9734 = json.loads(event_9734_json)
        lnd_invoice = json.loads(lnd_invoice_json)
        nostr_event_tags = [["description", event_9734_json], ["bolt11", lnd_invoice["payment_request"]],
                            self._get_tag(nostr_9734["tags"], "p")]
        if self._count_tag(nostr_9734["tags"], "e") == 1:
            nostr_event_tags.append(self._get_tag(nostr_9734["tags"], "e"))
        if self._count_tag(nostr_9734["tags"], "a") == 1:
            nostr_event_tags.append(self._get_tag(nostr_9734["tags"], "a"))
        nostr_event = Event(content="", kind=9735, public_key=self._public_key.hex(), tags=nostr_event_tags,
                            created_at=int(lnd_invoice["settle_date"]))
        self._private_key.sign_event(nostr_event)
        self._logger.debug(json.dumps(nostr_event.to_message()))
        relays = self.add_default_relays(self.get_relays_from_9734(event_9734_json))
        self.send_event_9735(relays, nostr_event)

        return True

    def send_event_9735(self, relays: list[str], event: Event):
        self._logger.info(f"Sending 9735 event to relays now")
        relay_manager = XRelayManager()
        for r in relays:
            relay_manager.add_x_relay(r, event, self._logger)
        relay_manager.open_connections({"cert_reqs": ssl.CERT_NONE}) 


if __name__ == '__main__':
    testlogger = logging.getLogger("Testcases")
    testlogger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    testlogger.addHandler(ch)
    tc = TestCase()
    helper = NostrHelper(testlogger)

    testlogger.info("Starting Tests")

    # Tests removed for github publishing