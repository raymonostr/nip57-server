import json
import logging
import os
import sys
import urllib.parse

from flask import Flask
from flask import request
from flask_cors import CORS
from waitress import serve

from lnd_helper import LndHelper
from nostr_helper import NostrHelper

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                        format="[%(asctime)s - %(levelname)s] %(message)s")
    logging.getLogger().setLevel(logging.DEBUG)

    app_logger = logging.getLogger("nip57Server")
    app = Flask("nip57Server")
    CORS(app)
    LNURL_ORIGIN = os.environ.get("LNURL_ORIGIN", "http://localhost:8080")
    SERVER_PORT = os.environ.get("SERVER_PORT", "8080")
    MIN_SENDABLE = os.environ.get("MIN_SENDABLE", 1000)
    MAX_SENDABLE = os.environ.get("MAX_SENDABLE", 1000000000)
    NIP57S_VERSION = "NIP57S V0.1.1"
    app_logger.debug("Loading file users.json")
    users_file = open('users.json')
    users: dict = json.load(users_file)
    users_file.close()
    app_logger.debug(f"Found {len(users)} users in users.json")
    nostr_helper: NostrHelper = NostrHelper(app_logger)
    lnd_helper: LndHelper = LndHelper(app_logger, nostr_helper)


    @app.route('/.well-known/lnurlp/<string:username>')
    def lnurlp(username):
        app_logger.debug("got lnurlp request for: " + username)
        parsed_url = urllib.parse.urlparse(LNURL_ORIGIN)
        if users.get(username) is None:
            return {"status": "ERROR", "reason": "User unknown"}, 404
        return {
            "callback": f"{LNURL_ORIGIN}/lnurlp/invoice/{username}",
            "maxSendable": MAX_SENDABLE,
            "minSendable": MIN_SENDABLE,
            "metadata": [["text/identifier", username + "@" + parsed_url.netloc],
                         ["text/plain", "Sats for " + username]],
            "tag": "payRequest",
            "allowsNostr": True,
            "commentAllowed": 255,
            "status": "OK",
            "nostrPubkey": users.get(username),
            "server_version": NIP57S_VERSION
        }


    @app.route('/lnurlp/state')
    def state():
        return lnd_helper.lnd_state()


    @app.route('/lnurlp/invoice/<string:username>')
    def invoice(username):
        app_logger.info("got lnurlp request for: " + username)

        amount = request.args.get(key='amount', type=int)
        if amount is None:
            return {"status": "ERROR", "reason": "No valid amount given"}, 400

        nostr = request.args.get(key='nostr', type=str)
        if nostr is None:
            return {"status": "ERROR", "reason": "No valid nostr given"}, 400
        if not nostr_helper.check_9734_event(nostr, amount):
            return {"status": "ERROR", "reason": "nostr event is not a valid kind 9734"}, 400

        bech32_invoice = lnd_helper.fetch_invoice(amount, urllib.parse.unquote_plus(nostr))
        if bech32_invoice == "":
            return {"status": "ERROR", "reason": "LND did not provide an invoice"}, 500

        lnd_helper.cache_payment(bech32_invoice["add_index"], urllib.parse.unquote_plus(nostr))

        return {"status": "OK", "pr": bech32_invoice["payment_request"], "routes": []}


    app_logger.info(f"nip57_server {NIP57S_VERSION} starting on port " + str(SERVER_PORT))
    app_logger.info("author contact: nostr:npub1c3lf9hdmghe4l7xcy8phlhepr66hz7wp5dnkpwxjvw8x7hzh0pesc9mpv4")
    app_logger.info("GitHub: https://github.com/raymonostr/nip57-server")
    app_logger.info("Config LNURL_ORIGIN: " + str(LNURL_ORIGIN))
    app_logger.info("Config MIN_SENDABLE: " + str(MIN_SENDABLE))
    app_logger.info("Config MAX_SENDABLE: " + str(MAX_SENDABLE))
    app_logger.info("Config DEFAULT_RELAYS: " + str(nostr_helper.DEFAULT_RELAYS))
    app_logger.info("Config SOCKS5H_PROXY: " + str(lnd_helper.SOCKS5H_PROXY))
    app_logger.info("Config LND_RESTADDR: " + str(lnd_helper.LND_RESTADDR)[:16] + "...")
    app_logger.info("Config INVOICE_MACAROON: " + str(lnd_helper.INVOICE_MACAROON)[:14] + "...")
    app_logger.info("Config ZAPPER_KEY: " + str(nostr_helper.ZAPPER_KEY)[:14] + "...")

    serve(app, host="0.0.0.0", port=SERVER_PORT)
