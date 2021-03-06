# -*- coding: utf-8 -*-
import bleach
import datetime
import feedparser
import hashlib
import json
import logging
import lxml.html
import magic
import oauth2 as oauth
import urllib
import urlparse
import random
import requests
import socket
import struct

from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import unescape_entities
from django.utils.translation import ugettext_lazy as _
from django_push.subscriber.signals import updated
from httplib import IncompleteRead
from lxml.etree import ParserError
from redis.exceptions import ResponseError
from requests.packages.urllib3.exceptions import LocationParseError

import pytz

from .fields import URLField
from .tasks import update_feed, update_favicon, store_entries
from .utils import FAVICON_FETCHER, USER_AGENT
from ..storage import OverwritingStorage
from ..tasks import enqueue

logger = logging.getLogger('feedupdater')

feedparser.PARSE_MICROFORMATS = False
feedparser.SANITIZE_HTML = False

COLORS = (
    ('red', _('Red')),
    ('dark-red', _('Dark Red')),
    ('pale-green', _('Pale Green')),
    ('green', _('Green')),
    ('army-green', _('Army Green')),
    ('pale-blue', _('Pale Blue')),
    ('blue', _('Blue')),
    ('dark-blue', _('Dark Blue')),
    ('orange', _('Orange')),
    ('dark-orange', _('Dark Orange')),
    ('black', _('Black')),
    ('gray', _('Gray')),
)


def random_color():
    return random.choice(COLORS)[0]


DURATIONS = (
    ('1day', _('One day')),
    ('2days', _('Two days')),
    ('1week', _('One week')),
    ('1month', _('One month')),
    ('1year', _('One year')),
)


TIMEDELTAS = {
    '1day': datetime.timedelta(days=1),
    '2days': datetime.timedelta(days=2),
    '1week': datetime.timedelta(weeks=1),
    '1month': datetime.timedelta(days=30),
    '1year': datetime.timedelta(days=365),
    #'never': None, # Implicit
}


def enqueue_favicon(url, force_update=False):
    enqueue(update_favicon, args=[url], kwargs={'force_update': force_update},
            queue='favicons')


class CategoryManager(models.Manager):
    def with_unread_counts(self):
        return self.values('id', 'name', 'slug', 'color').annotate(
            unread_count=models.Sum('feeds__unread_count'))


class Category(models.Model):
    """Used to sort our feeds"""
    name = models.CharField(_('Name'), max_length=1023, db_index=True)
    slug = models.SlugField(_('Slug'), db_index=True)
    user = models.ForeignKey(User, verbose_name=_('User'),
                             related_name='categories')
    # Some day there will be drag'n'drop ordering
    order = models.PositiveIntegerField(blank=True, null=True)

    # Categories have nice cute colors
    color = models.CharField(_('Color'), max_length=50, choices=COLORS,
                             default=random_color)

    objects = CategoryManager()

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('order', 'name', 'id')
        verbose_name_plural = 'categories'
        unique_together = (
            ('user', 'slug'),
            ('user', 'name'),
        )

    def get_absolute_url(self):
        return reverse('feeds:category', args=[self.slug])

    def save(self, *args, **kwargs):
        update_slug = kwargs.pop('update_slug', False)
        if not self.slug or update_slug:
            slug = slugify(self.name)
            if not slug:
                slug = 'unknown'
            valid = False
            candidate = slug
            num = 1
            while not valid:
                if candidate in ('add', 'import'):  # gonna conflict
                    candidate = '{0}-{1}'.format(slug, num)
                categories = self.user.categories.filter(slug=candidate)
                if self.pk is not None:
                    categories = categories.exclude(pk=self.pk)
                if categories.exists():
                    candidate = '{0}-{1}'.format(slug, num)
                    num += 1
                else:
                    valid = True
            self.slug = candidate
        return super(Category, self).save(*args, **kwargs)


class UniqueFeedManager(models.Manager):
    def update_feed(self, url, etag=None, last_modified=None, subscribers=1,
                    request_timeout=10, backoff_factor=1, previous_error=None,
                    link=None, title=None, hub=None):
        if subscribers == 1:
            subscribers_text = '1 subscriber'
        else:
            subscribers_text = '{0} subscribers'.format(subscribers)

        headers = {
            'User-Agent': USER_AGENT % subscribers_text,
            'Accept': feedparser.ACCEPT_HEADER,
        }

        if last_modified:
            headers['If-Modified-Since'] = last_modified
        if etag:
            headers['If-None-Match'] = etag

        if settings.TESTS:
            # Make sure requests.get is properly mocked during tests
            if str(type(requests.get)) != "<class 'mock.MagicMock'>":
                raise ValueError("Not Mocked")

        start = datetime.datetime.now()
        error = None
        try:
            response = requests.get(url, headers=headers,
                                    timeout=request_timeout)
        except (requests.RequestException, socket.timeout, socket.error,
                IncompleteRead) as e:
            logger.debug("Error fetching %s, %s" % (url, str(e)))
            if backoff_factor == UniqueFeed.MAX_BACKOFF - 1:
                logger.debug(
                    "%s reached max backoff period (%s)" % (url, str(e))
                )
            if isinstance(e, IncompleteRead):
                error = UniqueFeed.CONNECTION_ERROR
            else:
                error = UniqueFeed.TIMEOUT
            self.backoff_feed(url, error, backoff_factor)
            return
        except LocationParseError:
            logger.debug("Failed to parse URL for {0}".format(url))
            self.mute_feed(url, UniqueFeed.PARSE_ERROR)
            return

        elapsed = (datetime.datetime.now() - start).seconds

        ctype = response.headers.get('Content-Type', None)
        if (response.history and
            url != response.url and ctype is not None and (
                ctype.startswith('application') or
                ctype.startswith('text/xml') or
                ctype.startswith('text/rss'))):
            redirection = None
            for index, redirect in enumerate(response.history):
                if redirect.status_code != 301:
                    break
                # Actual redirection is next request's url
                try:
                    redirection = response.history[index + 1].url
                except IndexError:  # next request is final request
                    redirection = response.url

            if redirection is not None and redirection != url:
                self.handle_redirection(url, redirection, subscribers)

        update = {'last_update': timezone.now()}

        if response.status_code == 410:
            logger.debug("Feed gone, {0}".format(url))
            self.mute_feed(url, UniqueFeed.GONE)
            return

        elif response.status_code in [400, 401, 403, 404, 500, 502, 503]:
            if backoff_factor == UniqueFeed.MAX_BACKOFF - 1:
                logger.debug("{0} reached max backoff period ({1})".format(
                    url, response.status_code,
                ))
            self.backoff_feed(url, str(response.status_code), backoff_factor)
            return

        elif response.status_code not in [200, 204, 304]:
            logger.debug("{0} returned {1}".format(url, response.status_code))

        else:
            # Avoid going back to 1 directly if it isn't safe given the
            # actual response time.
            if previous_error and error is None:
                update['error'] = ''
            new_backoff = min(backoff_factor, self.safe_backoff(elapsed))
            if new_backoff != backoff_factor:
                update['backoff_factor'] = new_backoff

        if response.status_code == 304:
            logger.debug("Feed not modified, {0}".format(url))
            self.filter(url=url).update(**update)
            return

        if 'etag' in response.headers:
            update['etag'] = response.headers['etag']
        else:
            update['etag'] = ''

        if 'last-modified' in response.headers:
            update['modified'] = response.headers['last-modified']
        else:
            update['modified'] = ''

        try:
            if not response.content:
                content = ' '  # chardet won't detect encoding on empty strings
            else:
                content = response.content
        except socket.timeout:
            logger.debug('{0} timed out'.format(url))
            self.backoff_feed(url, UniqueFeed.TIMEOUT, backoff_factor)
            return
        parsed = feedparser.parse(content)

        if 'link' in parsed.feed and parsed.feed.link != link:
            update['link'] = parsed.feed.link

        if 'title' in parsed.feed and parsed.feed.title != title:
            update['title'] = parsed.feed.title

        if 'links' in parsed.feed:
            for link in parsed.feed.links:
                if link.rel == 'hub' and link.href != hub:
                    update['hub'] = link.href
                    # TODO actually subscribe

        self.filter(url=url).update(**update)

        entries = filter(
            None,
            [self.entry_data(entry, parsed) for entry in parsed.entries]
        )
        try:
            enqueue(store_entries, args=[url, entries], queue='store')
        except ResponseError:
            # Protocol error: too big bulk count string
            # Redis can't handle this. Enqueue synchronously for now.
            logger.info("Synchronously storing entries for {0}".format(url))
            store_entries(url, entries)

    @classmethod
    def entry_data(cls, entry, parsed):
        if not 'link' in entry:
            return
        title = entry.title if 'title' in entry else u''
        if len(title) > 255:  # FIXME this is gross
            title = title[:254] + u'…'
        data = {
            'title': title,
            'link': entry.link,
            'date': cls.entry_date(entry),
            'author': entry.get('author', parsed.get('author', '')),
            'guid': entry.get('id', entry.link),
        }
        if 'description' in entry:
            data['subtitle'] = entry.description
        if 'summary' in entry:
            data['subtitle'] = entry.summary
        if 'content' in entry:
            data['subtitle'] = ''
            for content in entry.content:
                data['subtitle'] += content.value
        if 'subtitle' in data:
            data['subtitle'] = u'<div>{0}</div>'.format(data['subtitle'])
        return data

    @classmethod
    def entry_date(cls, entry):
        if 'published_parsed' in entry and entry.published_parsed is not None:
            field = entry.published_parsed
        elif 'updated_parsed' in entry and entry.updated_parsed is not None:
            field = entry.updated_parsed
        else:
            field = None

        if field is None:
            entry_date = timezone.now()
        else:
            entry_date = timezone.make_aware(
                datetime.datetime(*field[:6]),
                pytz.utc,
            )
            # Sometimes entries are published in the future. If they're
            # published, it's probably safe to adjust the date.
            if entry_date > timezone.now():
                entry_date = timezone.now()
        return entry_date

    def handle_redirection(self, old_url, new_url, subscribers):
        logger.debug("{0} moved to {1}".format(old_url, new_url))
        Feed.objects.filter(url=old_url).update(url=new_url)
        unique, created = self.get_or_create(
            url=new_url, defaults={'subscribers': subscribers})
        if created and not settings.TESTS:
            enqueue_favicon(new_url)
        self.filter(url=old_url).delete()

    def mute_feed(self, url, reason):
        self.filter(url=url).update(muted=True, error=reason,
                                    last_update=timezone.now())

    def backoff_feed(self, url, error, backoff_factor):
        self.filter(url=url).update(error=error, last_update=timezone.now(),
                                    backoff_factor=min(UniqueFeed.MAX_BACKOFF,
                                                       backoff_factor + 1))

    def safe_backoff(self, response_time):
        """
        Returns the backoff factor that should be used to keep the feed
        working given the last response time. Keep a margin. Backoff time
        shouldn't increase, this is only used to avoid returning back to 10s
        if the response took more than that.
        """
        return int((response_time * 1.2) / 10) + 1


class UniqueFeed(models.Model):
    GONE = 'gone'
    TIMEOUT = 'timeout'
    PARSE_ERROR = 'parseerror'
    CONNECTION_ERROR = 'connerror'
    HTTP_400 = '400'
    HTTP_401 = '401'
    HTTP_403 = '403'
    HTTP_404 = '404'
    HTTP_500 = '500'
    HTTP_502 = '502'
    HTTP_503 = '503'
    MUTE_CHOICES = (
        (GONE, 'Feed gone (410)'),
        (TIMEOUT, 'Feed timed out'),
        (PARSE_ERROR, 'Location parse error'),
        (CONNECTION_ERROR, 'Connection error'),
        (HTTP_400, 'HTTP 400'),
        (HTTP_401, 'HTTP 401'),
        (HTTP_403, 'HTTP 403'),
        (HTTP_404, 'HTTP 404'),
        (HTTP_500, 'HTTP 500'),
        (HTTP_502, 'HTTP 502'),
        (HTTP_503, 'HTTP 503'),
    )

    url = URLField(_('URL'), unique=True)
    title = models.CharField(_('Title'), max_length=2048, blank=True)
    link = URLField(_('Link'), blank=True)
    etag = models.CharField(_('Etag'), max_length=1023, null=True, blank=True)
    modified = models.CharField(_('Modified'), max_length=1023, null=True,
                                blank=True)
    last_update = models.DateTimeField(_('Last update'), default=timezone.now,
                                       db_index=True)
    muted = models.BooleanField(_('Muted'), default=False, db_index=True)
    # Muted is only for 410, this is populated even when the feed is not
    # muted. It's more an indicator of the reason the backoff factor isn't 1.
    error = models.CharField(_('Error'), max_length=50, null=True, blank=True,
                             choices=MUTE_CHOICES, db_column='muted_reason')
    hub = URLField(_('Hub'), null=True, blank=True)
    backoff_factor = models.PositiveIntegerField(_('Backoff factor'),
                                                 default=1)
    last_loop = models.DateTimeField(_('Last loop'), default=timezone.now,
                                     db_index=True)
    subscribers = models.PositiveIntegerField(_('Subscribers'), default=1,
                                              db_index=True)

    objects = UniqueFeedManager()

    MAX_BACKOFF = 10  # Approx. 24 hours
    UPDATE_PERIOD = 60  # in minutes
    BACKOFF_EXPONENT = 1.5
    TIMEOUT_BASE = 20

    def __unicode__(self):
        if self.title:
            return u'%s' % self.title
        return u'%s' % self.url

    def backoff(self):
        self.backoff_factor = min(self.MAX_BACKOFF, self.backoff_factor + 1)

    @property
    def request_timeout(self):
        return 10 * self.backoff_factor


class Feed(models.Model):
    """A URL and some extra stuff"""
    name = models.CharField(_('Name'), max_length=1023)
    url = URLField(_('URL'))
    category = models.ForeignKey(
        Category, verbose_name=_('Category'), related_name='feeds',
        help_text=_('<a href="/category/add/">Add a category</a>'),
        null=True, blank=True,
    )
    user = models.ForeignKey(User, verbose_name=_('User'),
                             related_name='feeds')
    unread_count = models.PositiveIntegerField(_('Unread count'), default=0)
    favicon = models.ImageField(_('Favicon'), upload_to='favicons', null=True,
                                storage=OverwritingStorage())
    img_safe = models.BooleanField(_('Display images by default'),
                                   default=False)

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('name',)

    def get_absolute_url(self):
        return reverse('feeds:feed', args=[self.id])

    def save(self, *args, **kwargs):
        feed_created = self.pk is None
        super(Feed, self).save(*args, **kwargs)
        # FIXME maybe find another way to ensure consistency
        unique, created = UniqueFeed.objects.get_or_create(url=self.url)
        if feed_created or created:
            enqueue(update_feed, kwargs={
                'url': self.url,
                'subscribers': unique.subscribers,
                'request_timeout': unique.backoff_factor * 10,
                'backoff_factor': unique.backoff_factor,
                'error': unique.error,
                'link': unique.link,
                'title': unique.title,
                'hub': unique.hub,
            }, queue='high', timeout=20)
            if not settings.TESTS:
                enqueue_favicon(unique.link)

    @property
    def media_safe(self):
        return self.img_safe

    def favicon_img(self):
        if not self.favicon:
            return ''
        return format_html(
            '<img src="{0}" width="16" height="16" />', self.favicon.url)

    def update_unread_count(self):
        self.unread_count = self.entries.filter(read=False).count()
        self.save(update_fields=['unread_count'])

    @property
    def color(self):
        md = hashlib.md5()
        md.update(self.url)
        index = int(md.hexdigest()[0], 16)
        index = index * len(COLORS) // 16
        return COLORS[index][0]


class EntryManager(models.Manager):
    def unread(self):
        return self.filter(read=False).count()


class Entry(models.Model):
    """An entry is a cached feed item"""
    feed = models.ForeignKey(Feed, verbose_name=_('Feed'), null=True,
                             blank=True, related_name='entries')
    title = models.CharField(_('Title'), max_length=255)
    subtitle = models.TextField(_('Abstract'))
    link = URLField(_('URL'), db_index=True)
    author = models.CharField(_('Author'), max_length=1023, blank=True)
    date = models.DateTimeField(_('Date'), db_index=True)
    guid = URLField(_('GUID'), db_index=True, blank=True)
    # The User FK is redundant but this may be better for performance and if
    # want to allow user input.
    user = models.ForeignKey(User, verbose_name=(_('User')),
                             related_name='entries')
    # Mark something as read or unread
    read = models.BooleanField(_('Read'), default=False, db_index=True)
    # Read later: store the URL
    read_later_url = URLField(_('Read later URL'), blank=True)
    starred = models.BooleanField(_('Starred'), default=False, db_index=True)
    broadcast = models.BooleanField(_('Broadcast'), default=False,
                                    db_index=True)

    objects = EntryManager()

    class Meta:
        # Display most recent entries first
        ordering = ('-date', '-id')
        verbose_name_plural = 'entries'

    ELEMENTS = (
        feedparser._HTMLSanitizer.acceptable_elements |
        feedparser._HTMLSanitizer.mathml_elements |
        feedparser._HTMLSanitizer.svg_elements
    )
    ATTRIBUTES = (
        feedparser._HTMLSanitizer.acceptable_attributes |
        feedparser._HTMLSanitizer.mathml_attributes |
        feedparser._HTMLSanitizer.svg_attributes
    )
    CSS_PROPERTIES = feedparser._HTMLSanitizer.acceptable_css_properties

    def __unicode__(self):
        return u'%s' % self.title

    @property
    def hex_pk(self):
        value = hex(struct.unpack("L", struct.pack("l", self.pk))[0])
        if value.endswith("L"):
            value = value[:-1]
        return value[2:].zfill(16)

    def sanitized_title(self):
        if self.title:
            return unescape_entities(bleach.clean(self.title, tags=[],
                                                  strip=True))
        return _('(No title)')

    @property
    def content(self):
        if not hasattr(self, '_content'):
            if self.subtitle:
                xml = lxml.html.fromstring(self.subtitle)
                xml.make_links_absolute(self.feed.url)
                self._content = lxml.html.tostring(xml)
            else:
                self._content = self.subtitle
        return self._content

    def sanitized_content(self):
        return bleach.clean(
            self.content,
            tags=self.ELEMENTS,
            attributes=self.ATTRIBUTES,
            styles=self.CSS_PROPERTIES,
            strip=True,
        )

    def sanitized_nomedia_content(self):
        return bleach.clean(
            self.content,
            tags=self.ELEMENTS - set(['img', 'audio', 'video']),
            attributes=self.ATTRIBUTES,
            styles=self.CSS_PROPERTIES,
            strip=True,
        )

    def get_absolute_url(self):
        return reverse('feeds:item', args=[self.id])

    def link_domain(self):
        return urlparse.urlparse(self.link).netloc

    def read_later_domain(self):
        netloc = urlparse.urlparse(self.read_later_url).netloc
        return netloc.replace('www.', '')

    def read_later(self):
        """Adds this item to the user's read list"""
        user = self.user
        if not user.read_later:
            return
        getattr(self, 'add_to_%s' % self.user.read_later)()

    def add_to_readitlater(self):
        url = 'https://readitlaterlist.com/v2/add'
        data = json.loads(self.user.read_later_credentials)
        data.update({
            'apikey': settings.API_KEYS['readitlater'],
            'url': self.link,
            'title': self.title,
        })
        # The readitlater API doesn't return anything back
        requests.post(url, data=data)

    def add_to_readability(self):
        url = 'https://www.readability.com/api/rest/v1/bookmarks'
        client = self.oauth_client('readability')
        params = {'url': self.link}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        response, data = client.request(response['location'], method='GET')
        url = 'https://www.readability.com/articles/%s'
        self.read_later_url = url % json.loads(data)['article']['id']
        self.save(update_fields=['read_later_url'])

    def add_to_instapaper(self):
        url = 'https://www.instapaper.com/api/1/bookmarks/add'
        client = self.oauth_client('instapaper')
        params = {'url': self.link}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        url = 'https://www.instapaper.com/read/%s'
        url = url % json.loads(data)[0]['bookmark_id']
        self.read_later_url = url
        self.save(update_fields=['read_later_url'])

    def oauth_client(self, service):
        service_settings = getattr(settings, service.upper())
        consumer = oauth.Consumer(service_settings['CONSUMER_KEY'],
                                  service_settings['CONSUMER_SECRET'])
        creds = json.loads(self.user.read_later_credentials)
        token = oauth.Token(key=creds['oauth_token'],
                            secret=creds['oauth_token_secret'])
        client = oauth.Client(consumer, token)
        client.set_signature_method(oauth.SignatureMethod_HMAC_SHA1())
        return client


def pubsubhubbub_update(notification, **kwargs):
    url = None
    for link in notification.feed.links:
        if link['rel'] == 'self':
            url = link['href']
    if url is None:
        return

    entries = filter(
        None,
        [UniqueFeedManager.entry_data(
            entry, notification) for entry in notification.entries]
    )
    enqueue(store_entries, args=[url, entries], queue='store')
updated.connect(pubsubhubbub_update)


class FaviconManager(models.Manager):
    def update_favicon(self, link, force_update=False):
        if not link:
            return
        parsed = list(urlparse.urlparse(link))
        if not parsed[0].startswith('http'):
            return
        favicon, created = self.get_or_create(url=link)
        urls = UniqueFeed.objects.filter(link=link).values_list('url',
                                                                flat=True)
        feeds = Feed.objects.filter(url__in=urls, favicon='')
        if (not created and not force_update) and favicon.favicon:
            # Still, add to existing
            favicon_urls = list(self.filter(url=link).exclude(
                favicon='').values_list('favicon', flat=True))
            if not favicon_urls:
                return favicon

            if not feeds.exists():
                return

            feeds.update(favicon=favicon_urls[0])
            return favicon

        ua = {'User-Agent': FAVICON_FETCHER}

        try:
            page = requests.get(link, headers=ua, timeout=10).content
        except requests.RequestException:
            return favicon
        except LocationParseError:
            return favicon
        if not page:
            return favicon

        try:
            icon_path = lxml.html.fromstring(page.lower()).xpath(
                '//link[@rel="icon" or @rel="shortcut icon"]/@href'
            )
        except ParserError:
            return favicon

        if not icon_path:
            parsed[2] = '/favicon.ico'  # 'path' element
            icon_path = [urlparse.urlunparse(parsed)]
        if not icon_path[0].startswith('http'):
            parsed[2] = icon_path[0]
            parsed[3] = parsed[4] = parsed[5] = ''
            icon_path = [urlparse.urlunparse(parsed)]
        try:
            response = requests.get(icon_path[0], headers=ua, timeout=10)
        except requests.RequestException:
            return favicon
        if response.status_code != 200:
            return favicon

        icon_file = ContentFile(response.content)
        m = magic.Magic()
        icon_type = m.from_buffer(response.content)
        if 'PNG' in icon_type:
            ext = 'png'
        elif ('MS Windows icon' in icon_type or
              'Claris clip art' in icon_type):
            ext = 'ico'
        elif 'GIF' in icon_type:
            ext = 'gif'
        elif 'JPEG' in icon_type:
            ext = 'jpg'
        elif 'PC bitmap' in icon_type:
            ext = 'bmp'
        elif 'TIFF' in icon_type:
            ext = 'tiff'
        elif icon_type == 'data':
            ext = 'ico'
        elif ('HTML' in icon_type or
              icon_type == 'empty' or
              'Photoshop' in icon_type or
              'ASCII' in icon_type or
              'XML' in icon_type or
              'Unicode text' in icon_type or
              'SGML' in icon_type or
              'PHP' in icon_type or
              'very short file' in icon_type or
              'gzip compressed data' in icon_type or
              'ISO-8859 text' in icon_type or
              'PCX' in icon_type):
            logger.debug("Ignored content type for %s: %s" % (link, icon_type))
            return favicon
        else:
            logger.info("Unknown content type for %s: %s" % (link, icon_type))
            favicon.delete()
            return

        filename = '%s.%s' % (urlparse.urlparse(favicon.url).netloc, ext)
        favicon.favicon.save(filename, icon_file)

        for feed in feeds:
            feed.favicon.save(filename, icon_file)
        return favicon


class Favicon(models.Model):
    url = URLField(_('Domain URL'), db_index=True, unique=True)
    favicon = models.FileField(upload_to='favicons', blank=True,
                               storage=OverwritingStorage())

    objects = FaviconManager()

    def __unicode__(self):
        return u'Favicon for %s' % self.url

    def favicon_img(self):
        if not self.favicon:
            return '(None)'
        return '<img src="%s">' % self.favicon.url
    favicon_img.allow_tags = True
