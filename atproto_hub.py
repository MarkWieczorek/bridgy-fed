"""Single-instance hub for ATProto subscription (firehose) server and client."""
from functools import lru_cache
from ipaddress import ip_address, ip_network
import logging
import os
from pathlib import Path
import socket
import threading
from threading import Thread, Timer

import arroba.server
from arroba import xrpc_sync
from flask import Flask, render_template
import lexrpc.client
import lexrpc.flask_server
from oauth_dropins.webutil.appengine_info import DEBUG, LOCAL_SERVER
from oauth_dropins.webutil import appengine_config, flask_util
import pytz

# all protocols
import activitypub, atproto, web
import atproto_firehose
import common
import models

# as of 2024-07-10
BSKY_TEAM_CIDRS = (
    # https://discord.com/channels/1097580399187738645/1115973909624397855/1260356452162469969
    ip_network('209.249.133.120/29'),
    ip_network('108.179.139.0/24'),
    # https://github.com/bluesky-social/atproto/discussions/3036#discussioncomment-11431550
    ip_network('67.213.161.32/29')
)
BSKY_TEAM_HOSTS = (
    'zip.zayo.com',  # maybe? https://github.com/bluesky-social/atproto/discussions/3036#discussioncomment-11399854
)

# WARNING: when this is higher than 1, we start seeing ndb context exceptions,
# "ContextError: No current context," in _handle, even though it has an ndb context
# from handler. No clue why. They happen more often as the number of threads
# increases. Are ndb clients/contexts not thread safe?!
# https://github.com/snarfed/bridgy-fed/issues/1315
# https://console.cloud.google.com/errors/detail/CJrBqKnRzPfNRA;time=PT1H;refresh=true;locations=global?project=bridgy-federated
HANDLE_THREADS = 10

logger = logging.getLogger(__name__)

models.reset_protocol_properties()

# Flask app
app = Flask(__name__)
app.json.compact = False
app_dir = Path(__file__).parent
app.config.from_pyfile(app_dir / 'config.py')

app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client,
    # limited context-local cache. avoid full one due to this bug:
    # https://github.com/googleapis/python-ndb/issues/888
    cache_policy=common.cache_policy,
    global_cache=common.global_cache,
    global_cache_policy=common.global_cache_policy,
    global_cache_timeout_policy=common.global_cache_timeout_policy)


@app.get('/liveness_check')
@app.get('/readiness_check')
def health_check():
    """App Engine Flex health checks.

    https://cloud.google.com/appengine/docs/flexible/reference/app-yaml?tab=python#updated_health_checks
    """
    return 'OK'


# ATProto XRPC server
lexrpc.flask_server.init_flask(arroba.server.server, app)


@app.post('/queue/atproto-commit')
@flask_util.cloud_tasks_only
def atproto_commit():
    """Handler for atproto-commit tasks.

    Triggers `subscribeRepos` to check for new commits.
    """
    xrpc_sync.send_events()
    return 'OK'


@lru_cache
def gethostbyaddr(addr):
    """Wrapper for :func:`socket.gethostbyaddr` that caches the result."""
    for subnet in BSKY_TEAM_CIDRS:
        if ip_address(addr) in subnet:
            return 'bsky'

    try:
        return socket.gethostbyaddr(addr)[0]
    except socket.herror:
        return None


@app.get('/admin/atproto')
def atproto_admin():
    return render_template(
        'atproto.html',
        subscribers=lexrpc.flask_server.subscribers,
        gethostbyaddr=gethostbyaddr,
        pytz=pytz,
    )



# start firehose consumer threads
if LOCAL_SERVER or not DEBUG:
    threads = [t.name for t in threading.enumerate()]
    assert 'atproto_firehose.subscriber' not in threads
    assert 'atproto_firehose.handler' not in threads

    Thread(target=atproto_firehose.subscriber, name='atproto_firehose.subscriber').start()
    for i in range(HANDLE_THREADS):
        Thread(target=atproto_firehose.handler, name=f'atproto_firehose.handler-{i}').start()


# send requestCrawl to relay every 5m.
# delay 15s at startup because we're not up and serving XRPCs at this point yet.
# not sure why not.
if 'GAE_INSTANCE' in os.environ:  # prod
    def request_crawl():
        bgs = lexrpc.client.Client(f'https://{os.environ["BGS_HOST"]}',
                                   headers={'User-Agent': common.USER_AGENT})
        resp = bgs.com.atproto.sync.requestCrawl({'hostname': os.environ['PDS_HOST']})
        logger.info(resp)
        Timer(5 * 60, request_crawl).start()

    Timer(15, request_crawl).start()
    logger.info('Will send relay requestCrawl in 15s')
