"""Handles requests for WebFinger endpoints.

https://webfinger.net/
https://tools.ietf.org/html/rfc7033
"""
import datetime
import logging
import re
import urllib.parse

from flask import g, render_template, request
from granary import as2
from oauth_dropins.webutil import flask_util, util
from oauth_dropins.webutil.flask_util import error, flash
from oauth_dropins.webutil.util import json_dumps, json_loads

import common
from flask_app import app, cache
from models import User
from protocol import Protocol
from web import Web

SUBSCRIBE_LINK_REL = 'http://ostatus.org/schema/1.0/subscribe'

logger = logging.getLogger(__name__)


class Webfinger(flask_util.XrdOrJrd):
    """Serves a user's WebFinger profile.

    Supports both JRD and XRD; defaults to JRD.
    https://tools.ietf.org/html/rfc7033#section-4
    """
    @flask_util.cached(cache, common.CACHE_TIME, headers=['Accept'])
    def dispatch_request(self, *args, **kwargs):
        return super().dispatch_request(*args, **kwargs)

    def template_prefix(self):
        return 'webfinger_user'

    def template_vars(self):
        logger.debug(f'Headers: {list(request.headers.items())}')

        resource = flask_util.get_required_param('resource').strip()
        resource = resource.removeprefix(common.host_url())

        # handle Bridgy Fed actor URLs, eg https://fed.brid.gy/snarfed.org
        host = util.domain_from_link(common.host_url())
        if resource in ('', '/', f'acct:{host}', f'acct:@{host}'):
            error('Expected other domain, not *.brid.gy')

        allow_indirect = False
        cls = None
        try:
            user, id = util.parse_acct_uri(resource)
            cls = Protocol.for_domain(id, fed=Web)
            if cls:
                id = user
                allow_indirect=True
        except ValueError:
            id = urllib.parse.urlparse(resource).netloc or resource

        if not cls:
            cls = Protocol.for_request(fed=Web)

        logger.info(f'Protocol {cls.__name__}, user id {id}')
        if cls.owns_id(id) is False:
            error(f'{id} is not a valid {cls.__name__} id')

        # only allow indirect users if this id is "on" a brid.gy subdomain,
        # eg user.com@bsky.brid.gy but not user.com@user.com
        if allow_indirect:
            g.user = cls.get_or_create(id)
        else:
            g.user = cls.get_by_id(id)
            if g.user and not g.user.direct:
                error(f"{g.user.key} hasn't signed up yet", status=404)

        if not g.user:
            error(f'No {cls.LABEL} user found for {id}', status=404)

        actor = g.user.to_as1() or {}
        logger.info(f'Generating WebFinger data for {g.user.key}')
        logger.info(f'AS1 actor: {actor}')
        urls = util.dedupe_urls(util.get_list(actor, 'urls') +
                                util.get_list(actor, 'url') +
                                [g.user.web_url()])
        logger.info(f'URLs: {urls}')
        canonical_url = urls[0]

        # generate webfinger content
        data = util.trim_nulls({
            'subject': 'acct:' + g.user.ap_address().lstrip('@'),
            'aliases': urls,
            'links':
            [{
                'rel': 'http://webfinger.net/rel/profile-page',
                'type': 'text/html',
                'href': url,
            } for url in urls if util.is_web(url)] +

            [{
                'rel': 'http://webfinger.net/rel/avatar',
                'href': url,
            } for url in util.get_urls(actor, 'image')] +

            [{
                'rel': 'canonical_uri',
                'type': 'text/html',
                'href': canonical_url,
            },

            # ActivityPub
            {
                'rel': 'self',
                'type': as2.CONTENT_TYPE,
                # WARNING: in python 2 sometimes request.host_url lost port,
                # http://localhost:8080 would become just http://localhost. no
                # clue how or why. pay attention here if that happens again.
                'href': g.user.ap_actor(),
            }, {
                # AP reads this and sharedInbox from the AS2 actor, not
                # webfinger, so strictly speaking, it's probably not needed here.
                'rel': 'inbox',
                'type': as2.CONTENT_TYPE,
                'href': g.user.ap_actor('inbox'),
            }, {
                # https://www.w3.org/TR/activitypub/#sharedInbox
                'rel': 'sharedInbox',
                'type': as2.CONTENT_TYPE,
                'href': common.host_url('/ap/sharedInbox'),
            },

            # remote follow
            # https://socialhub.activitypub.rocks/t/what-is-the-current-spec-for-remote-follow/2020/11?u=snarfed
            # https://github.com/snarfed/bridgy-fed/issues/60#issuecomment-1325589750
            {
                'rel': 'http://ostatus.org/schema/1.0/subscribe',
                # TODO: switch to:
                # 'template': common.host_url(g.user.user_page_path('?url={uri}')),
                # the problem is that user_page_path() uses readable_id, which uses
                # custom username instead of domain, which may not be unique
                'template': common.host_url(f'{cls.ABBREV}/{id}?url={{uri}}'),
            }]
        })

        logger.info(f'Returning WebFinger data: {json_dumps(data, indent=2)}')
        return data


class HostMeta(flask_util.XrdOrJrd):
    """Renders and serves the /.well-known/host-meta file.

    Supports both JRD and XRD; defaults to XRD.
    https://tools.ietf.org/html/rfc6415#section-3
    """
    DEFAULT_TYPE = flask_util.XrdOrJrd.XRD

    def template_prefix(self):
        return 'host-meta'

    def template_vars(self):
        return {'host_uri': common.host_url()}


@app.get('/.well-known/host-meta.xrds')
def host_meta_xrds():
    """Renders and serves the /.well-known/host-meta.xrds XRDS-Simple file."""
    return (render_template('host-meta.xrds', host_uri=common.host_url()),
            {'Content-Type': 'application/xrds+xml'})


def fetch(addr):
    """Fetches and returns an address's Webfinger data.

    On failure, flashes a message and returns None.

    TODO: unit tests. right now it's only tested indirectly, in test_follow.
    TODO: switch to raising exceptions instead of flashing messages and
    returning None

    Args:
      addr: str, a Webfinger-compatible address, eg @x@y, acct:x@y, or
        https://x/y

    Returns:
      dict, fetched Webfinger data, or None on error

    """
    addr = addr.strip().strip('@')
    split = addr.split('@')
    if len(split) == 2:
        addr_domain = split[1]
        resource = f'acct:{addr}'
    elif util.is_web(addr):
        addr_domain = util.domain_from_link(addr, minimize=False)
        resource = addr
    else:
        flash('Enter a fediverse address in @user@domain.social format')
        return None

    try:
        resp = util.requests_get(
            f'https://{addr_domain}/.well-known/webfinger?resource={resource}')
    except BaseException as e:
        if util.is_connection_failure(e):
            flash(f"Couldn't connect to {addr_domain}")
            return None
        raise

    if not resp.ok:
        flash(f'WebFinger on {addr_domain} returned HTTP {resp.status_code}')
        return None

    try:
        data = resp.json()
    except ValueError as e:
        logger.warning(f'Got {e}', exc_info=True)
        flash(f'WebFinger on {addr_domain} returned non-JSON')
        return None

    logger.info(f'Got: {json_dumps(data, indent=2)}')
    return data


app.add_url_rule('/.well-known/webfinger', view_func=Webfinger.as_view('webfinger'))
app.add_url_rule('/.well-known/host-meta', view_func=HostMeta.as_view('hostmeta'))
app.add_url_rule('/.well-known/host-meta.json', view_func=HostMeta.as_view('hostmeta-json'))
