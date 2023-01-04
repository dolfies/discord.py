"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
import json
import logging
from random import choice, choices
import string
from typing import (
    Any,
    ClassVar,
    Coroutine,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    overload,
    Sequence,
    TYPE_CHECKING,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import quote as _uriquote
import weakref

import aiohttp

from .enums import RelationshipAction, InviteType
from .errors import HTTPException, Forbidden, NotFound, LoginFailure, DiscordServerError, CaptchaRequired
from .file import File
from .tracking import ContextProperties
from . import utils
from .mentions import AllowedMentions
from .utils import MISSING

CAPTCHA_VALUES = {
    'incorrect-captcha',
    'response-already-used',
    'captcha-required',
    'invalid-input-response',
    'invalid-response',
    'You need to update your app',  # Discord moment
}
_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing_extensions import Self

    from .channel import TextChannel, DMChannel, GroupChannel, PartialMessageable, VoiceChannel, ForumChannel
    from .handlers import CaptchaHandler
    from .threads import Thread
    from .file import File
    from .mentions import AllowedMentions
    from .message import Attachment, Message
    from .flags import MessageFlags
    from .enums import AuditLogAction, ChannelType, InteractionType
    from .embeds import Embed

    from .types import (
        appinfo,
        audit_log,
        channel,
        emoji,
        guild,
        integration,
        invite,
        member,
        message,
        template,
        role,
        user,
        webhook,
        widget,
        team,
        threads,
        scheduled_event,
        subscriptions,
        sticker,
        welcome_screen,
    )
    from .types.snowflake import Snowflake, SnowflakeList

    from types import TracebackType

    T = TypeVar('T')
    BE = TypeVar('BE', bound=BaseException)
    Response = Coroutine[Any, Any, T]
    MessageableChannel = Union[TextChannel, Thread, DMChannel, GroupChannel, PartialMessageable, VoiceChannel, ForumChannel]


async def json_or_text(response: aiohttp.ClientResponse) -> Union[Dict[str, Any], str]:
    text = await response.text(encoding='utf-8')
    try:
        if response.headers['content-type'] == 'application/json':
            return utils._from_json(text)
    except KeyError:
        # Thanks Cloudflare
        pass

    return text


class MultipartParameters(NamedTuple):
    payload: Optional[Dict[str, Any]]
    multipart: Optional[List[Dict[str, Any]]]
    files: Optional[Sequence[File]]

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BE]],
        exc: Optional[BE],
        traceback: Optional[TracebackType],
    ) -> None:
        if self.files:
            for file in self.files:
                file.close()


def handle_message_parameters(
    content: Optional[str] = MISSING,
    *,
    username: str = MISSING,
    avatar_url: Any = MISSING,
    tts: bool = False,
    nonce: Optional[Union[int, str]] = MISSING,
    flags: MessageFlags = MISSING,
    file: File = MISSING,
    files: Sequence[File] = MISSING,
    embed: Optional[Embed] = MISSING,
    embeds: Sequence[Embed] = MISSING,
    attachments: Sequence[Union[Attachment, File]] = MISSING,
    allowed_mentions: Optional[AllowedMentions] = MISSING,
    message_reference: Optional[message.MessageReference] = MISSING,
    stickers: Optional[SnowflakeList] = MISSING,
    previous_allowed_mentions: Optional[AllowedMentions] = None,
    mention_author: Optional[bool] = None,
    extras: Dict[str, Any] = MISSING,
) -> MultipartParameters:
    if files is not MISSING and file is not MISSING:
        raise TypeError('Cannot mix file and files keyword arguments.')
    if embeds is not MISSING and embed is not MISSING:
        raise TypeError('Cannot mix embed and embeds keyword arguments.')

    if file is not MISSING:
        files = [file]

    if attachments is not MISSING and files is not MISSING:
        raise TypeError('Cannot mix attachments and files keyword arguments.')

    payload: Any = {'tts': tts}
    if embeds is not MISSING:
        if len(embeds) > 10:
            raise ValueError('embeds has a maximum of 10 elements.')
        payload['embeds'] = [e.to_dict() for e in embeds]

    if embed is not MISSING:
        if embed is None:
            payload['embeds'] = []
        else:
            payload['embeds'] = [embed.to_dict()]

    if content is not MISSING:
        if content is not None:
            payload['content'] = str(content)
        else:
            payload['content'] = None

    if nonce is MISSING:
        payload['nonce'] = utils._generate_nonce()
    elif nonce:
        payload['nonce'] = nonce

    if message_reference is not MISSING:
        payload['message_reference'] = message_reference

    if stickers is not MISSING:
        if stickers is not None:
            payload['sticker_ids'] = stickers
        else:
            payload['sticker_ids'] = []

    if avatar_url:
        payload['avatar_url'] = str(avatar_url)
    if username:
        payload['username'] = username

    if flags is not MISSING:
        payload['flags'] = flags.value

    if allowed_mentions:
        if previous_allowed_mentions is not None:
            payload['allowed_mentions'] = previous_allowed_mentions.merge(allowed_mentions).to_dict()
        else:
            payload['allowed_mentions'] = allowed_mentions.to_dict()
    elif previous_allowed_mentions is not None:
        payload['allowed_mentions'] = previous_allowed_mentions.to_dict()

    if mention_author is not None:
        if 'allowed_mentions' not in payload:
            payload['allowed_mentions'] = AllowedMentions().to_dict()
        payload['allowed_mentions']['replied_user'] = mention_author

    if attachments is MISSING:
        attachments = files
    else:
        files = [a for a in attachments if isinstance(a, File)]

    if attachments is not MISSING:
        file_index = 0
        attachments_payload = []
        for attachment in attachments:
            if isinstance(attachment, File):
                attachments_payload.append(attachment.to_dict(file_index))
                file_index += 1
            else:
                attachments_payload.append(attachment.to_dict())

        payload['attachments'] = attachments_payload

    if extras is not MISSING:
        payload.update(extras)

    multipart = []
    if files:
        multipart.append({'name': 'payload_json', 'value': utils._to_json(payload)})
        payload = None
        for index, file in enumerate(files):
            multipart.append(
                {
                    'name': f'files[{index}]',
                    'value': file.fp,
                    'filename': file.filename,
                    'content_type': 'application/octet-stream',
                }
            )

    return MultipartParameters(payload=payload, multipart=multipart, files=files)


def _gen_accept_encoding_header():
    return 'gzip, deflate, br' if aiohttp.http_parser.HAS_BROTLI else 'gzip, deflate'  # type: ignore


class Route:
    BASE: ClassVar[str] = 'https://discord.com/api/v9'

    def __init__(self, method: str, path: str, **parameters: Any) -> None:
        self.path: str = path
        self.method: str = method
        url = self.BASE + self.path
        if parameters:
            url = url.format_map({k: _uriquote(v) if isinstance(v, str) else v for k, v in parameters.items()})
        self.url: str = url

        # Major parameters
        self.channel_id: Optional[Snowflake] = parameters.get('channel_id')
        self.guild_id: Optional[Snowflake] = parameters.get('guild_id')
        self.webhook_id: Optional[Snowflake] = parameters.get('webhook_id')
        self.webhook_token: Optional[str] = parameters.get('webhook_token')

    @property
    def bucket(self) -> str:
        # TODO: Implement buckets :(
        return f'{self.channel_id}:{self.guild_id}:{self.path}'


class MaybeUnlock:
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock: asyncio.Lock = lock
        self._unlock: bool = True

    def __enter__(self) -> Self:
        return self

    def defer(self) -> None:
        self._unlock = False

    def __exit__(
        self,
        exc_type: Optional[Type[BE]],
        exc: Optional[BE],
        traceback: Optional[TracebackType],
    ) -> None:
        if self._unlock:
            self.lock.release()


# For some reason, the Discord voice websocket expects this header to be
# completely lowercase while aiohttp respects spec and does it as case-insensitive
aiohttp.hdrs.WEBSOCKET = 'websocket'  # type: ignore
# Support brotli if installed
aiohttp.client_reqrep.ClientRequest.DEFAULT_HEADERS[aiohttp.hdrs.ACCEPT_ENCODING] = _gen_accept_encoding_header()  # type: ignore


class _FakeResponse:
    def __init__(self, reason: str, status: int) -> None:
        self.reason = reason
        self.status = status


class HTTPClient:
    """Represents an HTTP client sending HTTP requests to the Discord API."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        connector: Optional[aiohttp.BaseConnector] = None,
        *,
        proxy: Optional[str] = None,
        proxy_auth: Optional[aiohttp.BasicAuth] = None,
        unsync_clock: bool = True,
        http_trace: Optional[aiohttp.TraceConfig] = None,
        captcha_handler: Optional[CaptchaHandler] = None,
    ) -> None:
        self.loop: asyncio.AbstractEventLoop = loop
        self.connector: aiohttp.BaseConnector = connector or MISSING
        self.__session: aiohttp.ClientSession = MISSING
        self._locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._global_over: asyncio.Event = MISSING
        self.token: Optional[str] = None
        self.ack_token: Optional[str] = None
        self.proxy: Optional[str] = proxy
        self.proxy_auth: Optional[aiohttp.BasicAuth] = proxy_auth
        self.http_trace: Optional[aiohttp.TraceConfig] = http_trace
        self.use_clock: bool = not unsync_clock
        self.captcha_handler: Optional[CaptchaHandler] = captcha_handler

        self.user_agent: str = MISSING
        self.super_properties: Dict[str, Any] = {}
        self.encoded_super_properties: str = MISSING
        self._started: bool = False

    def __del__(self) -> None:
        session = self.__session
        if session:
            try:
                session.connector._close()  # type: ignore # Handled below
            except AttributeError:
                pass

    async def startup(self) -> None:
        if self._started:
            return

        self.__session = session = aiohttp.ClientSession(
            connector=self.connector,
            loop=self.loop,
            trace_configs=None if self.http_trace is None else [self.http_trace],
        )
        self.user_agent, self.browser_version, self.client_build_number = ua, bv, bn = await utils._get_info(session)
        _log.info('Found user agent %s (%s), build number %s.', ua, bv, bn)
        self.super_properties = sp = {
            'os': 'Windows',
            'browser': 'Chrome',
            'device': '',
            'browser_user_agent': ua,
            'browser_version': bv,
            'os_version': '10',
            'referrer': '',
            'referring_domain': '',
            'referrer_current': '',
            'referring_domain_current': '',
            'release_channel': 'stable',
            'system_locale': 'en-US',
            'client_build_number': bn,
            'client_event_source': None,
        }
        self.encoded_super_properties = b64encode(json.dumps(sp).encode()).decode('utf-8')

        if self.captcha_handler is not None:
            await self.captcha_handler.startup()

        self._started = True

    async def ws_connect(
        self, url: str, *, compress: int = 0, host: Optional[str] = None
    ) -> aiohttp.ClientWebSocketResponse:
        if not host:
            host = url[6:].split('?')[0].rstrip('/')  # Removes 'wss://' and the query params

        kwargs: Dict[str, Any] = {
            'proxy_auth': self.proxy_auth,
            'proxy': self.proxy,
            'max_msg_size': 0,
            'timeout': 30.0,
            'autoclose': False,
            'headers': {
                'Accept-Language': 'en-US',
                'Cache-Control': 'no-cache',
                'Connection': 'Upgrade',
                'Host': host,
                'Origin': 'https://discord.com',
                'Pragma': 'no-cache',
                'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
                'User-Agent': self.user_agent,
            },
            'compress': compress,
        }

        return await self.__session.ws_connect(url, **kwargs)

    async def request(
        self,
        route: Route,
        *,
        files: Optional[Sequence[File]] = None,
        form: Optional[Iterable[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Any:
        bucket = route.bucket
        method = route.method
        url = route.url
        captcha_handler = self.captcha_handler

        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            if bucket is not None:
                self._locks[bucket] = lock

        # Header creation
        headers = {
            'Accept-Language': 'en-US',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Origin': 'https://discord.com',
            'Pragma': 'no-cache',
            'Referer': 'https://discord.com/channels/@me',
            'Sec-CH-UA': '"Google Chrome";v="{0}", "Chromium";v="{0}", ";Not A Brand";v="99"'.format(
                self.browser_version.split('.')[0]
            ),
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': self.user_agent,
            'X-Discord-Locale': 'en-US',
            'X-Debug-Options': 'bugReporterEnabled',
            'X-Super-Properties': self.encoded_super_properties,
        }

        # Header modification
        if self.token is not None and kwargs.get('auth', True):
            headers['Authorization'] = self.token

        reason = kwargs.pop('reason', None)
        if reason:
            headers['X-Audit-Log-Reason'] = _uriquote(reason)

        if (payload := kwargs.pop('json', None)) is not None:
            headers['Content-Type'] = 'application/json'
            kwargs['data'] = utils._to_json(payload)

        if 'context_properties' in kwargs:
            props = kwargs.pop('context_properties')
            if isinstance(props, ContextProperties):
                headers['X-Context-Properties'] = props.value

        if kwargs.pop('super_properties_to_track', False):
            headers['X-Track'] = headers.pop('X-Super-Properties')

        kwargs['headers'] = headers

        # Proxy support
        if self.proxy is not None:
            kwargs['proxy'] = self.proxy
        if self.proxy_auth is not None:
            kwargs['proxy_auth'] = self.proxy_auth

        if not self._global_over.is_set():
            await self._global_over.wait()

        response: Optional[aiohttp.ClientResponse] = None
        data: Optional[Union[Dict[str, Any], str]] = None
        await lock.acquire()
        with MaybeUnlock(lock) as maybe_lock:
            for tries in range(5):
                if files:
                    for f in files:
                        f.reset(seek=tries)

                if form:
                    # With quote_fields=True '[' and ']' in file field names are escaped, which Discord does not support
                    form_data = aiohttp.FormData(quote_fields=False)
                    for params in form:
                        form_data.add_field(**params)
                    kwargs['data'] = form_data

                try:
                    async with self.__session.request(method, url, **kwargs) as response:
                        _log.debug('%s %s with %s has returned %s.', method, url, kwargs.get('data'), response.status)
                        data = await json_or_text(response)

                        # Check if we have rate limit information
                        remaining = response.headers.get('X-Ratelimit-Remaining')
                        if remaining == '0' and response.status != 429:
                            # We've depleted our current bucket
                            delta = utils._parse_ratelimit_header(response, use_clock=self.use_clock)
                            _log.debug('A rate limit bucket has been exhausted (bucket: %s, retry: %s).', bucket, delta)
                            maybe_lock.defer()
                            self.loop.call_later(delta, lock.release)

                        # Request was successful so just return the text/json
                        if 300 > response.status >= 200:
                            _log.debug('%s %s has received %s.', method, url, data)
                            return data

                        # Rate limited
                        if response.status == 429:
                            if not response.headers.get('Via') or isinstance(data, str):
                                # Banned by Cloudflare more than likely.
                                raise HTTPException(response, data)

                            fmt = 'We are being rate limited. Retrying in %.2f seconds. Handled under the bucket "%s".'

                            # Sleep a bit
                            retry_after: float = data['retry_after']
                            _log.warning(fmt, retry_after, bucket)

                            # Check if it's a global rate limit
                            is_global = data.get('global', False)
                            if is_global:
                                _log.warning('Global rate limit has been hit. Retrying in %.2f seconds.', retry_after)
                                self._global_over.clear()

                            await asyncio.sleep(retry_after)
                            _log.debug('Done sleeping for the rate limit. Retrying...')

                            # Release the global lock now that the rate limit passed
                            if is_global:
                                self._global_over.set()
                                _log.debug('Global rate limit is now over.')

                            continue

                        # Unconditional retry
                        if response.status in {500, 502, 504}:
                            await asyncio.sleep(1 + tries * 2)
                            continue

                        # Usual error cases
                        if response.status == 403:
                            raise Forbidden(response, data)
                        elif response.status == 404:
                            raise NotFound(response, data)
                        elif response.status >= 500:
                            raise DiscordServerError(response, data)
                        else:
                            if 'captcha_key' in data:
                                raise CaptchaRequired(response, data)  # type: ignore # Should not be text at this point
                            raise HTTPException(response, data)

                # This is handling exceptions from the request
                except OSError as e:
                    # Connection reset by peer
                    if tries < 4 and e.errno in (54, 10054):
                        await asyncio.sleep(1 + tries * 2)
                        continue
                    raise

                # Captcha handling
                except CaptchaRequired as e:
                    values = [i for i in e.json['captcha_key'] if any(value in i for value in CAPTCHA_VALUES)]
                    if captcha_handler is None or tries == 4:
                        raise
                    elif not values:
                        raise
                    else:
                        previous = payload or {}
                        previous['captcha_key'] = await captcha_handler.fetch_token(e.json, self.proxy, self.proxy_auth)
                        if (rqtoken := e.json.get('captcha_rqtoken')) is not None:
                            previous['captcha_rqtoken'] = rqtoken
                        if 'nonce' in previous:
                            previous['nonce'] = utils._generate_nonce()
                        kwargs['headers']['Content-Type'] = 'application/json'
                        kwargs['data'] = utils._to_json(previous)

            if response is not None:
                # We've run out of retries, raise
                if response.status >= 500:
                    raise DiscordServerError(response, data)

                raise HTTPException(response, data)

            raise RuntimeError('Unreachable code in HTTP handling')

    async def get_from_cdn(self, url: str) -> bytes:
        async with self.__session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
            elif resp.status == 404:
                raise NotFound(resp, 'asset not found')
            elif resp.status == 403:
                raise Forbidden(resp, 'cannot retrieve asset')
            else:
                raise HTTPException(resp, 'failed to get asset')

    async def upload_to_cloud(self, url: str, file: Union[File, str], hash: Optional[str] = None) -> Any:
        response: Optional[aiohttp.ClientResponse] = None
        data: Optional[Union[Dict[str, Any], str]] = None

        # aiohttp helpfully sets the content type for us,
        # but Google explodes if we do that; therefore, empty string
        headers = {'Content-Type': ''}
        if hash:
            headers['Content-MD5'] = hash

        for tries in range(5):
            if isinstance(file, File):
                file.reset(seek=tries)

            try:
                async with self.__session.put(url, data=getattr(file, 'fp', file), headers=headers) as response:
                    _log.debug('PUT %s with %s has returned %s.', url, file, response.status)
                    data = await json_or_text(response)

                    # Request was successful so just return the text/json
                    if 300 > response.status >= 200:
                        _log.debug('PUT %s has received %s.', url, data)
                        return data

                    # Unconditional retry
                    if response.status in {500, 502, 504}:
                        await asyncio.sleep(1 + tries * 2)
                        continue

                    # Usual error cases
                    if response.status == 403:
                        raise Forbidden(response, data)
                    elif response.status == 404:
                        raise NotFound(response, data)
                    elif response.status >= 500:
                        raise DiscordServerError(response, data)
                    else:
                        raise HTTPException(response, data)
            except OSError as e:
                # Connection reset by peer
                if tries < 4 and e.errno in (54, 10054):
                    await asyncio.sleep(1 + tries * 2)
                    continue
                raise

        if response is not None:
            # We've run out of retries, raise
            if response.status >= 500:
                raise DiscordServerError(response, data)

            raise HTTPException(response, data)

    # State management

    def recreate(self) -> None:
        if self.__session and self.__session.closed:
            self.__session = aiohttp.ClientSession(
                connector=self.connector,
                loop=self.loop,
                trace_configs=None if self.http_trace is None else [self.http_trace],
            )

    async def close(self) -> None:
        if self.__session:
            await self.__session.close()

    # Login management

    def _token(self, token: str) -> None:
        self.token = token
        self.ack_token = None

    def get_me(self, with_analytics_token: bool = True) -> Response[user.User]:
        params = {'with_analytics_token': str(with_analytics_token).lower()}
        return self.request(Route('GET', '/users/@me'), params=params)

    async def static_login(self, token: str) -> user.User:
        old_token, self.token = self.token, token

        if self.connector is MISSING:
            self.connector = aiohttp.TCPConnector(loop=self.loop, limit=0)

        self._global_over = asyncio.Event()
        self._global_over.set()

        await self.startup()

        try:
            data = await self.get_me()
        except HTTPException as exc:
            self.token = old_token
            if exc.status == 401:
                raise LoginFailure('Improper token has been passed') from exc
            raise

        return data

    # PM functionality

    def start_group(self, recipients: SnowflakeList) -> Response[channel.GroupDMChannel]:
        payload = {
            'recipients': recipients,
        }
        props = ContextProperties._from_new_group_dm()  # New Group DM button

        return self.request(Route('POST', '/users/@me/channels'), json=payload, context_properties=props)

    def add_group_recipient(self, channel_id: Snowflake, user_id: Snowflake):  # TODO: return typings
        r = Route('PUT', '/channels/{channel_id}/recipients/{user_id}', channel_id=channel_id, user_id=user_id)
        return self.request(r)

    def remove_group_recipient(self, channel_id: Snowflake, user_id: Snowflake):  # TODO: return typings
        r = Route('DELETE', '/channels/{channel_id}/recipients/{user_id}', channel_id=channel_id, user_id=user_id)
        return self.request(r)

    def get_private_channels(self) -> Response[List[Union[channel.DMChannel, channel.GroupDMChannel]]]:
        return self.request(Route('GET', '/users/@me/channels'))

    def start_private_message(self, user_id: Snowflake) -> Response[channel.DMChannel]:
        payload = {
            'recipients': [user_id],
        }
        props = ContextProperties._empty()  # {}

        return self.request(Route('POST', '/users/@me/channels'), json=payload, context_properties=props)

    def accept_message_request(self, channel_id: Snowflake) -> Response[channel.DMChannel]:
        payload = {
            'consent_status': 2,
        }

        return self.request(Route('PUT', '/channels/{channel_id}/recipients/@me', channel_id=channel_id), json=payload)

    def decline_message_request(self, channel_id: Snowflake) -> Response[channel.DMChannel]:
        return self.request(Route('DELETE', '/channels/{channel_id}/recipients/@me', channel_id=channel_id))

    def mark_message_request(self, channel_id: Snowflake) -> Response[channel.DMChannel]:
        payload = {
            'consent_status': 1,
        }

        return self.request(Route('PUT', '/channels/{channel_id}/recipients/@me', channel_id=channel_id), json=payload)

    def reset_message_request(self, channel_id: Snowflake) -> Response[channel.DMChannel]:
        payload = {
            'consent_status': 0,
        }

        return self.request(Route('PUT', '/channels/{channel_id}/recipients/@me', channel_id=channel_id), json=payload)

    # Message management

    def send_message(
        self,
        channel_id: Snowflake,
        *,
        params: MultipartParameters,
    ) -> Response[message.Message]:
        r = Route('POST', '/channels/{channel_id}/messages', channel_id=channel_id)
        if params.files:
            return self.request(r, files=params.files, form=params.multipart)
        else:
            return self.request(r, json=params.payload)

    def send_typing(self, channel_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/channels/{channel_id}/typing', channel_id=channel_id))

    async def ack_message(self, channel_id: Snowflake, message_id: Snowflake):  # TODO: response type (simple)
        r = Route('POST', '/channels/{channel_id}/messages/{message_id}/ack', channel_id=channel_id, message_id=message_id)
        payload = {'token': self.ack_token}

        data = await self.request(r, json=payload)
        self.ack_token = data['token']

    def unack_message(self, channel_id: Snowflake, message_id: Snowflake, *, mention_count: int = 0) -> Response[None]:
        r = Route('POST', '/channels/{channel_id}/messages/{message_id}/ack', channel_id=channel_id, message_id=message_id)
        payload = {'manual': True, 'mention_count': mention_count}

        return self.request(r, json=payload)

    def ack_messages(self, read_states) -> Response[None]:  # TODO: type and implement
        payload = {'read_states': read_states}

        return self.request(Route('POST', '/read-states/ack-bulk'), json=payload)

    def ack_guild(self, guild_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/guilds/{guild_id}/ack', guild_id=guild_id))

    def unack_something(self, channel_id: Snowflake) -> Response[None]:  # TODO: research
        return self.request(Route('DELETE', '/channels/{channel_id}/messages/ack', channel_id=channel_id))

    def delete_message(
        self, channel_id: Snowflake, message_id: Snowflake, *, reason: Optional[str] = None
    ) -> Response[None]:
        r = Route('DELETE', '/channels/{channel_id}/messages/{message_id}', channel_id=channel_id, message_id=message_id)
        return self.request(r, reason=reason)

    def edit_message(
        self, channel_id: Snowflake, message_id: Snowflake, *, params: MultipartParameters
    ) -> Response[message.Message]:
        r = Route('PATCH', '/channels/{channel_id}/messages/{message_id}', channel_id=channel_id, message_id=message_id)
        if params.files:
            return self.request(r, files=params.files, form=params.multipart)
        else:
            return self.request(r, json=params.payload)

    def add_reaction(self, channel_id: Snowflake, message_id: Snowflake, emoji: str) -> Response[None]:
        r = Route(
            'PUT',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me',
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )
        return self.request(r)

    def remove_reaction(
        self, channel_id: Snowflake, message_id: Snowflake, emoji: str, member_id: Snowflake
    ) -> Response[None]:
        r = Route(
            'DELETE',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/{member_id}',
            channel_id=channel_id,
            message_id=message_id,
            member_id=member_id,
            emoji=emoji,
        )
        return self.request(r)

    def remove_own_reaction(self, channel_id: Snowflake, message_id: Snowflake, emoji: str) -> Response[None]:
        r = Route(
            'DELETE',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me',
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )
        return self.request(r)

    def get_reaction_users(
        self,
        channel_id: Snowflake,
        message_id: Snowflake,
        emoji: str,
        limit: int,
        after: Optional[Snowflake] = None,
    ) -> Response[List[user.User]]:
        r = Route(
            'GET',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}',
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )
        params: Dict[str, Any] = {
            'limit': limit,
        }
        if after:
            params['after'] = after

        return self.request(r, params=params)

    def clear_reactions(self, channel_id: Snowflake, message_id: Snowflake) -> Response[None]:
        r = Route(
            'DELETE',
            '/channels/{channel_id}/messages/{message_id}/reactions',
            channel_id=channel_id,
            message_id=message_id,
        )
        return self.request(r)

    def clear_single_reaction(self, channel_id: Snowflake, message_id: Snowflake, emoji: str) -> Response[None]:
        r = Route(
            'DELETE',
            '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}',
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )
        return self.request(r)

    async def get_message(self, channel_id: Snowflake, message_id: Snowflake) -> message.Message:
        data = await self.logs_from(channel_id, 1, around=message_id)

        try:
            msg = data[0]
        except IndexError:
            raise NotFound(_FakeResponse('Not Found', 404), 'message not found')  # type: ignore # _FakeResponse is not a real response
        if int(msg['id']) != message_id:
            raise NotFound(_FakeResponse('Not Found', 404), 'message not found')  # type: ignore # _FakeResponse is not a real Response

        return msg

    def get_channel(self, channel_id: Snowflake) -> Response[channel.Channel]:
        return self.request(Route('GET', '/channels/{channel_id}', channel_id=channel_id))

    def logs_from(
        self,
        channel_id: Snowflake,
        limit: int,
        before: Optional[Snowflake] = None,
        after: Optional[Snowflake] = None,
        around: Optional[Snowflake] = None,
    ) -> Response[List[message.Message]]:
        params: Dict[str, Any] = {
            'limit': limit,
        }
        if before is not None:
            params['before'] = before
        if after is not None:
            params['after'] = after
        if around is not None:
            params['around'] = around

        return self.request(Route('GET', '/channels/{channel_id}/messages', channel_id=channel_id), params=params)

    def publish_message(self, channel_id: Snowflake, message_id: Snowflake) -> Response[message.Message]:
        r = Route(
            'POST',
            '/channels/{channel_id}/messages/{message_id}/crosspost',
            channel_id=channel_id,
            message_id=message_id,
        )
        return self.request(r)

    def pin_message(self, channel_id: Snowflake, message_id: Snowflake, reason: Optional[str] = None) -> Response[None]:
        r = Route(
            'PUT',
            '/channels/{channel_id}/pins/{message_id}',
            channel_id=channel_id,
            message_id=message_id,
        )
        return self.request(r, reason=reason)

    def unpin_message(self, channel_id: Snowflake, message_id: Snowflake, reason: Optional[str] = None) -> Response[None]:
        r = Route(
            'DELETE',
            '/channels/{channel_id}/pins/{message_id}',
            channel_id=channel_id,
            message_id=message_id,
        )
        return self.request(r, reason=reason)

    def pins_from(self, channel_id: Snowflake) -> Response[List[message.Message]]:
        return self.request(Route('GET', '/channels/{channel_id}/pins', channel_id=channel_id))

    def ack_pins(self, channel_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/channels/{channel_id}/pins/ack', channel_id=channel_id))

    # Member management

    def kick(self, user_id: Snowflake, guild_id: Snowflake, reason: Optional[str] = None) -> Response[None]:
        r = Route('DELETE', '/guilds/{guild_id}/members/{user_id}', guild_id=guild_id, user_id=user_id)
        return self.request(r, reason=reason)

    def ban(
        self,
        user_id: Snowflake,
        guild_id: Snowflake,
        delete_message_days: int = 1,
        reason: Optional[str] = None,
    ) -> Response[None]:
        r = Route('PUT', '/guilds/{guild_id}/bans/{user_id}', guild_id=guild_id, user_id=user_id)
        payload = {
            'delete_message_days': str(delete_message_days),
        }

        return self.request(r, json=payload, reason=reason)

    def unban(self, user_id: Snowflake, guild_id: Snowflake, *, reason: Optional[str] = None) -> Response[None]:
        r = Route('DELETE', '/guilds/{guild_id}/bans/{user_id}', guild_id=guild_id, user_id=user_id)
        return self.request(r, reason=reason)

    def guild_voice_state(
        self,
        user_id: Snowflake,
        guild_id: Snowflake,
        *,
        mute: Optional[bool] = None,
        deafen: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> Response[member.Member]:
        r = Route('PATCH', '/guilds/{guild_id}/members/{user_id}', guild_id=guild_id, user_id=user_id)
        payload = {}
        if mute is not None:
            payload['mute'] = mute

        if deafen is not None:
            payload['deaf'] = deafen

        return self.request(r, json=payload, reason=reason)

    def edit_profile(self, payload: Dict[str, Any]) -> Response[user.User]:
        return self.request(Route('PATCH', '/users/@me'), json=payload)

    def edit_my_voice_state(self, guild_id: Snowflake, payload: Dict[str, Any]) -> Response[None]:  # TODO: remove payload
        r = Route('PATCH', '/guilds/{guild_id}/voice-states/@me', guild_id=guild_id)
        return self.request(r, json=payload)

    def edit_voice_state(
        self, guild_id: Snowflake, user_id: Snowflake, payload: Dict[str, Any]
    ) -> Response[None]:  # TODO: remove payload
        r = Route('PATCH', '/guilds/{guild_id}/voice-states/{user_id}', guild_id=guild_id, user_id=user_id)
        return self.request(r, json=payload)

    def edit_me(
        self,
        guild_id: Snowflake,
        *,
        nick: Optional[str] = MISSING,
        avatar: Optional[bytes] = MISSING,
        reason: Optional[str] = None,
    ) -> Response[member.MemberWithUser]:
        payload = {}
        if nick is not MISSING:
            payload['nick'] = nick
        if avatar is not MISSING:
            r = Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=guild_id)
            payload['avatar'] = avatar
        else:
            r = choice(
                (
                    Route('PATCH', '/guilds/{guild_id}/members/@me/nick', guild_id=guild_id),
                    Route('PATCH', '/guilds/{guild_id}/members/@me', guild_id=guild_id),
                )
            )

        return self.request(r, json=payload, reason=reason)

    def edit_member(
        self,
        guild_id: Snowflake,
        user_id: Snowflake,
        *,
        reason: Optional[str] = None,
        **fields: Any,  # TODO: Is this cheating
    ) -> Response[member.MemberWithUser]:
        r = Route('PATCH', '/guilds/{guild_id}/members/{user_id}', guild_id=guild_id, user_id=user_id)
        return self.request(r, json=fields, reason=reason)

    # Channel management

    def edit_channel(
        self,
        channel_id: Snowflake,
        *,
        reason: Optional[str] = None,
        **options: Any,  # TODO: Is this cheating
    ) -> Response[channel.Channel]:
        r = Route('PATCH', '/channels/{channel_id}', channel_id=channel_id)
        valid_keys = (  # TODO: Why is this being validated?
            'name',
            'parent_id',
            'topic',
            'bitrate',
            'nsfw',
            'user_limit',
            'position',
            'permission_overwrites',
            'rate_limit_per_user',
            'type',
            'rtc_region',
            'video_quality_mode',
            'archived',
            'auto_archive_duration',
            'locked',
            'invitable',
            'default_auto_archive_duration',
            'flags',
            'icon',
            'owner',
        )
        payload = {k: v for k, v in options.items() if k in valid_keys}
        return self.request(r, reason=reason, json=payload)

    def bulk_channel_update(
        self,
        guild_id: Snowflake,
        data: List[guild.ChannelPositionUpdate],
        *,
        reason: Optional[str] = None,
    ) -> Response[None]:
        r = Route('PATCH', '/guilds/{guild_id}/channels', guild_id=guild_id)
        return self.request(r, json=data, reason=reason)

    def create_channel(
        self,
        guild_id: Snowflake,
        channel_type: channel.ChannelType,
        *,
        reason: Optional[str] = None,
        **options: Any,  # TODO: Is this cheating
    ) -> Response[channel.GuildChannel]:
        payload = {  # TODO: WTF is happening here??
            'type': channel_type,
        }
        valid_keys = (
            'name',
            'parent_id',
            'topic',
            'bitrate',
            'nsfw',
            'user_limit',
            'position',
            'permission_overwrites',
            'rate_limit_per_user',
            'rtc_region',
            'video_quality_mode',
            'auto_archive_duration',
        )
        payload.update({k: v for k, v in options.items() if k in valid_keys and v is not None})

        return self.request(Route('POST', '/guilds/{guild_id}/channels', guild_id=guild_id), json=payload, reason=reason)

    def delete_channel(
        self, channel_id: Snowflake, *, reason: Optional[str] = None, silent: bool = MISSING
    ) -> Response[channel.Channel]:
        params = {}
        if silent is not MISSING:
            params['silent'] = str(silent).lower()

        return self.request(Route('DELETE', '/channels/{channel_id}', channel_id=channel_id), params=params, reason=reason)

    # Thread management

    def start_thread_with_message(
        self,
        channel_id: Snowflake,
        message_id: Snowflake,
        *,
        name: str,
        auto_archive_duration: threads.ThreadArchiveDuration,
        rate_limit_per_user: Optional[int] = None,
        location: str = MISSING,
        reason: Optional[str] = None,
    ) -> Response[threads.Thread]:
        route = Route(
            'POST', '/channels/{channel_id}/messages/{message_id}/threads', channel_id=channel_id, message_id=message_id
        )
        payload = {
            'name': name,
            'location': location if location is not MISSING else choice(('Message', 'Reply Chain Nudge')),
            'auto_archive_duration': auto_archive_duration,
            'type': 11,
        }
        if rate_limit_per_user is not None:
            payload['rate_limit_per_user'] = rate_limit_per_user

        return self.request(route, json=payload, reason=reason)

    def start_thread_without_message(
        self,
        channel_id: Snowflake,
        *,
        name: str,
        auto_archive_duration: threads.ThreadArchiveDuration,
        type: threads.ThreadType,
        invitable: bool = True,
        rate_limit_per_user: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Response[threads.Thread]:
        r = Route('POST', '/channels/{channel_id}/threads', channel_id=channel_id)
        payload = {
            'auto_archive_duration': auto_archive_duration,
            'location': choice(('Plus Button', 'Thread Browser Toolbar')),
            'name': name,
            'type': type,
        }
        if invitable is not MISSING:
            payload['invitable'] = invitable
        if rate_limit_per_user is not None:
            payload['rate_limit_per_user'] = rate_limit_per_user

        return self.request(r, json=payload, reason=reason)

    def start_thread_in_forum(
        self,
        channel_id: Snowflake,
        *,
        params: MultipartParameters,
        reason: Optional[str] = None,
    ) -> Response[threads.Thread]:
        r = Route('POST', '/channels/{channel_id}/threads', channel_id=channel_id)
        if params.files:
            return self.request(r, files=params.files, form=params.multipart, reason=reason)
        else:
            return self.request(r, json=params.payload, reason=reason)

    def join_thread(self, channel_id: Snowflake) -> Response[None]:
        r = Route('POST', '/channels/{channel_id}/thread-members/@me', channel_id=channel_id)
        params = {'location': choice(('Banner', 'Toolbar Overflow', 'Sidebar Overflow', 'Context Menu'))}

        return self.request(r, params=params)

    def add_user_to_thread(
        self, channel_id: Snowflake, user_id: Snowflake
    ) -> Response[None]:  # TODO: Find a way to test private thread stuff
        r = Route('PUT', '/channels/{channel_id}/thread-members/{user_id}', channel_id=channel_id, user_id=user_id)
        return self.request(r)

    def leave_thread(self, channel_id: Snowflake) -> Response[None]:
        r = Route('DELETE', '/channels/{channel_id}/thread-members/@me', channel_id=channel_id)
        params = {'location': choice(('Toolbar Overflow', 'Context Menu', 'Sidebar Overflow'))}

        return self.request(r, params=params)

    def remove_user_from_thread(self, channel_id: Snowflake, user_id: Snowflake) -> Response[None]:
        r = Route('DELETE', '/channels/{channel_id}/thread-members/{user_id}', channel_id=channel_id, user_id=user_id)
        params = {'location': 'Context Menu'}

        return self.request(r, params=params)

    def get_public_archived_threads(
        self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    ) -> Response[threads.ThreadPaginationPayload]:
        route = Route('GET', '/channels/{channel_id}/threads/archived/public', channel_id=channel_id)

        params = {}
        if before:
            params['before'] = before
        if limit and limit != 50:
            params['limit'] = limit
        return self.request(route, params=params)

    def get_private_archived_threads(
        self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    ) -> Response[threads.ThreadPaginationPayload]:
        route = Route('GET', '/channels/{channel_id}/threads/archived/private', channel_id=channel_id)

        params = {}
        if before:
            params['before'] = before
        if limit and limit != 50:
            params['limit'] = limit
        return self.request(route, params=params)

    def get_joined_private_archived_threads(
        self, channel_id: Snowflake, before: Optional[Snowflake] = None, limit: int = 50
    ) -> Response[threads.ThreadPaginationPayload]:
        route = Route('GET', '/channels/{channel_id}/users/@me/threads/archived/private', channel_id=channel_id)
        params = {}
        if before:
            params['before'] = before
        if limit and limit != 50:
            params['limit'] = limit
        return self.request(route, params=params)

    # Webhook management

    def create_webhook(
        self,
        channel_id: Snowflake,
        *,
        name: str,
        avatar: Optional[bytes] = None,
        reason: Optional[str] = None,
    ) -> Response[webhook.Webhook]:
        payload: Dict[str, Any] = {
            'name': name,
        }
        if avatar is not None:
            payload['avatar'] = avatar

        r = Route('POST', '/channels/{channel_id}/webhooks', channel_id=channel_id)
        return self.request(r, json=payload, reason=reason)

    def channel_webhooks(self, channel_id: Snowflake) -> Response[List[webhook.Webhook]]:
        return self.request(Route('GET', '/channels/{channel_id}/webhooks', channel_id=channel_id))

    def guild_webhooks(self, guild_id: Snowflake) -> Response[List[webhook.Webhook]]:
        return self.request(Route('GET', '/guilds/{guild_id}/webhooks', guild_id=guild_id))

    def get_webhook(self, webhook_id: Snowflake) -> Response[webhook.Webhook]:
        return self.request(Route('GET', '/webhooks/{webhook_id}', webhook_id=webhook_id))

    def follow_webhook(
        self,
        channel_id: Snowflake,
        webhook_channel_id: Snowflake,
        reason: Optional[str] = None,
    ) -> Response[None]:
        r = Route('POST', '/channels/{channel_id}/followers', channel_id=channel_id)
        payload = {
            'webhook_channel_id': str(webhook_channel_id),
        }

        return self.request(r, json=payload, reason=reason)

    # Guild management

    def get_guilds(self, with_counts: bool = True) -> Response[List[guild.Guild]]:
        params = {'with_counts': str(with_counts).lower()}

        return self.request(Route('GET', '/users/@me/guilds'), params=params, super_properties_to_track=True)

    def join_guild(
        self,
        guild_id: Snowflake,
        lurker: bool,
        session_id: Optional[str] = MISSING,
        load_id: str = MISSING,
        location: str = MISSING,
    ) -> Response[guild.Guild]:
        params = {
            'lurker': str(lurker).lower(),
        }
        if lurker:
            params['session_id'] = session_id or utils._generate_session_id()
        if load_id is not MISSING:
            params['recommendation_load_id'] = load_id
            params['location'] = 'Guild%20Discovery'
        if location is not MISSING:
            params['location'] = location
        props = ContextProperties._empty() if lurker else ContextProperties._from_lurking()

        return self.request(
            Route('PUT', '/guilds/{guild_id}/members/@me', guild_id=guild_id),
            context_properties=props,
            params=params,
            json={},
        )

    def leave_guild(self, guild_id: Snowflake, lurking: bool = False) -> Response[None]:
        r = Route('DELETE', '/users/@me/guilds/{guild_id}', guild_id=guild_id)
        payload = {'lurking': lurking}

        return self.request(r, json=payload)

    def get_guild(self, guild_id: Snowflake, with_counts: bool = True) -> Response[guild.Guild]:
        params = {'with_counts': str(with_counts).lower()}

        return self.request(Route('GET', '/guilds/{guild_id}', guild_id=guild_id), params=params)

    def get_guild_preview(self, guild_id: Snowflake) -> Response[guild.GuildPreview]:
        return self.request(Route('GET', '/guilds/{guild_id}/preview', guild_id=guild_id))

    def delete_guild(self, guild_id: Snowflake) -> Response[None]:
        return self.request(Route('DELETE', '/guilds/{guild_id}', guild_id=guild_id))

    def create_guild(
        self, name: str, icon: Optional[str] = None, *, template: str = '2TffvPucqHkN'
    ) -> Response[guild.Guild]:
        payload = {
            'name': name,
            'icon': icon,
            'system_channel_id': None,
            'channels': [],
            'guild_template_code': template,  # API go brrr
        }

        return self.request(Route('POST', '/guilds'), json=payload)

    def edit_guild(self, guild_id: Snowflake, *, reason: Optional[str] = None, **fields: Any) -> Response[guild.Guild]:
        valid_keys = (  # TODO: is this necessary?
            'name',
            'icon',
            'afk_timeout',
            'owner_id',
            'afk_channel_id',
            'splash',
            'discovery_splash',
            'features',
            'verification_level',
            'system_channel_id',
            'default_message_notifications',
            'description',
            'explicit_content_filter',
            'banner',
            'system_channel_flags',
            'rules_channel_id',
            'public_updates_channel_id',
            'preferred_locale',
            'premium_progress_bar_enabled',
        )
        payload = {k: v for k, v in fields.items() if k in valid_keys}

        return self.request(Route('PATCH', '/guilds/{guild_id}', guild_id=guild_id), json=payload, reason=reason)

    def edit_guild_settings(self, guild_id: Snowflake, fields):  # TODO: type
        return self.request(Route('PATCH', '/users/@me/guilds/{guild_id}/settings', guild_id=guild_id), json=fields)

    def get_template(self, code: str) -> Response[template.Template]:
        return self.request(Route('GET', '/guilds/templates/{code}', code=code))

    def guild_templates(self, guild_id: Snowflake) -> Response[List[template.Template]]:
        return self.request(Route('GET', '/guilds/{guild_id}/templates', guild_id=guild_id))

    def create_template(self, guild_id: Snowflake, payload: Dict[str, Any]) -> Response[template.Template]:
        return self.request(Route('POST', '/guilds/{guild_id}/templates', guild_id=guild_id), json=payload)

    def sync_template(self, guild_id: Snowflake, code: str) -> Response[template.Template]:
        return self.request(Route('PUT', '/guilds/{guild_id}/templates/{code}', guild_id=guild_id, code=code))

    def edit_template(self, guild_id: Snowflake, code: str, payload: Dict[str, Any]) -> Response[template.Template]:
        valid_keys = (
            'name',
            'description',
        )
        payload = {k: v for k, v in payload.items() if k in valid_keys}

        return self.request(
            Route('PATCH', '/guilds/{guild_id}/templates/{code}', guild_id=guild_id, code=code), json=payload
        )

    def delete_template(self, guild_id: Snowflake, code: str) -> Response[None]:
        return self.request(Route('DELETE', '/guilds/{guild_id}/templates/{code}', guild_id=guild_id, code=code))

    def create_from_template(self, code: str, name: str, icon: Optional[str]) -> Response[guild.Guild]:
        payload = {
            'name': name,
            'icon': icon,
        }

        return self.request(Route('POST', '/guilds/templates/{code}', code=code), json=payload)

    def get_bans(
        self,
        guild_id: Snowflake,
        limit: int,
        before: Optional[Snowflake] = None,
        after: Optional[Snowflake] = None,
    ) -> Response[List[guild.Ban]]:
        params: Dict[str, Any] = {}
        if limit != 1000:
            params['limit'] = limit
        if before is not None:
            params['before'] = before
        if after is not None:
            params['after'] = after

        return self.request(Route('GET', '/guilds/{guild_id}/bans', guild_id=guild_id), params=params)

    def get_ban(self, user_id: Snowflake, guild_id: Snowflake) -> Response[guild.Ban]:
        return self.request(Route('GET', '/guilds/{guild_id}/bans/{user_id}', guild_id=guild_id, user_id=user_id))

    def get_vanity_code(self, guild_id: Snowflake) -> Response[invite.VanityInvite]:
        return self.request(Route('GET', '/guilds/{guild_id}/vanity-url', guild_id=guild_id))

    def change_vanity_code(self, guild_id: Snowflake, code: str, *, reason: Optional[str] = None) -> Response[None]:
        payload = {'code': code}

        return self.request(Route('PATCH', '/guilds/{guild_id}/vanity-url', guild_id=guild_id), json=payload, reason=reason)

    def get_all_guild_channels(self, guild_id: Snowflake) -> Response[List[guild.GuildChannel]]:
        return self.request(Route('GET', '/guilds/{guild_id}/channels', guild_id=guild_id))

    def get_member(self, guild_id: Snowflake, member_id: Snowflake) -> Response[member.MemberWithUser]:
        return self.request(Route('GET', '/guilds/{guild_id}/members/{member_id}', guild_id=guild_id, member_id=member_id))

    def prune_members(
        self,
        guild_id: Snowflake,
        days: int,
        compute_prune_count: bool,
        roles: Iterable[str],
        *,
        reason: Optional[str] = None,
    ) -> Response[guild.GuildPrune]:
        payload: Dict[str, Any] = {
            'days': days,
            'compute_prune_count': str(compute_prune_count).lower(),
        }
        if roles:
            payload['include_roles'] = ', '.join(roles)

        return self.request(Route('POST', '/guilds/{guild_id}/prune', guild_id=guild_id), json=payload, reason=reason)

    def estimate_pruned_members(
        self,
        guild_id: Snowflake,
        days: int,
        roles: Iterable[str],
    ) -> Response[guild.GuildPrune]:
        params: Dict[str, Any] = {
            'days': days,
        }
        if roles:
            params['include_roles'] = ', '.join(roles)

        return self.request(Route('GET', '/guilds/{guild_id}/prune', guild_id=guild_id), params=params)

    def get_sticker(self, sticker_id: Snowflake) -> Response[sticker.Sticker]:
        return self.request(Route('GET', '/stickers/{sticker_id}', sticker_id=sticker_id))

    def get_sticker_guild(self, sticker_id: Snowflake) -> Response[guild.Guild]:
        return self.request(Route('GET', '/stickers/{sticker_id}/guild', sticker_id=sticker_id))

    def list_premium_sticker_packs(
        self, country: str = 'US', locale: str = 'en-US', payment_source_id: Optional[Snowflake] = None
    ) -> Response[sticker.ListPremiumStickerPacks]:
        params: Dict[str, Snowflake] = {
            'country_code': country,
            'locale': locale,
        }
        if payment_source_id:
            params['payment_source_id'] = payment_source_id

        return self.request(Route('GET', '/sticker-packs'), params=params)

    def get_sticker_pack(self, pack_id: Snowflake):
        return self.request(Route('GET', '/sticker-packs/{pack_id}', pack_id=pack_id))

    def get_all_guild_stickers(self, guild_id: Snowflake) -> Response[List[sticker.GuildSticker]]:
        return self.request(Route('GET', '/guilds/{guild_id}/stickers', guild_id=guild_id))

    def get_guild_sticker(self, guild_id: Snowflake, sticker_id: Snowflake) -> Response[sticker.GuildSticker]:
        r = Route('GET', '/guilds/{guild_id}/stickers/{sticker_id}', guild_id=guild_id, sticker_id=sticker_id)
        return self.request(r)

    def create_guild_sticker(
        self, guild_id: Snowflake, payload: Dict[str, Any], file: File, reason: Optional[str]
    ) -> Response[sticker.GuildSticker]:
        initial_bytes = file.fp.read(16)

        try:
            mime_type = utils._get_mime_type_for_image(initial_bytes)
        except ValueError:
            if initial_bytes.startswith(b'{'):
                mime_type = 'application/json'
            else:
                mime_type = 'application/octet-stream'
        finally:
            file.reset()

        form: List[Dict[str, Any]] = [
            {
                'name': 'file',
                'value': file.fp,
                'filename': file.filename,
                'content_type': mime_type,
            }
        ]

        for k, v in payload.items():
            form.append(
                {
                    'name': k,
                    'value': v,
                }
            )

        return self.request(
            Route('POST', '/guilds/{guild_id}/stickers', guild_id=guild_id), form=form, files=[file], reason=reason
        )

    def modify_guild_sticker(
        self,
        guild_id: Snowflake,
        sticker_id: Snowflake,
        payload: Dict[str, Any],
        reason: Optional[str],
    ) -> Response[sticker.GuildSticker]:
        return self.request(
            Route('PATCH', '/guilds/{guild_id}/stickers/{sticker_id}', guild_id=guild_id, sticker_id=sticker_id),
            json=payload,
            reason=reason,
        )

    def delete_guild_sticker(self, guild_id: Snowflake, sticker_id: Snowflake, reason: Optional[str]) -> Response[None]:
        return self.request(
            Route('DELETE', '/guilds/{guild_id}/stickers/{sticker_id}', guild_id=guild_id, sticker_id=sticker_id),
            reason=reason,
        )

    def get_all_custom_emojis(self, guild_id: Snowflake) -> Response[List[emoji.Emoji]]:
        return self.request(Route('GET', '/guilds/{guild_id}/emojis', guild_id=guild_id))

    def get_custom_emoji(self, guild_id: Snowflake, emoji_id: Snowflake) -> Response[emoji.Emoji]:
        return self.request(Route('GET', '/guilds/{guild_id}/emojis/{emoji_id}', guild_id=guild_id, emoji_id=emoji_id))

    def get_emoji_guild(self, emoji_id: Snowflake) -> Response[guild.Guild]:
        return self.request(Route('GET', '/emojis/{emoji_id}', emoji_id=emoji_id))

    def create_custom_emoji(
        self,
        guild_id: Snowflake,
        name: str,
        image: str,
        *,
        roles: Optional[SnowflakeList] = None,
        reason: Optional[str] = None,
    ) -> Response[emoji.Emoji]:
        payload: Dict[str, Any] = {
            'name': name,
            'image': image,
        }
        if roles:
            payload['roles'] = roles

        r = Route('POST', '/guilds/{guild_id}/emojis', guild_id=guild_id)
        return self.request(r, json=payload, reason=reason)

    def delete_custom_emoji(
        self,
        guild_id: Snowflake,
        emoji_id: Snowflake,
        *,
        reason: Optional[str] = None,
    ) -> Response[None]:
        r = Route('DELETE', '/guilds/{guild_id}/emojis/{emoji_id}', guild_id=guild_id, emoji_id=emoji_id)
        return self.request(r, reason=reason)

    def edit_custom_emoji(
        self,
        guild_id: Snowflake,
        emoji_id: Snowflake,
        *,
        payload: Dict[str, Any],  # TODO: Is this cheating?
        reason: Optional[str] = None,
    ) -> Response[emoji.Emoji]:
        r = Route('PATCH', '/guilds/{guild_id}/emojis/{emoji_id}', guild_id=guild_id, emoji_id=emoji_id)
        return self.request(r, json=payload, reason=reason)

    def get_member_verification(
        self, guild_id: Snowflake, *, with_guild: bool = False, invite: str = MISSING
    ):  # TODO: return type
        params = {
            'with_guild': str(with_guild).lower(),
        }
        if invite is not MISSING:
            params['invite_code'] = invite

        return self.request(Route('GET', '/guilds/{guild_id}/member-verification', guild_id=guild_id), params=params)

    def accept_member_verification(
        self, guild_id: Snowflake, **payload
    ) -> Response[None]:  # payload is the same as the above return type
        return self.request(Route('PUT', '/guilds/{guild_id}/requests/@me', guild_id=guild_id), json=payload)

    def get_all_integrations(
        self, guild_id: Snowflake, include_applications: bool = True
    ) -> Response[List[integration.Integration]]:
        r = Route('GET', '/guilds/{guild_id}/integrations', guild_id=guild_id)
        params = {
            'include_applications': str(include_applications).lower(),
        }

        return self.request(r, params=params)

    def create_integration(self, guild_id: Snowflake, type: integration.IntegrationType, id: int) -> Response[None]:
        r = Route('POST', '/guilds/{guild_id}/integrations', guild_id=guild_id)
        payload = {
            'type': type,
            'id': id,
        }

        return self.request(r, json=payload)

    def edit_integration(self, guild_id: Snowflake, integration_id: Snowflake, **payload: Any) -> Response[None]:
        r = Route(
            'PATCH', '/guilds/{guild_id}/integrations/{integration_id}', guild_id=guild_id, integration_id=integration_id
        )
        return self.request(r, json=payload)

    def sync_integration(self, guild_id: Snowflake, integration_id: Snowflake) -> Response[None]:
        r = Route(
            'POST', '/guilds/{guild_id}/integrations/{integration_id}/sync', guild_id=guild_id, integration_id=integration_id
        )
        return self.request(r)

    def delete_integration(
        self, guild_id: Snowflake, integration_id: Snowflake, *, reason: Optional[str] = None
    ) -> Response[None]:
        r = Route(
            'DELETE', '/guilds/{guild_id}/integrations/{integration_id}', guild_id=guild_id, integration_id=integration_id
        )
        return self.request(r, reason=reason)

    def get_audit_logs(
        self,
        guild_id: Snowflake,
        limit: int = 100,
        before: Optional[Snowflake] = None,
        after: Optional[Snowflake] = None,
        user_id: Optional[Snowflake] = None,
        action_type: Optional[AuditLogAction] = None,
    ) -> Response[audit_log.AuditLog]:
        r = Route('GET', '/guilds/{guild_id}/audit-logs', guild_id=guild_id)
        params: Dict[str, Any] = {'limit': limit}
        if before:
            params['before'] = before
        if after:
            params['after'] = after
        if user_id:
            params['user_id'] = user_id
        if action_type:
            params['action_type'] = action_type

        return self.request(r, params=params)

    def get_widget(self, guild_id: Snowflake) -> Response[widget.Widget]:
        return self.request(Route('GET', '/guilds/{guild_id}/widget.json', guild_id=guild_id))

    def edit_widget(
        self, guild_id: Snowflake, payload: widget.EditWidgetSettings, reason: Optional[str] = None
    ) -> Response[widget.WidgetSettings]:
        return self.request(Route('PATCH', '/guilds/{guild_id}/widget', guild_id=guild_id), json=payload, reason=reason)

    def get_welcome_screen(self, guild_id: Snowflake) -> Response[welcome_screen.WelcomeScreen]:
        return self.request(Route('GET', '/guilds/{guild_id}/welcome-screen', guild_id=guild_id))

    def edit_welcome_screen(self, guild_id: Snowflake, payload) -> Response[welcome_screen.WelcomeScreen]:
        return self.request(Route('PATCH', '/guilds/{guild_id}/welcome-screen', guild_id=guild_id), json=payload)

    # Invite management

    def accept_invite(
        self,
        invite_id: str,
        type: InviteType,
        *,
        guild_id: Snowflake = MISSING,
        channel_id: Snowflake = MISSING,
        channel_type: ChannelType = MISSING,
        message: Message = MISSING,
    ):  # TODO: response type
        if message is not MISSING:  # Invite Button Embed
            props = ContextProperties._from_invite_embed(
                guild_id=getattr(message.guild, 'id', None),
                channel_id=message.channel.id,
                channel_type=getattr(message.channel, 'type', None),
                message_id=message.id,
            )
        elif type is InviteType.guild or type is InviteType.group_dm:  # Join Guild, Accept Invite Page
            props = choice(
                (
                    ContextProperties._from_accept_invite_page,
                    ContextProperties._from_join_guild_popup,
                )
            )(guild_id=guild_id, channel_id=channel_id, channel_type=channel_type)
        else:  # Accept Invite Page
            props = ContextProperties._from_accept_invite_page(
                guild_id=guild_id, channel_id=channel_id, channel_type=channel_type
            )
        return self.request(Route('POST', '/invites/{invite_id}', invite_id=invite_id), context_properties=props, json={})

    def create_invite(
        self,
        channel_id: Snowflake,
        *,
        reason: Optional[str] = None,
        max_age: int = 0,
        max_uses: int = 0,
        temporary: bool = False,
        unique: bool = True,
        target_type: Optional[invite.InviteTargetType] = None,
        target_user_id: Optional[Snowflake] = None,
        target_application_id: Optional[Snowflake] = None,
    ) -> Response[invite.Invite]:
        payload = {
            'max_age': max_age,
            'max_uses': max_uses,
            'temporary': temporary,
            'unique': unique,
        }
        if target_type:
            payload['target_type'] = target_type
        if target_user_id:
            payload['target_user_id'] = target_user_id
        if target_application_id:
            payload['target_application_id'] = str(target_application_id)
        props = choice(
            (
                ContextProperties._from_guild_header,
                ContextProperties._from_context_menu,
            )
        )()

        return self.request(
            Route('POST', '/channels/{channel_id}/invites', channel_id=channel_id),
            reason=reason,
            json=payload,
            context_properties=props,
        )

    def create_group_invite(self, channel_id: Snowflake, *, max_age: int = 86400) -> Response[invite.Invite]:
        payload = {
            'max_age': max_age,
        }
        props = ContextProperties._from_group_dm_invite()

        return self.request(
            Route('POST', '/channels/{channel_id}/invites', channel_id=channel_id), json=payload, context_properties=props
        )

    def create_friend_invite(self) -> Response[invite.Invite]:
        return self.request(Route('POST', '/users/@me/invites'), json={}, context_properties=ContextProperties._empty())

    def get_invite(
        self,
        invite_id: str,
        *,
        with_counts: bool = True,
        with_expiration: bool = True,
        guild_scheduled_event_id: Optional[Snowflake] = None,
    ) -> Response[invite.Invite]:
        params: Dict[str, Any] = {
            'inputValue': invite_id,
            'with_counts': str(with_counts).lower(),
            'with_expiration': str(with_expiration).lower(),
        }
        if guild_scheduled_event_id:
            params['guild_scheduled_event_id'] = guild_scheduled_event_id

        return self.request(Route('GET', '/invites/{invite_id}', invite_id=invite_id), params=params)

    def invites_from(self, guild_id: Snowflake) -> Response[List[invite.Invite]]:
        return self.request(Route('GET', '/guilds/{guild_id}/invites', guild_id=guild_id))

    def invites_from_channel(self, channel_id: Snowflake) -> Response[List[invite.Invite]]:
        return self.request(Route('GET', '/channels/{channel_id}/invites', channel_id=channel_id))

    def get_friend_invites(self) -> Response[List[invite.Invite]]:
        return self.request(Route('GET', '/users/@me/invites'), context_properties=ContextProperties._empty())

    def delete_invite(self, invite_id: str, *, reason: Optional[str] = None) -> Response[invite.Invite]:
        return self.request(Route('DELETE', '/invites/{invite_id}', invite_id=invite_id), reason=reason)

    def delete_friend_invites(self) -> Response[List[invite.Invite]]:
        return self.request(Route('DELETE', '/users/@me/invites'), context_properties=ContextProperties._empty())

    # Role management

    def get_roles(self, guild_id: Snowflake) -> Response[List[role.Role]]:
        return self.request(Route('GET', '/guilds/{guild_id}/roles', guild_id=guild_id))

    def edit_role(
        self, guild_id: Snowflake, role_id: Snowflake, *, reason: Optional[str] = None, **fields: Any
    ) -> Response[role.Role]:
        r = Route('PATCH', '/guilds/{guild_id}/roles/{role_id}', guild_id=guild_id, role_id=role_id)
        valid_keys = ('name', 'permissions', 'color', 'hoist', 'icon', 'unicode_emoji', 'mentionable')
        payload = {k: v for k, v in fields.items() if k in valid_keys}
        return self.request(r, json=payload, reason=reason)

    def delete_role(self, guild_id: Snowflake, role_id: Snowflake, *, reason: Optional[str] = None) -> Response[None]:
        r = Route('DELETE', '/guilds/{guild_id}/roles/{role_id}', guild_id=guild_id, role_id=role_id)
        return self.request(r, reason=reason)

    def replace_roles(
        self,
        user_id: Snowflake,
        guild_id: Snowflake,
        role_ids: List[int],
        *,
        reason: Optional[str] = None,
    ) -> Response[member.MemberWithUser]:
        return self.edit_member(guild_id=guild_id, user_id=user_id, roles=role_ids, reason=reason)

    def create_role(self, guild_id: Snowflake, *, reason: Optional[str] = None, **fields: Any) -> Response[role.Role]:
        r = Route('POST', '/guilds/{guild_id}/roles', guild_id=guild_id)
        return self.request(r, json=fields, reason=reason)

    def move_role_position(
        self,
        guild_id: Snowflake,
        positions: List[guild.RolePositionUpdate],
        *,
        reason: Optional[str] = None,
    ) -> Response[List[role.Role]]:
        r = Route('PATCH', '/guilds/{guild_id}/roles', guild_id=guild_id)
        return self.request(r, json=positions, reason=reason)

    def add_role(
        self, guild_id: Snowflake, user_id: Snowflake, role_id: Snowflake, *, reason: Optional[str] = None
    ) -> Response[None]:
        r = Route(
            'PUT',
            '/guilds/{guild_id}/members/{user_id}/roles/{role_id}',
            guild_id=guild_id,
            user_id=user_id,
            role_id=role_id,
        )
        return self.request(r, reason=reason)

    def remove_role(
        self, guild_id: Snowflake, user_id: Snowflake, role_id: Snowflake, *, reason: Optional[str] = None
    ) -> Response[None]:
        r = Route(
            'DELETE',
            '/guilds/{guild_id}/members/{user_id}/roles/{role_id}',
            guild_id=guild_id,
            user_id=user_id,
            role_id=role_id,
        )
        return self.request(r, reason=reason)

    def get_role_members(self, guild_id: Snowflake, role_id: Snowflake) -> Response[List[Snowflake]]:
        return self.request(
            Route('GET', '/guilds/{guild_id}/roles/{role_id}/member-ids', guild_id=guild_id, role_id=role_id)
        )

    def add_members_to_role(
        self, guild_id: Snowflake, role_id: Snowflake, member_ids: Sequence[Snowflake], *, reason: Optional[str]
    ) -> Response[Dict[Snowflake, member.MemberWithUser]]:
        payload = {'member_ids': member_ids}

        return self.request(
            Route('PATCH', '/guilds/{guild_id}/roles/{role_id}/members', guild_id=guild_id, role_id=role_id),
            json=payload,
            reason=reason,
        )

    def edit_channel_permissions(
        self,
        channel_id: Snowflake,
        target: Snowflake,
        allow: str,
        deny: str,
        type: channel.OverwriteType,
        *,
        reason: Optional[str] = None,
    ) -> Response[None]:
        payload = {'id': target, 'allow': allow, 'deny': deny, 'type': type}
        r = Route('PUT', '/channels/{channel_id}/permissions/{target}', channel_id=channel_id, target=target)
        return self.request(r, json=payload, reason=reason)

    def delete_channel_permissions(
        self, channel_id: Snowflake, target: Snowflake, *, reason: Optional[str] = None
    ) -> Response[None]:
        r = Route('DELETE', '/channels/{channel_id}/permissions/{target}', channel_id=channel_id, target=target)
        return self.request(r, reason=reason)

    # Voice management

    def move_member(
        self,
        user_id: Snowflake,
        guild_id: Snowflake,
        channel_id: Snowflake,
        *,
        reason: Optional[str] = None,
    ) -> Response[member.MemberWithUser]:
        return self.edit_member(guild_id=guild_id, user_id=user_id, channel_id=channel_id, reason=reason)

    def get_ringability(self, channel_id: Snowflake):
        return self.request(Route('GET', '/channels/{channel_id}/call', channel_id=channel_id))

    def ring(self, channel_id: Snowflake, *recipients: Snowflake) -> Response[None]:
        payload = {'recipients': recipients or None}

        return self.request(Route('POST', '/channels/{channel_id}/call/ring', channel_id=channel_id), json=payload)

    def stop_ringing(self, channel_id: Snowflake, *recipients: Snowflake) -> Response[None]:
        r = Route('POST', '/channels/{channel_id}/call/stop-ringing', channel_id=channel_id)
        payload = {'recipients': recipients}

        return self.request(r, json=payload)

    def change_call_voice_region(self, channel_id: int, voice_region: str):  # TODO: return type
        payload = {'region': voice_region}

        return self.request(Route('PATCH', '/channels/{channel_id}/call', channel_id=channel_id), json=payload)

    # Stage instance management
    # TODO: Check all :(

    def get_stage_instance(self, channel_id: Snowflake) -> Response[channel.StageInstance]:
        return self.request(Route('GET', '/stage-instances/{channel_id}', channel_id=channel_id))

    def create_stage_instance(self, *, reason: Optional[str], **payload: Any) -> Response[channel.StageInstance]:
        valid_keys = (
            'channel_id',
            'topic',
            'privacy_level',
        )
        payload = {k: v for k, v in payload.items() if k in valid_keys}

        return self.request(Route('POST', '/stage-instances'), json=payload, reason=reason)

    def edit_stage_instance(self, channel_id: Snowflake, *, reason: Optional[str] = None, **payload: Any) -> Response[None]:
        r = Route('PATCH', '/stage-instances/{channel_id}', channel_id=channel_id)
        valid_keys = (
            'topic',
            'privacy_level',
        )
        payload = {k: v for k, v in payload.items() if k in valid_keys}

        return self.request(r, json=payload, reason=reason)

    def delete_stage_instance(self, channel_id: Snowflake, *, reason: Optional[str] = None) -> Response[None]:
        return self.request(Route('DELETE', '/stage-instances/{channel_id}', channel_id=channel_id), reason=reason)

    # Guild scheduled event management

    @overload
    def get_scheduled_events(
        self, guild_id: Snowflake, with_user_count: Literal[True]
    ) -> Response[List[scheduled_event.GuildScheduledEventWithUserCount]]:
        ...

    @overload
    def get_scheduled_events(
        self, guild_id: Snowflake, with_user_count: Literal[False]
    ) -> Response[List[scheduled_event.GuildScheduledEvent]]:
        ...

    @overload
    def get_scheduled_events(
        self, guild_id: Snowflake, with_user_count: bool
    ) -> Union[
        Response[List[scheduled_event.GuildScheduledEventWithUserCount]], Response[List[scheduled_event.GuildScheduledEvent]]
    ]:
        ...

    def get_scheduled_events(self, guild_id: Snowflake, with_user_count: bool) -> Response[Any]:
        params = {'with_user_count': int(with_user_count)}
        return self.request(Route('GET', '/guilds/{guild_id}/scheduled-events', guild_id=guild_id), params=params)

    def create_guild_scheduled_event(
        self, guild_id: Snowflake, *, reason: Optional[str] = None, **payload: Any
    ) -> Response[scheduled_event.GuildScheduledEvent]:
        valid_keys = (
            'channel_id',
            'entity_metadata',
            'name',
            'privacy_level',
            'scheduled_start_time',
            'scheduled_end_time',
            'description',
            'entity_type',
            'image',
        )
        payload = {k: v for k, v in payload.items() if k in valid_keys}

        return self.request(
            Route('POST', '/guilds/{guild_id}/scheduled-events', guild_id=guild_id), json=payload, reason=reason
        )

    @overload
    def get_scheduled_event(
        self, guild_id: Snowflake, guild_scheduled_event_id: Snowflake, with_user_count: Literal[True]
    ) -> Response[scheduled_event.GuildScheduledEventWithUserCount]:
        ...

    @overload
    def get_scheduled_event(
        self, guild_id: Snowflake, guild_scheduled_event_id: Snowflake, with_user_count: Literal[False]
    ) -> Response[scheduled_event.GuildScheduledEvent]:
        ...

    @overload
    def get_scheduled_event(
        self, guild_id: Snowflake, guild_scheduled_event_id: Snowflake, with_user_count: bool
    ) -> Union[Response[scheduled_event.GuildScheduledEventWithUserCount], Response[scheduled_event.GuildScheduledEvent]]:
        ...

    def get_scheduled_event(
        self, guild_id: Snowflake, guild_scheduled_event_id: Snowflake, with_user_count: bool
    ) -> Response[Any]:
        params = {'with_user_count': int(with_user_count)}
        return self.request(
            Route(
                'GET',
                '/guilds/{guild_id}/scheduled-events/{guild_scheduled_event_id}',
                guild_id=guild_id,
                guild_scheduled_event_id=guild_scheduled_event_id,
            ),
            params=params,
        )

    def edit_scheduled_event(
        self, guild_id: Snowflake, guild_scheduled_event_id: Snowflake, *, reason: Optional[str] = None, **payload: Any
    ) -> Response[scheduled_event.GuildScheduledEvent]:
        valid_keys = (
            'channel_id',
            'entity_metadata',
            'name',
            'privacy_level',
            'scheduled_start_time',
            'scheduled_end_time',
            'status',
            'description',
            'entity_type',
            'image',
        )
        payload = {k: v for k, v in payload.items() if k in valid_keys}

        return self.request(
            Route(
                'PATCH',
                '/guilds/{guild_id}/scheduled-events/{guild_scheduled_event_id}',
                guild_id=guild_id,
                guild_scheduled_event_id=guild_scheduled_event_id,
            ),
            json=payload,
            reason=reason,
        )

    def delete_scheduled_event(
        self,
        guild_id: Snowflake,
        guild_scheduled_event_id: Snowflake,
        *,
        reason: Optional[str] = None,
    ) -> Response[None]:
        return self.request(
            Route(
                'DELETE',
                '/guilds/{guild_id}/scheduled-events/{guild_scheduled_event_id}',
                guild_id=guild_id,
                guild_scheduled_event_id=guild_scheduled_event_id,
            ),
            reason=reason,
        )

    @overload
    def get_scheduled_event_users(
        self,
        guild_id: Snowflake,
        guild_scheduled_event_id: Snowflake,
        limit: int,
        with_member: Literal[True],
        before: Optional[Snowflake] = ...,
        after: Optional[Snowflake] = ...,
    ) -> Response[scheduled_event.ScheduledEventUsersWithMember]:
        ...

    @overload
    def get_scheduled_event_users(
        self,
        guild_id: Snowflake,
        guild_scheduled_event_id: Snowflake,
        limit: int,
        with_member: Literal[False],
        before: Optional[Snowflake] = ...,
        after: Optional[Snowflake] = ...,
    ) -> Response[scheduled_event.ScheduledEventUsers]:
        ...

    @overload
    def get_scheduled_event_users(
        self,
        guild_id: Snowflake,
        guild_scheduled_event_id: Snowflake,
        limit: int,
        with_member: bool,
        before: Optional[Snowflake] = ...,
        after: Optional[Snowflake] = ...,
    ) -> Union[Response[scheduled_event.ScheduledEventUsersWithMember], Response[scheduled_event.ScheduledEventUsers]]:
        ...

    def get_scheduled_event_users(
        self,
        guild_id: Snowflake,
        guild_scheduled_event_id: Snowflake,
        limit: int,
        with_member: bool,
        before: Optional[Snowflake] = None,
        after: Optional[Snowflake] = None,
    ) -> Response[Any]:
        params: Dict[str, Any] = {
            'limit': limit,
            'with_member': str(with_member).lower(),
        }

        if before is not None:
            params['before'] = before
        if after is not None:
            params['after'] = after

        return self.request(
            Route(
                'GET',
                '/guilds/{guild_id}/scheduled-events/{guild_scheduled_event_id}/users',
                guild_id=guild_id,
                guild_scheduled_event_id=guild_scheduled_event_id,
            ),
            params=params,
        )

    # Relationships

    def get_relationships(self):  # TODO: return type
        return self.request(Route('GET', '/users/@me/relationships'))

    def remove_relationship(self, user_id: Snowflake, *, action: RelationshipAction) -> Response[None]:
        r = Route('DELETE', '/users/@me/relationships/{user_id}', user_id=user_id)
        if action is RelationshipAction.deny_request:  # User Profile, Friends, DM Channel
            props = choice(
                (
                    ContextProperties._from_friends_page,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_dm_channel,
                )
            )()
        elif action is RelationshipAction.unfriend:  # Friends, ContextMenu, User Profile, DM Channel
            props = choice(
                (
                    ContextProperties._from_contextmenu,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_friends_page,
                    ContextProperties._from_dm_channel,
                )
            )()
        elif action == RelationshipAction.unblock:  # Friends, ContextMenu, User Profile, DM Channel, NONE
            props = choice(
                (
                    ContextProperties._from_contextmenu,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_friends_page,
                    ContextProperties._from_dm_channel,
                )
            )()
        elif action == RelationshipAction.remove_pending_request:  # Friends
            props = ContextProperties._from_friends_page()

        return self.request(r, context_properties=props)  # type: ignore

    def add_relationship(self, user_id: Snowflake, type: int = MISSING, *, action: RelationshipAction):  # TODO: return type
        r = Route('PUT', '/users/@me/relationships/{user_id}', user_id=user_id)
        if action is RelationshipAction.accept_request:  # User Profile, Friends, DM Channel
            props = choice(
                (
                    ContextProperties._from_friends_page,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_dm_channel,
                )
            )()
        elif action is RelationshipAction.block:  # Friends, ContextMenu, User Profile, DM Channel.
            props = choice(
                (
                    ContextProperties._from_contextmenu,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_friends_page,
                    ContextProperties._from_dm_channel,
                )
            )()
        elif action is RelationshipAction.send_friend_request:  # ContextMenu, User Profile, DM Channel
            props = choice(
                (
                    ContextProperties._from_contextmenu,
                    ContextProperties._from_user_profile,
                    ContextProperties._from_dm_channel,
                )
            )()
        kwargs = {'context_properties': props}  # type: ignore
        if type:
            kwargs['json'] = {'type': type}

        return self.request(r, **kwargs)

    def send_friend_request(self, username, discriminator) -> Response[None]:
        r = Route('POST', '/users/@me/relationships')
        props = choice((ContextProperties._from_add_friend_page, ContextProperties._from_group_dm))()  # Friends, Group DM
        payload = {'username': username, 'discriminator': int(discriminator)}

        return self.request(r, json=payload, context_properties=props)

    def edit_relationship(self, user_id, **payload):  # TODO: return type
        return self.request(Route('PATCH', '/users/@me/relationships/{user_id}', user_id=user_id), json=payload)

    # Connections

    def get_connections(self) -> Response[List[user.Connection]]:
        return self.request(Route('GET', '/users/@me/connections'))

    def edit_connection(self, type: str, id: str, **payload) -> Response[user.Connection]:
        return self.request(Route('PATCH', '/users/@me/connections/{type}/{id}', type=type, id=id), json=payload)

    def refresh_connection(self, type: str, id: str, **payload) -> Response[None]:
        return self.request(Route('POST', '/users/@me/connections/{type}/{id}/refresh', type=type, id=id), json=payload)

    def delete_connection(self, type: str, id: str) -> Response[None]:
        return self.request(Route('DELETE', '/users/@me/connections/{type}/{id}', type=type, id=id))

    def authorize_connection(
        self,
        type: str,
        two_way_link_type: Optional[str] = None,
        two_way_user_code: Optional[str] = None,
        continuation: bool = False,
    ) -> Response[user.ConnectionAuthorization]:
        params = {}
        if two_way_link_type is not None:
            params['two_way_link'] = 'true'
            params['two_way_link_type'] = two_way_link_type
        if two_way_user_code is not None:
            params['two_way_link'] = 'true'
            params['two_way_user_code'] = two_way_user_code
        if continuation:
            params['continuation'] = 'true'
        return self.request(Route('GET', '/connections/{type}/authorize', type=type), params=params)

    def add_connection(
        self,
        type: str,
        code: str,
        state: str,
        *,
        two_way_link_code: Optional[str] = None,
        insecure: bool,
        friend_sync: bool,
    ) -> Response[None]:
        payload = {'code': code, 'state': state, 'insecure': insecure, 'friend_sync': friend_sync}
        if two_way_link_code is not None:
            payload['two_way_link_code'] = two_way_link_code

        return self.request(Route('POST', '/connections/{type}/callback', type=type), json=payload)

    def get_connection_token(self, type: str, id: str) -> Response[user.ConnectionAccessToken]:
        return self.request(Route('GET', '/users/@me/connections/{type}/{id}/access-token', type=type, id=id))

    # Applications / Store

    def get_my_applications(self, *, with_team_applications: bool = True) -> Response[List[appinfo.Application]]:
        params = {'with_team_applications': str(with_team_applications).lower()}

        return self.request(Route('GET', '/applications'), params=params)

    def get_my_application(self, app_id: Snowflake) -> Response[appinfo.Application]:
        return self.request(Route('GET', '/applications/{app_id}', app_id=app_id), super_properties_to_track=True)

    def edit_application(self, app_id: Snowflake, payload: dict) -> Response[appinfo.Application]:
        return self.request(
            Route('PATCH', '/applications/{app_id}', app_id=app_id), super_properties_to_track=True, json=payload
        )

    def delete_application(self, app_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/applications/{app_id}/delete', app_id=app_id), super_properties_to_track=True)

    def transfer_application(self, app_id: Snowflake, team_id: Snowflake) -> Response[appinfo.Application]:
        payload = {'team_id': team_id}

        return self.request(
            Route('POST', '/applications/{app_id}/transfer', app_id=app_id), json=payload, super_properties_to_track=True
        )

    def get_partial_application(self, app_id: Snowflake) -> Response[appinfo.PartialApplication]:
        return self.request(Route('GET', '/oauth2/applications/{app_id}/rpc', app_id=app_id))

    def get_public_application(self, app_id: Snowflake) -> Response[appinfo.PartialApplication]:
        return self.request(Route('GET', '/applications/{app_id}/public', app_id=app_id))

    def get_public_applications(self, app_ids: Sequence[Snowflake]) -> Response[List[appinfo.PartialApplication]]:
        return self.request(Route('GET', '/applications/public'), params={'application_ids': app_ids})

    def create_app(self, name: str, team_id: Optional[Snowflake] = None) -> Response[appinfo.Application]:
        payload = {'name': name, team_id: team_id}

        return self.request(Route('POST', '/applications'), json=payload)

    def get_app_entitlements(
        self,
        app_id: Snowflake,
        *,
        user_id: Optional[Snowflake] = None,
        sku_ids: Optional[Sequence[Snowflake]] = None,
        with_payments: bool = False,
        before: Optional[Snowflake] = None,
        after: Optional[Snowflake] = None,
        limit: int = 100,
    ) -> Response[List[dict]]:
        params: Dict[str, Any] = {'with_payments': str(with_payments).lower()}
        if user_id:
            params['user_id'] = user_id
        if sku_ids:
            params['sku_ids'] = sku_ids
        if before:
            params['before'] = before
        if after:
            params['after'] = after
        if limit != 100:
            params['limit'] = limit

        return self.request(Route('GET', '/applications/{app_id}/entitlements', app_id=app_id), params=params)

    def get_app_entitlement(
        self, app_id: Snowflake, entitlement_id: Snowflake, with_payments: bool = False
    ) -> Response[dict]:
        params = {'with_payments': str(with_payments).lower()}
        return self.request(
            Route(
                'GET', '/applications/{app_id}/entitlements/{entitlement_id}', app_id=app_id, entitlement_id=entitlement_id
            ),
            params=params,
        )

    def delete_app_entitlement(self, app_id: Snowflake, entitlement_id: Snowflake) -> Response[None]:
        return self.request(
            Route(
                'DELETE',
                '/applications/{app_id}/entitlements/{entitlement_id}',
                app_id=app_id,
                entitlement_id=entitlement_id,
            )
        )

    def consume_app_entitlement(self, app_id: Snowflake, entitlement_id: Snowflake) -> Response[None]:
        return self.request(
            Route(
                'POST',
                '/applications/{app_id}/entitlements/{entitlement_id}/consume',
                app_id=app_id,
                entitlement_id=entitlement_id,
            )
        )

    def get_user_app_entitlements(
        self, app_id: Snowflake, *, sku_ids: Optional[Sequence[Snowflake]] = None, exclude_consumed: bool = True
    ) -> Response[List[dict]]:
        params: Dict[str, Any] = {'exclude_consumed': str(exclude_consumed).lower()}
        if sku_ids:
            params['sku_ids'] = sku_ids
        return self.request(Route('GET', '/users/@me/applications/{app_id}/entitlements', app_id=app_id, params=params))

    def get_user_entitlements(
        self, with_sku: bool = True, with_application: bool = True, entitlement_type: Optional[int] = None
    ) -> Response[List[dict]]:
        params: Dict[str, Any] = {'with_sku': str(with_sku).lower(), 'with_application': str(with_application).lower()}
        if entitlement_type is not None:
            params['entitlement_type'] = entitlement_type

        return self.request(Route('GET', '/users/@me/entitlements'), params=params)

    def get_giftable_entitlements(
        self, country_code: Optional[str] = None, payment_source_id: Optional[Snowflake] = None
    ) -> Response[List[dict]]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id

        return self.request(Route('GET', '/users/@me/entitlements/gifts'), params=params)

    def get_guild_entitlements(
        self, guild_id: Snowflake, with_sku: bool = True, with_application: bool = True, exclude_deleted: bool = False
    ) -> Response[List[dict]]:
        params: Dict[str, Any] = {
            'with_sku': str(with_sku).lower(),
            'with_application': str(with_application).lower(),
            'exclude_deleted': str(exclude_deleted).lower(),
        }
        return self.request(Route('GET', '/guilds/{guild_id}/entitlements', guild_id=guild_id), params=params)

    def get_app_skus(
        self,
        app_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        localize: bool = True,
        with_bundled_skus: bool = True,
    ):  # TODO: return type
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'
        if with_bundled_skus:
            params['with_bundled_skus'] = 'true'

        return self.request(
            Route('GET', '/applications/{app_id}/skus', app_id=app_id), params=params, super_properties_to_track=True
        )

    def create_sku(self, payload: dict):
        return self.request(Route('POST', '/store/skus'), json=payload, super_properties_to_track=True)

    def get_app_whitelist(self, app_id: Snowflake):
        return self.request(
            Route('GET', '/oauth2/applications/{app_id}/allowlist', app_id=app_id), super_properties_to_track=True
        )

    def get_app_assets(self, app_id: Snowflake) -> Response[List[appinfo.Asset]]:
        return self.request(Route('GET', '/oauth2/applications/{app_id}/assets', app_id=app_id))

    def get_store_assets(self, app_id: Snowflake) -> Response[List[appinfo.StoreAsset]]:
        return self.request(
            Route('GET', '/store/applications/{app_id}/assets', app_id=app_id), super_properties_to_track=True
        )

    def create_asset(self, app_id: Snowflake, name: str, type: int, image: str) -> Response[appinfo.Asset]:
        payload = {'name': name, 'type': type, 'image': image}

        return self.request(
            Route('POST', '/oauth2/applications/{app_id}/assets', app_id=app_id),
            json=payload,
            super_properties_to_track=True,
        )

    def create_store_asset(self, app_id: Snowflake, file: File) -> Response[appinfo.StoreAsset]:
        initial_bytes = file.fp.read(16)

        try:
            mime_type = utils._get_mime_type_for_image(initial_bytes, True)
        except ValueError:
            if initial_bytes.startswith(b'{'):
                mime_type = 'application/json'
            else:
                mime_type = 'application/octet-stream'
        finally:
            file.reset()

        form: List[Dict[str, Any]] = [
            {
                'name': 'assets',  # Not a typo
                'value': file.fp,
                'filename': file.filename,
                'content_type': mime_type,
            }
        ]

        return self.request(Route('POST', '/store/applications/{app_id}/assets', app_id=app_id), form=form, files=[file])

    def delete_asset(self, app_id: Snowflake, asset_id: Snowflake) -> Response[None]:
        return self.request(
            Route('DELETE', '/oauth2/applications/{app_id}/assets/{asset_id}', app_id=app_id, asset_id=asset_id),
            super_properties_to_track=True,
        )

    def delete_store_asset(self, app_id: Snowflake, asset_id: Snowflake) -> Response[None]:
        return self.request(
            Route('DELETE', '/store/applications/{app_id}/assets/{asset_id}', app_id=app_id, asset_id=asset_id),
            super_properties_to_track=True,
        )

    def create_team(self, name: str):
        payload = {'name': name}

        return self.request(Route('POST', '/teams'), json=payload, super_properties_to_track=True)

    def get_teams(self, *, include_payout_account_status: bool = False) -> Response[List[team.Team]]:
        params = {}
        if include_payout_account_status:
            params['include_payout_account_status'] = 'true'

        return self.request(Route('GET', '/teams'), params=params)

    def get_team(self, team_id: Snowflake) -> Response[team.Team]:
        return self.request(Route('GET', '/teams/{team_id}', team_id=team_id), super_properties_to_track=True)

    def edit_team(self, team_id: Snowflake, payload: dict) -> Response[team.Team]:
        return self.request(
            Route('PATCH', '/teams/{team_id}', team_id=team_id), json=payload, super_properties_to_track=True
        )

    def delete_team(self, team_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/teams/{team_id}/delete', team_id=team_id), super_properties_to_track=True)

    def get_team_applications(self, team_id: Snowflake) -> Response[List[appinfo.Application]]:
        return self.request(Route('GET', '/teams/{team_id}/applications', team_id=team_id), super_properties_to_track=True)

    def get_team_members(self, team_id: Snowflake) -> Response[List[team.TeamMember]]:
        return self.request(Route('GET', '/teams/{team_id}/members', team_id=team_id), super_properties_to_track=True)

    def invite_team_member(self, team_id: Snowflake, username: str, discriminator: Snowflake):
        payload = {'username': username, 'discriminator': str(discriminator)}

        return self.request(
            Route('POST', '/teams/{team_id}/members', team_id=team_id), json=payload, super_properties_to_track=True
        )

    def remove_team_member(self, team_id: Snowflake, user_id: Snowflake):
        return self.request(
            Route('DELETE', '/teams/{team_id}/members/{user_id}', team_id=team_id, user_id=user_id),
            super_properties_to_track=True,
        )

    def create_team_company(self, team_id: Snowflake, name: str) -> Response[appinfo.Company]:
        payload = {'name': name}

        return self.request(
            Route('POST', '/teams/{team_id}/companies', team_id=team_id), json=payload, super_properties_to_track=True
        )

    def search_companies(self, query: str) -> Response[List[appinfo.Company]]:
        # This endpoint 204s without a query?
        params = {'query': query}
        data = self.request(Route('GET', '/companies'), params=params, super_properties_to_track=True)
        return data or []

    def get_team_payouts(
        self, team_id: Snowflake, *, limit: int = 96, before: Optional[Snowflake] = None
    ) -> Response[List[team.TeamPayout]]:
        params: Dict[str, Any] = {'limit': limit}
        if before is not None:
            params['before'] = before

        return self.request(
            Route('GET', '/teams/{team_id}/payouts', team_id=team_id), params=params, super_properties_to_track=True
        )

    def get_team_payout_report(self, team_id: Snowflake, payout_id: Snowflake, type: str) -> Response[bytes]:
        params = {'type': type}
        return self.request(
            Route('GET', '/teams/{team_id}/payouts/{payout_id}/report', team_id=team_id, payout_id=payout_id),
            params=params,
            super_properties_to_track=True,
        )

    def botify_app(self, app_id: Snowflake) -> Response[None]:
        return self.request(
            Route('POST', '/applications/{app_id}/bot', app_id=app_id), json={}, super_properties_to_track=True
        )

    def edit_bot(self, app_id: Snowflake, payload: dict) -> Response[user.User]:
        return self.request(
            Route('PATCH', '/applications/{app_id}/bot', app_id=app_id), json=payload, super_properties_to_track=True
        )

    def reset_secret(self, app_id: Snowflake) -> Response[dict]:
        return self.request(Route('POST', '/applications/{app_id}/reset', app_id=app_id), super_properties_to_track=True)

    def reset_bot_token(self, app_id: Snowflake) -> Response[dict]:
        return self.request(Route('POST', '/applications/{app_id}/bot/reset', app_id=app_id), super_properties_to_track=True)

    def get_detectable_applications(self) -> Response[List[appinfo.PartialApplication]]:
        return self.request(Route('GET', '/applications/detectable'))

    def get_guild_applications(
        self,
        guild_id: Snowflake,
        *,
        type: Optional[int] = None,
        include_team: bool = False,
        channel_id: Optional[Snowflake] = None,
    ) -> Response[List[appinfo.PartialApplication]]:
        params = {}
        if type is not None:
            params['type'] = type
        if include_team:
            params['include_team'] = 'true'
        if channel_id is not None:
            params['channel_id'] = channel_id

        return self.request(Route('GET', '/guilds/{guild_id}/applications', guild_id=guild_id), params=params)

    def get_app_ticket(self, app_id: Snowflake, test_mode: bool = False) -> Response[appinfo.Ticket]:
        payload = {'test_mode': test_mode}

        return self.request(Route('POST', '/users/@me/applications/{app_id}/ticket', app_id=app_id), json=payload)

    def get_app_entitlement_ticket(self, app_id: Snowflake, test_mode: bool = False) -> Response[appinfo.Ticket]:
        payload = {'test_mode': test_mode}

        return self.request(
            Route('POST', '/users/@me/applications/{app_id}/entitlement-ticket', app_id=app_id), json=payload
        )

    def get_app_activity_statistics(self, app_id: Snowflake) -> Response[List[appinfo.ActivityStatistics]]:
        return self.request(Route('GET', '/activities/statistics/applications/{app_id}', app_id=app_id))

    def get_activity_statistics(self) -> Response[List[appinfo.ActivityStatistics]]:
        return self.request(Route('GET', '/users/@me/activities/statistics/applications'))

    def get_app_manifest_labels(self, app_id: Snowflake) -> Response[List[appinfo.ManifestLabel]]:
        return self.request(
            Route('GET', '/applications/{app_id}/manifest-labels', app_id=app_id), super_properties_to_track=True
        )

    def get_app_branches(self, app_id: Snowflake) -> Response[List[appinfo.Branch]]:
        return self.request(Route('GET', '/applications/{app_id}/branches', app_id=app_id))

    def create_app_branch(self, app_id: Snowflake, name: str) -> Response[appinfo.Branch]:
        payload = {'name': name}

        return self.request(Route('POST', '/applications/{app_id}/branches', app_id=app_id), json=payload)

    def delete_app_branch(self, app_id: Snowflake, branch_id: Snowflake) -> Response[None]:
        return self.request(
            Route('DELETE', '/applications/{app_id}/branches/{branch_id}', app_id=app_id, branch_id=branch_id)
        )

    def get_branch_builds(self, app_id: Snowflake, branch_id: Snowflake) -> Response[List[appinfo.Build]]:
        return self.request(
            Route('GET', '/applications/{app_id}/branches/{branch_id}/builds', app_id=app_id, branch_id=branch_id)
        )

    def get_branch_build(self, app_id: Snowflake, branch_id: Snowflake, build_id: Snowflake) -> Response[appinfo.Build]:
        return self.request(
            Route(
                'GET',
                '/applications/{app_id}/branches/{branch_id}/builds/{build_id}',
                app_id=app_id,
                branch_id=branch_id,
                build_id=build_id,
            )
        )

    def get_latest_branch_build(self, app_id: Snowflake, branch_id: Snowflake) -> Response[appinfo.Build]:
        return self.request(
            Route('GET', '/applications/{app_id}/branches/{branch_id}/builds/latest', app_id=app_id, branch_id=branch_id)
        )

    def get_live_branch_build(
        self, app_id: Snowflake, branch_id: Snowflake, locale: str, platform: str
    ) -> Response[appinfo.Build]:
        params = {'locale': locale, 'platform': platform}
        return self.request(
            Route('GET', '/applications/{app_id}/branches/{branch_id}/builds/live', app_id=app_id, branch_id=branch_id),
            params=params,
        )

    def get_build_ids(self, branch_ids: Sequence[Snowflake]) -> Response[List[appinfo.Branch]]:
        payload = {'branch_ids': branch_ids}

        return self.request(Route('POST', '/branches'), json=payload)

    def create_branch_build(self, app_id: Snowflake, branch_id: Snowflake, payload: dict) -> Response[appinfo.CreatedBuild]:
        return self.request(
            Route('POST', '/applications/{app_id}/branches/{branch_id}/builds', app_id=app_id, branch_id=branch_id),
            json=payload,
        )

    def edit_build(self, app_id: Snowflake, build_id: Snowflake, status: str) -> Response[None]:
        payload = {'status': status}

        return self.request(
            Route('PATCH', '/applications/{app_id}/builds/{build_id}', app_id=app_id, build_id=build_id), json=payload
        )

    def delete_build(self, app_id: Snowflake, build_id: Snowflake) -> Response[None]:
        return self.request(Route('DELETE', '/applications/{app_id}/builds/{build_id}', app_id=app_id, build_id=build_id))

    def get_branch_build_size(
        self, app_id: Snowflake, branch_id: Snowflake, build_id: Snowflake, manifest_ids: Sequence[Snowflake]
    ) -> Response[appinfo.BranchSize]:
        payload = {'manifest_ids': manifest_ids}

        return self.request(
            Route(
                'POST',
                '/applications/{app_id}/branches/{branch_id}/builds/{build_id}/size',
                app_id=app_id,
                branch_id=branch_id,
                build_id=build_id,
            ),
            json=payload,
        )

    def get_branch_build_download_signatures(
        self, app_id: Snowflake, branch_id: Snowflake, build_id: Snowflake, manifest_label_ids: Sequence[Snowflake]
    ) -> Response[Dict[str, appinfo.DownloadSignature]]:
        params = {'branch_id': branch_id, 'build_id': build_id}
        payload = {'manifest_label_ids': manifest_label_ids}

        return self.request(
            Route(
                'POST',
                '/applications/{app_id}/download-signatures',
                app_id=app_id,
            ),
            params=params,
            json=payload,
        )

    def get_build_upload_urls(self, app_id: Snowflake, build_id: Snowflake, files: Sequence[File], hash: bool = True) -> Response[List[appinfo.CreatedBuildFile]]:
        payload = {'files': []}
        for file in files:
            # We create a new ID and set it as the filename
            id = ''.join(choices(string.ascii_letters + string.digits, k=32)).upper()
            file.filename = id
            data = {'id': file.filename}
            if hash:
                data['md5_hash'] = file.md5

            payload['files'].append(data)

        return self.request(Route('POST', '/applications/{app_id}/builds/{build_id}/files', app_id=app_id, build_id=build_id), json=payload)

    def publish_build(self, app_id: Snowflake, branch_id: Snowflake, build_id: Snowflake):
        return self.request(
            Route(
                'POST',
                '/applications/{app_id}/branches/{branch_id}/builds/{build_id}/publish',
                app_id=app_id,
                branch_id=branch_id,
                build_id=build_id,
            )
        )

    def promote_build(self, app_id: Snowflake, branch_id: Snowflake, target_branch_id: Snowflake) -> Response[None]:
        return self.request(
            Route(
                'PUT',
                '/applications/{app_id}/branches/{branch_id}/promote/{target_branch_id}',
                app_id=app_id,
                branch_id=branch_id,
                target_branch_id=target_branch_id,
            )
        )

    def get_store_listing(
        self,
        listing_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        localize: bool = True,
    ) -> Response[dict]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/listings/{listing_id}', app_id=listing_id), params=params)

    def get_store_listing_by_sku(
        self,
        sku_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        localize: bool = True,
    ) -> Response[dict]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/published-listings/skus/{sku_id}', sku_id=sku_id), params=params)

    def get_sku_store_listings(
        self,
        sku_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[int] = None,
        localize: bool = True,
    ) -> Response[List[dict]]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/skus/{sku_id}/listings', sku_id=sku_id), params=params)

    def get_store_listing_subscription_plans(
        self,
        sku_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        include_unpublished: bool = False,
        revenue_surface: Optional[int] = None,
    ) -> Response[List[dict]]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if include_unpublished:
            params['include_unpublished'] = 'true'
        if revenue_surface:
            params['revenue_surface'] = revenue_surface

        return self.request(Route('GET', '/store/published-listings/skus/{sku_id}/subscription-plans', sku_id=sku_id))

    def get_store_listings_subscription_plans(
        self,
        sku_ids: Sequence[Snowflake],
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        include_unpublished: bool = False,
        revenue_surface: Optional[int] = None,
    ) -> Response[List[dict]]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if include_unpublished:
            params['include_unpublished'] = 'true'
        if revenue_surface:
            params['revenue_surface'] = revenue_surface

        return self.request(Route('GET', '/store/published-listings/skus/subscription-plans'), params={'sku_ids': sku_ids})

    def get_app_store_listings(
        self,
        app_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[int] = None,
        localize: bool = True,
    ) -> Response[List[dict]]:
        params = {'application_id': app_id}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/published-listings/skus'), params=params)

    def get_app_store_listing(
        self,
        app_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[int] = None,
        localize: bool = True,
    ) -> Response[dict]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(
            Route('GET', '/store/published-listings/applications/{application_id}', application_id=app_id), params=params
        )

    def get_apps_store_listing(
        self,
        app_ids: Sequence[Snowflake],
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        localize: bool = True,
    ) -> Response[List[dict]]:
        params: Dict[str, Any] = {'application_ids': app_ids}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/published-listings/applications'), params=params)

    def create_store_listing(self, application_id: Snowflake, sku_id: Snowflake, payload: dict) -> Response[dict]:
        return self.request(
            Route('POST', '/store/listings'),
            json={**payload, 'application_id': application_id, 'sku_id': sku_id},
            super_properties_to_track=True,
        )

    def edit_store_listing(self, listing_id: Snowflake, payload: dict) -> Response[dict]:
        return self.request(
            Route('PATCH', '/store/listings/{listing_id}', listing_id=listing_id),
            json=payload,
            super_properties_to_track=True,
        )

    def get_sku(
        self,
        sku_id: Snowflake,
        *,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        localize: bool = True,
    ) -> Response[dict]:
        params = {}
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id
        if not localize:
            params['localize'] = 'false'

        return self.request(Route('GET', '/store/skus/{sku_id}', sku_id=sku_id), params=params)

    def edit_sku(self, sku_id: Snowflake, payload: dict) -> Response[dict]:
        return self.request(
            Route('PATCH', '/store/skus/{sku_id}', sku_id=sku_id), json=payload, super_properties_to_track=True
        )

    def preview_sku_purchase(
        self,
        sku_id: Snowflake,
        payment_source_id: Snowflake,
        subscription_plan_id: Optional[Snowflake] = None,
        *,
        test_mode: bool = False,
    ) -> Response[dict]:
        params = {'payment_source_id': payment_source_id}
        if subscription_plan_id:
            params['subscription_plan_id'] = subscription_plan_id
        if test_mode:
            params['test_mode'] = 'true'

        return self.request(
            Route('GET', '/store/skus/{sku_id}/purchase', sku_id=sku_id),
            params=params,
            context_properties=ContextProperties._empty(),
        )

    def purchase_sku(
        self,
        sku_id: Snowflake,
        payment_source_id: Optional[Snowflake] = None,
        *,
        subscription_plan_id: Optional[Snowflake] = None,
        expected_amount: Optional[int] = None,
        expected_currency: Optional[str] = None,
        gift: bool = False,
        gift_style: Optional[int] = None,
        test_mode: bool = False,
        payment_source_token: Optional[str] = None,
        purchase_token: Optional[str] = None,
        return_url: Optional[str] = None,
        gateway_checkout_context: Optional[str] = None,
    ) -> Response[dict]:
        payload = {
            'gift': gift,
            'purchase_token': purchase_token,
            'gateway_checkout_context': gateway_checkout_context,
        }
        if payment_source_id:
            payload['payment_source_id'] = payment_source_id
            payload['payment_source_token'] = payment_source_token
        if subscription_plan_id:
            payload['sku_subscription_plan_id'] = subscription_plan_id
        if expected_amount is not None:
            payload['expected_amount'] = expected_amount
        if expected_currency:
            payload['expected_currency'] = expected_currency
        if gift_style:
            payload['gift_style'] = gift_style
        if test_mode:
            payload['test_mode'] = True
        if return_url:
            payload['return_url'] = return_url

        return self.request(
            Route('POST', '/store/skus/{sku_id}/purchase', sku_id=sku_id),
            json=payload,
            context_properties=ContextProperties._empty(),
        )

    def create_sku_discount(self, sku_id: Snowflake, user_id: Snowflake, percent_off: int, ttl: int = 600) -> Response[None]:
        payload = {'percent_off': percent_off, 'ttl': ttl}
        return self.request(
            Route('PUT', '/store/skus/{sku_id}/discounts/{user_id}', sku_id=sku_id, user_id=user_id), json=payload
        )

    def delete_sku_discount(self, sku_id: Snowflake, user_id: Snowflake) -> Response[None]:
        return self.request(Route('DELETE', '/store/skus/{sku_id}/discounts/{user_id}', sku_id=sku_id, user_id=user_id))

    def get_eula(self, eula_id: Snowflake) -> Response[appinfo.EULA]:
        return self.request(Route('GET', '/store/eulas/{eula_id}', eula_id=eula_id))

    def get_price_tiers(self, type: Optional[int] = None, guild_id: Optional[Snowflake] = None) -> Response[List[int]]:
        params = {}
        if type:
            params['price_tier_type'] = type
        if guild_id:
            params['guild_id'] = guild_id

        return self.request(Route('GET', '/store/price-tiers'), params=params, super_properties_to_track=True)

    def get_price_tier(self, price_tier: Snowflake) -> Response[Dict[str, int]]:
        return self.request(
            Route('GET', '/store/price-tiers/{price_tier}', price_tier=price_tier), super_properties_to_track=True
        )

    def create_achievement(
        self,
        app_id: Snowflake,
        *,
        name: str,
        name_localizations: Optional[Mapping[str, str]] = None,
        description: str,
        description_localizations: Optional[Mapping[str, str]] = None,
        icon: str,
        secure: bool,
        secret: bool,
    ) -> Response[appinfo.Achievement]:
        payload = {
            'name': {
                'default': name,
                'localizations': {str(k): v for k, v in (name_localizations or {}).items()},
            },
            'description': {
                'default': description,
                'localizations': {str(k): v for k, v in (description_localizations or {}).items()},
            },
            'icon': icon,
            'secure': secure,
            'secret': secret,
        }

        return self.request(
            Route('POST', '/applications/{app_id}/achievements', app_id=app_id), json=payload, super_properties_to_track=True
        )

    def get_achievements(self, app_id: Snowflake) -> Response[List[appinfo.Achievement]]:
        return self.request(
            Route('GET', '/applications/{app_id}/achievements', app_id=app_id), super_properties_to_track=True
        )

    def get_my_achievements(self, app_id: Snowflake) -> Response[List[appinfo.Achievement]]:
        return self.request(Route('GET', '/users/@me/applications/{app_id}/achievements', app_id=app_id))

    def get_achievement(self, app_id: Snowflake, achievement_id: Snowflake) -> Response[appinfo.Achievement]:
        return self.request(
            Route(
                'GET', '/applications/{app_id}/achievements/{achievement_id}', app_id=app_id, achievement_id=achievement_id
            ),
            super_properties_to_track=True,
        )

    def edit_achievement(self, app_id: Snowflake, achievement_id: Snowflake, payload: dict) -> Response[appinfo.Achievement]:
        return self.request(
            Route(
                'PATCH', '/applications/{app_id}/achievements/{achievement_id}', app_id=app_id, achievement_id=achievement_id
            ),
            json=payload,
            super_properties_to_track=True,
        )

    def update_user_achievement(
        self, app_id: Snowflake, achievement_id: Snowflake, user_id: Snowflake, percent_complete: int
    ) -> Response[None]:
        payload = {'percent_complete': percent_complete}

        return self.request(
            Route(
                'PUT',
                '/users/{user_id}/applications/{app_id}/achievements/{achievement_id}',
                user_id=user_id,
                app_id=app_id,
                achievement_id=achievement_id,
            ),
            json=payload,
        )

    def delete_achievement(self, app_id: Snowflake, achievement_id: Snowflake) -> Response[None]:
        return self.request(
            Route(
                'DELETE',
                '/applications/{app_id}/achievements/{achievement_id}',
                app_id=app_id,
                achievement_id=achievement_id,
            ),
            super_properties_to_track=True,
        )

    def get_gift_batches(self, app_id: Snowflake) -> Response[List[appinfo.GiftBatch]]:
        return self.request(
            Route('GET', '/applications/{app_id}/gift-code-batches', app_id=app_id), super_properties_to_track=True
        )

    def get_gift_batch_csv(self, app_id: Snowflake, batch_id: Snowflake) -> Response[bytes]:
        return self.request(
            Route('GET', '/applications/{app_id}/gift-code-batches/{batch_id}', app_id=app_id, batch_id=batch_id),
            super_properties_to_track=True,
        )

    def create_gift_batch(
        self,
        app_id: Snowflake,
        sku_id: Snowflake,
        amount: int,
        description: str,
        *,
        entitlement_branches: Optional[Sequence[Snowflake]] = None,
        entitlement_starts_at: Optional[str] = None,
        entitlement_ends_at: Optional[str] = None,
    ) -> Response[appinfo.GiftBatch]:
        payload = {
            'sku_id': sku_id,
            'amount': str(amount),
            'description': description,
            'entitlement_branches': entitlement_branches or [],
            'entitlement_starts_at': entitlement_starts_at or '',
            'entitlement_ends_at': entitlement_ends_at or '',
        }

        return self.request(
            Route('POST', '/applications/{app_id}/gift-code-batches', app_id=app_id),
            json=payload,
            super_properties_to_track=True,
        )

    def get_gift(
        self,
        code: str,
        country_code: Optional[str] = None,
        payment_source_id: Optional[Snowflake] = None,
        with_application: bool = False,
        with_subscription_plan: bool = True,
    ) -> Response[dict]:
        params: Dict[str, Any] = {
            'with_application': str(with_application).lower(),
            'with_subscription_plan': str(with_subscription_plan).lower(),
        }
        if country_code:
            params['country_code'] = country_code
        if payment_source_id:
            params['payment_source_id'] = payment_source_id

        return self.request(Route('GET', '/entitlements/gift-codes/{code}', code=code), params=params)

    def get_sku_gifts(self, sku_id: Snowflake, subscription_plan_id: Optional[Snowflake] = None) -> Response[List[dict]]:
        params: Dict[str, Any] = {'sku_id': sku_id}
        if subscription_plan_id:
            params['subscription_plan_id'] = subscription_plan_id

        return self.request(Route('GET', '/users/@me/entitlements/gift-codes'), params=params)

    def create_gift(
        self, sku_id: Snowflake, *, subscription_plan_id: Optional[Snowflake] = None, gift_style: Optional[int] = None
    ) -> Response[dict]:
        payload: Dict[str, Any] = {'sku_id': sku_id}
        if subscription_plan_id:
            payload['subscription_plan_id'] = subscription_plan_id
        if gift_style:
            payload['gift_style'] = gift_style

        return self.request(Route('POST', '/users/@me/entitlements/gift-codes'), json=payload)

    def redeem_gift(
        self,
        code: str,
        payment_source_id: Optional[Snowflake] = None,
        channel_id: Optional[Snowflake] = None,
        gateway_checkout_context: Optional[str] = None,
    ) -> Response[dict]:
        payload: Dict[str, Any] = {'channel_id': channel_id, 'gateway_checkout_context': gateway_checkout_context}
        if payment_source_id:
            payload['payment_source_id'] = payment_source_id

        return self.request(Route('POST', '/entitlements/gift-codes/{code}/redeem', code=code), json=payload)

    def delete_gift(self, code: str) -> Response[None]:
        return self.request(Route('DELETE', '/users/@me/entitlements/gift-codes/{code}', code=code))

    # Billing

    def get_payment_sources(self) -> Response[List[dict]]:
        return self.request(Route('GET', '/users/@me/billing/payment-sources'))

    def get_payment_source(self, source_id: Snowflake) -> Response[dict]:
        return self.request(Route('GET', '/users/@me/billing/payment-sources/{source_id}', source_id=source_id))

    def create_payment_source(
        self,
        *,
        token: str,
        payment_gateway: int,
        billing_address: dict,
        billing_address_token: Optional[str] = None,
        return_url: Optional[str] = None,
        bank: Optional[str] = None,
    ) -> Response[dict]:
        payload = {
            'token': token,
            'payment_gateway': int(payment_gateway),
            'billing_address': billing_address,
        }
        if billing_address_token:
            payload['billing_address_token'] = billing_address_token
        if return_url:
            payload['return_url'] = return_url
        if bank:
            payload['bank'] = bank

        return self.request(Route('POST', '/users/@me/billing/payment-sources'), json=payload)

    def edit_payment_source(self, source_id: Snowflake, payload: dict) -> Response[dict]:
        return self.request(
            Route('PATCH', '/users/@me/billing/payment-sources/{source_id}', source_id=source_id), json=payload
        )

    def delete_payment_source(self, source_id: Snowflake) -> Response[None]:
        return self.request(Route('DELETE', '/users/@me/billing/payment-sources/{source_id}', source_id=source_id))

    def validate_billing_address(self, address: dict) -> Response[dict]:
        payload = {'billing_address': address}

        return self.request(Route('POST', '/users/@me/billing/payment-sources/validate-billing-address'), json=payload)

    def get_subscriptions(self, limit: Optional[int] = None, include_inactive: bool = False) -> Response[List[dict]]:
        params = {}
        if limit:
            params['limit'] = limit
        if include_inactive:
            params['include_inactive'] = 'true'

        return self.request(Route('GET', '/users/@me/billing/subscriptions'), params=params)

    def get_subscription(self, subscription_id: Snowflake) -> Response[dict]:
        return self.request(
            Route('GET', '/users/@me/billing/subscriptions/{subscription_id}', subscription_id=subscription_id)
        )

    def create_subscription(
        self,
        items: List[dict],
        payment_source_id: int,
        currency: str,
        *,
        trial_id: Optional[Snowflake] = None,
        payment_source_token: Optional[str] = None,
        return_url: Optional[str] = None,
        purchase_token: Optional[str] = None,
        gateway_checkout_context: Optional[str] = None,
        code: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Response[dict]:
        payload = {
            'items': items,
            'payment_source_id': payment_source_id,
            'currency': currency,
            'trial_id': trial_id,
            'payment_source_token': payment_source_token,
            'return_url': return_url,
            'purchase_token': purchase_token,
            'gateway_checkout_context': gateway_checkout_context,
        }
        if code:
            payload['code'] = code
        if metadata:
            payload['metadata'] = metadata

        return self.request(Route('POST', '/users/@me/billing/subscriptions'), json=payload)

    def edit_subscription(
        self,
        subscription_id: Snowflake,
        location: Optional[Union[str, List[str]]] = None,
        location_stack: Optional[Union[str, List[str]]] = None,
        **payload: dict,
    ) -> Response[dict]:
        params = {}
        if location:
            params['location'] = location
        if location_stack:
            params['location_stack'] = location_stack

        return self.request(
            Route('PATCH', '/users/@me/billing/subscriptions/{subscription_id}', subscription_id=subscription_id),
            params=params,
            json=payload,
        )

    def delete_subscription(
        self,
        subscription_id: Snowflake,
        location: Optional[Union[str, List[str]]] = None,
        location_stack: Optional[Union[str, List[str]]] = None,
    ) -> Response[None]:
        params = {}
        if location:
            params['location'] = location
        if location_stack:
            params['location_stack'] = location_stack

        return self.request(
            Route('DELETE', '/users/@me/billing/subscriptions/{subscription_id}', subscription_id=subscription_id),
            params=params,
        )

    def preview_subscriptions_update(
        self,
        items: List[dict],
        currency: str,
        payment_source_id: Optional[Snowflake] = None,
        trial_id: Optional[Snowflake] = None,
        apply_entitlements: bool = MISSING,
        renewal: bool = MISSING,
        code: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Response[dict]:
        payload: Dict[str, Any] = {
            'items': items,
            'currency': currency,
            'payment_source_id': payment_source_id,
            'trial_id': trial_id,
        }
        if apply_entitlements is not MISSING:
            payload['apply_entitlements'] = apply_entitlements
        if renewal is not MISSING:
            payload['renewal'] = renewal
        if code:
            payload['code'] = code
        if metadata:
            payload['metadata'] = metadata

        return self.request(Route('POST', '/users/@me/billing/subscriptions/preview'), json=payload)

    def get_subscription_preview(self, subscription_id: Snowflake) -> Response[dict]:
        return self.request(
            Route('GET', '/users/@me/billing/subscriptions/{subscription_id}/preview', subscription_id=subscription_id)
        )

    def preview_subscription_update(self, subscription_id: Snowflake, **payload):
        return self.request(
            Route('PATCH', '/users/@me/billing/subscriptions/{subscription_id}/preview', subscription_id=subscription_id),
            json=payload,
        )

    def get_subscription_invoices(self, subscription_id: Snowflake) -> Response[List[dict]]:
        return self.request(
            Route('GET', '/users/@me/billing/subscriptions/{subscription_id}/invoices', subscription_id=subscription_id)
        )

    def get_applied_guild_subscriptions(self) -> Response[List[subscriptions.PremiumGuildSubscription]]:
        return self.request(Route('GET', '/users/@me/guilds/premium/subscriptions'))

    def get_guild_subscriptions(self, guild_id: Snowflake) -> Response[List[subscriptions.PremiumGuildSubscription]]:
        return self.request(Route('GET', '/guilds/{guild_id}/premium/subscriptions', guild_id=guild_id))

    def delete_guild_subscription(self, guild_id: Snowflake, subscription_id: Snowflake) -> Response[None]:
        return self.request(
            Route(
                'DELETE',
                '/guilds/{guild_id}/premium/subscriptions/{subscription_id}',
                guild_id=guild_id,
                subscription_id=subscription_id,
            )
        )

    def get_guild_subscriptions_cooldown(self) -> Response[subscriptions.PremiumGuildSubscriptionCooldown]:
        return self.request(Route('GET', '/users/@me/guilds/premium/subscriptions/cooldown'))

    def get_guild_subscription_slots(self) -> Response[List[subscriptions.PremiumGuildSubscriptionSlot]]:
        return self.request(Route('GET', '/users/@me/guilds/premium/subscription-slots'))

    def apply_guild_subscription_slots(
        self, guild_id: Snowflake, slot_ids: Sequence[Snowflake]
    ) -> Response[List[subscriptions.PremiumGuildSubscription]]:
        payload = {'user_premium_guild_subscription_slot_ids': slot_ids}

        return self.request(Route('PUT', '/guilds/{guild_id}/premium/subscriptions', guild_id=guild_id), json=payload)

    def cancel_guild_subscription_slot(self, slot_id: Snowflake) -> Response[subscriptions.PremiumGuildSubscriptionSlot]:
        return self.request(Route('POST', '/users/@me/guilds/premium/subscription-slots/{slot_id}/cancel', slot_id=slot_id))

    def uncancel_guild_subscription_slot(self, slot_id: Snowflake) -> Response[subscriptions.PremiumGuildSubscriptionSlot]:
        return self.request(
            Route('POST', '/users/@me/guilds/premium/subscription-slots/{slot_id}/uncancel', slot_id=slot_id)
        )

    def pay_invoice(
        self,
        subscription_id: Snowflake,
        invoice_id: Snowflake,
        payment_source_id: Optional[Snowflake],
        payment_source_token: Optional[str] = None,
        currency: str = 'usd',
        return_url: Optional[str] = None,
    ) -> Response[dict]:
        payload = {
            'payment_source_id': payment_source_id,
            'payment_source_token': payment_source_token,
            'currency': currency,
            'return_url': return_url,
        }

        return self.request(
            Route(
                'POST',
                '/users/@me/billing/subscriptions/{subscription_id}/invoices/{invoice_id}/pay',
                subscription_id=subscription_id,
                invoice_id=invoice_id,
            ),
            json=payload,
        )

    def get_payments(
        self, limit: int, before: Optional[Snowflake] = None, after: Optional[Snowflake] = None
    ) -> Response[List[dict]]:
        params: Dict[str, Snowflake] = {'limit': limit}
        if before:
            params['before'] = before
        if after:
            params['after'] = after

        return self.request(Route('GET', '/users/@me/billing/payments'), params=params)

    def get_payment(self, payment_id: Snowflake) -> Response[dict]:
        return self.request(Route('GET', '/users/@me/billing/payments/{payment_id}', payment_id=payment_id))

    def void_payment(self, payment_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/users/@me/billing/payments/{payment_id}/void', payment_id=payment_id))

    def refund_payment(self, payment_id: Snowflake, reason: Optional[int] = None) -> Response[None]:
        payload = {'reason': reason}

        return self.request(
            Route('POST', '/users/@me/billing/payments/{payment_id}/refund', payment_id=payment_id), json=payload
        )

    def get_promotions(self, locale: str = 'en-US') -> Response[dict]:
        params = {'locale': locale}
        return self.request(Route('GET', '/outbound-promotions'), params=params)

    def get_claimed_promotions(self, locale: str = 'en-US') -> Response[dict]:
        params = {'locale': locale}
        return self.request(Route('GET', '/users/@me/outbound-promotions/codes'), params=params)

    def claim_promotion(self, promotion_id: Snowflake) -> Response[dict]:
        return self.request(Route('POST', '/outbound-promotions/{promotion_id}/claim', promotion_id=promotion_id))

    def get_trial_offer(self) -> Response[dict]:
        return self.request(Route('GET', '/users/@me/billing/user-trial-offer'))

    def ack_trial_offer(self, trial_id: Snowflake) -> Response[None]:
        return self.request(Route('POST', '/users/@me/billing/user-trial-offer/{trial_id}/ack', trial_id=trial_id))

    def get_pricing_promotion(self) -> Response[dict]:
        return self.request(Route('GET', '/users/@me/billing/localized-pricing-promo'))

    # Misc

    async def get_gateway(self, *, encoding: str = 'json', zlib: bool = True) -> str:
        # The gateway URL hasn't changed for over 5 years
        # And, the official clients aren't GETting it anymore, sooooo...
        self.zlib = zlib
        if zlib:
            value = 'wss://gateway.discord.gg?encoding={0}&v=9&compress=zlib-stream'
        else:
            value = 'wss://gateway.discord.gg?encoding={0}&v=9'

        return value.format(encoding)

    def get_user(self, user_id: Snowflake) -> Response[user.User]:
        return self.request(Route('GET', '/users/{user_id}', user_id=user_id))

    def get_user_profile(
        self, user_id: Snowflake, guild_id: Snowflake = MISSING, *, with_mutual_guilds: bool = True
    ):  # TODO: return type
        params: Dict[str, Any] = {'with_mutual_guilds': str(with_mutual_guilds).lower()}
        if guild_id is not MISSING:
            params['guild_id'] = guild_id

        return self.request(Route('GET', '/users/{user_id}/profile', user_id=user_id), params=params)

    def get_mutual_friends(self, user_id: Snowflake):  # TODO: return type
        return self.request(Route('GET', '/users/{user_id}/relationships', user_id=user_id))

    def get_notes(self):  # TODO: return type
        return self.request(Route('GET', '/users/@me/notes'))

    def get_note(self, user_id: Snowflake):  # TODO: return type
        return self.request(Route('GET', '/users/@me/notes/{user_id}', user_id=user_id))

    def set_note(self, user_id: Snowflake, *, note: Optional[str] = None) -> Response[None]:
        payload = {'note': note or ''}

        return self.request(Route('PUT', '/users/@me/notes/{user_id}', user_id=user_id), json=payload)

    def change_hypesquad_house(self, house_id: int) -> Response[None]:
        payload = {'house_id': house_id}

        return self.request(Route('POST', '/hypesquad/online'), json=payload)

    def leave_hypesquad_house(self) -> Response[None]:
        return self.request(Route('DELETE', '/hypesquad/online'))

    def get_settings(self):  # TODO: return type
        return self.request(Route('GET', '/users/@me/settings'))

    def edit_settings(self, **payload):  # TODO: return type, is this cheating?
        return self.request(Route('PATCH', '/users/@me/settings'), json=payload)

    def get_tracking(self):  # TODO: return type
        return self.request(Route('GET', '/users/@me/consent'))

    def edit_tracking(self, payload):
        return self.request(Route('POST', '/users/@me/consent'), json=payload)

    def get_email_settings(self):
        return self.request(Route('GET', '/users/@me/email-settings'))

    def edit_email_settings(self, **payload):
        return self.request(Route('PATCH', '/users/@me/email-settings'), json={'settings': payload})

    def mobile_report(  # Report v1
        self, guild_id: Snowflake, channel_id: Snowflake, message_id: Snowflake, reason: str
    ):  # TODO: return type
        payload = {'guild_id': guild_id, 'channel_id': channel_id, 'message_id': message_id, 'reason': reason}

        return self.request(Route('POST', '/report'), json=payload)

    def get_application_commands(self, app_id: Snowflake):
        return self.request(Route('GET', '/applications/{application_id}/commands', application_id=app_id))

    def search_application_commands(
        self,
        channel_id: Snowflake,
        type: int,
        *,
        limit: Optional[int] = None,
        query: Optional[str] = None,
        cursor: Optional[str] = None,
        command_ids: Optional[List[Snowflake]] = None,
        application_id: Optional[Snowflake] = None,
        include_applications: Optional[bool] = None,
    ):
        params: Dict[str, Any] = {
            'type': type,
        }
        if include_applications is not None:
            params['include_applications'] = str(include_applications).lower()
        if limit is not None:
            params['limit'] = limit
        if query:
            params['query'] = query
        if cursor:
            params['cursor'] = cursor
        if command_ids:
            params['command_ids'] = ','.join(map(str, command_ids))
        if application_id:
            params['application_id'] = application_id

        return self.request(
            Route('GET', '/channels/{channel_id}/application-commands/search', channel_id=channel_id), params=params
        )

    def interact(
        self,
        type: InteractionType,
        data: dict,
        channel: MessageableChannel,
        message: Optional[Message] = None,
        *,
        nonce: Optional[str] = MISSING,
        application_id: Snowflake = MISSING,
        files: Optional[List[File]] = None,
    ) -> Response[None]:
        state = getattr(message, '_state', channel._state)
        payload = {
            'application_id': str((message.application_id or message.author.id) if message else application_id),
            'channel_id': str(channel.id),
            'data': data,
            'nonce': nonce if nonce is not MISSING else utils._generate_nonce(),
            'session_id': state.session_id or utils._generate_session_id(),
            'type': type.value,
        }
        if message is not None:
            payload['message_flags'] = message.flags.value
            payload['message_id'] = str(message.id)
            if message.guild:
                payload['guild_id'] = str(message.guild.id)
        else:
            guild = getattr(channel, 'guild', None)
            if guild is not None:
                payload['guild_id'] = str(guild.id)

        form = []
        if files is not None:
            form.append({'name': 'payload_json', 'value': utils._to_json(payload)})
            for index, file in enumerate(files or []):
                form.append(
                    {
                        'name': f'files[{index}]',
                        'value': file.fp,
                        'filename': file.filename,
                        'content_type': 'application/octet-stream',
                    }
                )
            payload = None

        return self.request(Route('POST', '/interactions'), json=payload, form=form, files=files)

    def get_country_code(self) -> Response[dict]:
        return self.request(Route('GET', '/users/@me/billing/country-code'))

    def get_library_entries(
        self, country_code: Optional[str] = None, payment_source_id: Optional[Snowflake] = None
    ) -> Response[List[dict]]:
        params = {}
        if country_code is not None:
            params['country_code'] = country_code
        if payment_source_id is not None:
            params['payment_source_id'] = payment_source_id

        return self.request(Route('GET', '/users/@me/library'), params=params)

    def edit_library_entry(self, app_id: Snowflake, branch_id: Snowflake, payload: dict) -> Response[dict]:
        return self.request(
            Route('PATCH', '/users/@me/library/{application_id}/{branch_id}', application_id=app_id, branch_id=branch_id),
            json=payload,
        )

    def delete_library_entry(self, app_id: Snowflake, branch_id: Snowflake) -> Response[None]:
        return self.request(
            Route('DELETE', '/users/@me/library/{application_id}/{branch_id}', application_id=app_id, branch_id=branch_id)
        )

    def mark_library_entry_installed(self, app_id: Snowflake, branch_id: Snowflake) -> Response[dict]:
        return self.request(
            Route(
                'POST',
                '/users/@me/library/{application_id}/{branch_id}/installed',
                application_id=app_id,
                branch_id=branch_id,
            )
        )

    async def get_preferred_voice_regions(self) -> List[dict]:
        async with self.__session.get('https://latency.discord.media/rtc') as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 404:
                raise NotFound(resp, 'rtc regions not found')
            elif resp.status == 403:
                raise Forbidden(resp, 'cannot retrieve rtc regions')
            else:
                raise HTTPException(resp, 'failed to get rtc regions')
