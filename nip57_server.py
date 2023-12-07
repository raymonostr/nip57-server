import json
import logging
import os
import sys
import threading
import time
import urllib.parse

import requests
from flask import Flask
from flask import request
from flask_cors import CORS
from waitress import serve

from lnd_helper import LndHelper
from nostr_helper import NostrHelper

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="[%(asctime)s - %(levelname)s] %(message)s")
    logging.getLogger().setLevel(logging.INFO)

    app_logger = logging.getLogger("nip57Server")
    app = Flask("nip57Server")
    CORS(app)
    LNURL_ORIGIN = os.environ.get("LNURL_ORIGIN", "http://localhost:8080")
    SERVER_PORT = os.environ.get("SERVER_PORT", "8080")
    MIN_SENDABLE = os.environ.get("MIN_SENDABLE", 1000)
    MAX_SENDABLE = os.environ.get("MAX_SENDABLE", 1000000000)
    NIP57S_VERSION = "NIP57S V1.0.0"
    app_logger.debug("Loading file users.json")
    users_file = open('users.json')
    users: dict = json.load(users_file)
    users_file.close()
    app_logger.debug(f"Found {len(users)} users in users.json")
    nostr_helper: NostrHelper = NostrHelper(app_logger)
    lnd_helper: LndHelper = LndHelper(app_logger, nostr_helper)


    def cleanup_cron():
        time.sleep(113)  # whatever...
        lnd_helper.cleanup_invoice_cache()
        threading.Thread(target=cleanup_cron).start()


    @app.route('/.well-known/lnurlp/<string:username>')
    def lnurlp(username):
        app_logger.debug("got lnurlp request for: " + username)
        parsed_url = urllib.parse.urlparse(LNURL_ORIGIN)
        if users.get(username) is None:
            return {"status": "ERROR", "reason": "User unknown"}, 404
        return {
            "callback": f"{LNURL_ORIGIN}/lnurlp/invoice/{username}",
            "maxSendable": int(MAX_SENDABLE),
            "minSendable": int(MIN_SENDABLE),
            "metadata": [["text/identifier", username + "@" + parsed_url.netloc],
                         ["text/plain", "Sats for " + username]],
            "tag": "payRequest",
            "allowsNostr": True,
            "commentAllowed": 255,
            "status": "OK",
            "nostrPubkey": nostr_helper.get_zapper_hexpub(),
            "server_version": NIP57S_VERSION
        }


    @app.route('/lnurlp/state')
    def state():
        return lnd_helper.lnd_state()


    @app.route('/lnurlp/set_clearnet')
    def set_clearnet():
        app_logger.debug("got set_clearnet request")

        secret = request.args.get(key='secret', type=str)
        if secret is None:
            return {"status": "ERROR", "reason": "No secret given"}, 403

        ipv4 = request.args.get(key='ipv4', type=str)
        if ipv4 is None:
            return {"status": "ERROR", "reason": "No valid IP given"}, 400

        port = request.args.get(key='port', type=int)
        if port is None:
            port = lnd_helper.DYNIP_PORT

        tls_verify = request.args.get(key='tls_verify', type=str)
        if tls_verify is None:
            tls_verify = lnd_helper.TLS_VERIFY
        elif tls_verify.lower() == "false":
            requests.packages.urllib3.disable_warnings()
            tls_verify = False

        return lnd_helper.set_clearnet(ipv4=ipv4, secret=secret, port=port, tls_verify=tls_verify)


    @app.route('/lnurlp/invoice/<string:username>')
    def invoice(username):
        amount = request.args.get(key='amount', type=int)
        if amount is None:
            return {"status": "ERROR", "reason": "No valid amount given"}, 400

        app_logger.info(f"got invoice request for {username} amount {str(amount)} sats")

        nostr = request.args.get(key='nostr', type=str)
        if nostr is None:
            return {"status": "ERROR", "reason": "No valid nostr given"}, 400
        if not nostr_helper.check_9734_event(nostr, amount):
            return {"status": "ERROR", "reason": "nostr event is not a valid kind 9734"}, 400

        bech32_invoice = lnd_helper.fetch_invoice(amount, urllib.parse.unquote_plus(nostr))
        if bech32_invoice == "":
            return {"status": "ERROR", "reason": "LND did not provide an invoice"}, 500

        lnd_helper.cache_payment(bech32_invoice["add_index"], urllib.parse.unquote_plus(nostr))
        lnd_helper.start_invoice_listener()

        return {"status": "OK", "pr": bech32_invoice["payment_request"], "routes": []}


    app_logger.info(f"nip57_server {NIP57S_VERSION} starting on port " + str(SERVER_PORT))
    app_logger.info("author contact: nostr:npub1c3lf9hdmghe4l7xcy8phlhepr66hz7wp5dnkpwxjvw8x7hzh0pesc9mpv4")
    app_logger.info("GitHub: https://github.com/raymonostr/nip57-server")
    app_logger.info("This software is provided AS IS without any warranty. Use it at your own risk.")
    app_logger.info("Config LNURL_ORIGIN: " + str(LNURL_ORIGIN))
    app_logger.info("Config MIN_SENDABLE: " + str(MIN_SENDABLE))
    app_logger.info("Config MAX_SENDABLE: " + str(MAX_SENDABLE))
    app_logger.info("Config DEFAULT_RELAYS: " + str(nostr_helper.DEFAULT_RELAYS))
    app_logger.info("Config SOCKS5H_PROXY: " + str(lnd_helper.SOCKS5H_PROXY))
    app_logger.info("Config LND_RESTADDR: " + str(lnd_helper.LND_RESTADDR)[:16] + "...")
    app_logger.info("Config INVOICE_MACAROON: " + str(lnd_helper.INVOICE_MACAROON)[:14] + "...")
    app_logger.info("Config ZAPPER_KEY: " + str(nostr_helper.ZAPPER_KEY)[:14] + "...")
    app_logger.info("Config DYNIP_SECRET: " + str(lnd_helper.DYNIP_SECRET)[:3] + "...")
    app_logger.info("Config TLS_VERIFY: " + str(lnd_helper.TLS_VERIFY))

    threading.Thread(target=cleanup_cron).start()
    serve(app, host="0.0.0.0", port=SERVER_PORT)
