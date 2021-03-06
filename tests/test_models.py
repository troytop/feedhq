from django.test import TestCase
from django.utils import timezone
from mock import patch

from feedhq.feeds.models import Category, Feed, UniqueFeed, Entry
from feedhq.feeds.tasks import update_feed

from .factories import CategoryFactory, FeedFactory
from . import responses


class ModelTests(TestCase):
    def test_category_model(self):
        """Behaviour of the ``Category`` model"""
        cat = CategoryFactory.create(name='New Cat', slug='new-cat')

        cat_from_db = Category.objects.get(pk=cat.id)

        # __unicode__
        self.assertEqual('%s' % cat_from_db, 'New Cat')

        # get_absolute_url()
        self.assertEqual('/category/new-cat/', cat_from_db.get_absolute_url())

    @patch('requests.get')
    def test_feed_model(self, get):
        """Behaviour of the ``Feed`` model"""
        get.return_value = responses(200, 'rss20.xml')
        feed = FeedFactory.create(name='RSS test', url='rss20.xml')
        feed.save()

        feed_from_db = Feed.objects.get(pk=feed.id)

        # __unicode__
        self.assertEqual('%s' % feed_from_db, 'RSS test')

        # get_absolute_url()
        self.assertEqual('/feed/%s/' % feed.id, feed.get_absolute_url())

        # update()
        update_feed(feed.url)

        unique_feed = UniqueFeed.objects.get(url=feed.url)
        self.assertEqual(unique_feed.title, 'Sample Feed')
        self.assertEqual(unique_feed.link, 'http://example.org/')

        feed = Feed.objects.get(pk=feed.id)
        self.assertEqual(feed.entries.count(), 1)
        self.assertEqual(feed.entries.all()[0].title, 'First item title')

    @patch('requests.get')
    def test_entry_model(self, get):
        get.return_value = responses(200, 'sw-all.xml')
        feed = FeedFactory.create()
        update_feed(feed.url)
        title = 'RE2: a principled approach to regular expression matching'
        entry = Entry.objects.get(title=title)

        # __unicode__
        self.assertEqual('%s' % entry, title)

    @patch("requests.get")
    def test_entry_model_behaviour(self, get):
        """Behaviour of the `Entry` model"""
        get.return_value = responses(304)
        feed = FeedFactory.create()
        entry = feed.entries.create(title='My title',
                                    user=feed.category.user,
                                    date=timezone.now())
        # __unicode__
        self.assertEqual('%s' % entry, 'My title')

        # get_absolute_url()
        self.assertEqual('/entries/%s/' % entry.id, entry.get_absolute_url())

    @patch("requests.get")
    def test_handle_etag(self, get):
        get.return_value = responses(200, headers={'etag': 'foo',
                                                   'last-modified': 'bar'})
        FeedFactory.create()
        feed = UniqueFeed.objects.get()
        self.assertEqual(feed.etag, 'foo')
        self.assertEqual(feed.modified, 'bar')
