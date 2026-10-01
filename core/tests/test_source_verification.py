"""Tests for core.services.source_verification — the check that a cited
market figure actually appears on its source page."""

import io
import socket
from unittest import mock

from django.core.cache import cache, caches
from django.test import SimpleTestCase, TestCase

import requests

from core.services import source_verification as sv


def _addrinfo(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]


class FakeResponse:
    def __init__(self, status=200, body=b'', content_type='text/html', location=None):
        self.status_code = status
        self._body = body
        self.headers = {'Content-Type': content_type}
        if location:
            self.headers['Location'] = location
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True


class NumberMatchingTests(SimpleTestCase):
    def assertOnPage(self, value, text, **kw):
        self.assertTrue(sv.number_on_page(value, text, **kw), f'{value!r} should match {text!r}')

    def assertNotOnPage(self, value, text, **kw):
        self.assertFalse(sv.number_on_page(value, text, **kw), f'{value!r} should NOT match {text!r}')

    def test_decimal_formats(self):
        self.assertOnPage(29.11, 'Diesel R29.11/L')
        self.assertOnPage(29.11, 'Diesel R29,11 per litre')
        self.assertOnPage(29.11, 'Diesel 29.1143 c')        # page more precise than the model
        self.assertOnPage(29.10, 'Diesel R29.1 per litre')
        self.assertNotOnPage(29.11, 'Diesel R29.12')
        self.assertNotOnPage(29.10, 'Diesel R29.15')

    def test_thousands_formats(self):
        for text in ('R1454', 'R1,454', 'R1 454', 'R1.454', 'R1454.00', 'R1454,0'):
            self.assertOnPage(1454, text)
        self.assertNotOnPage(1454, 'R1454.50')
        self.assertNotOnPage(1454, 'R11454')

    def test_not_part_of_a_longer_number(self):
        self.assertNotOnPage(950, 'code 1950 applies')
        self.assertNotOnPage(950, 'ref 2.950')
        self.assertNotOnPage(29.11, '129.11')

    def test_fuel_cents_per_litre(self):
        self.assertOnPage(29.11, 'Diesel 2911.43 c/l', allow_cents=True)
        self.assertOnPage(29.11, 'Diesel 2 911 c/l', allow_cents=True)
        self.assertNotOnPage(29.11, 'Diesel 2911.43 c/l')   # only when allowed

    def test_check_figure_reasons(self):
        pages = {'https://a.test': {'text': 'Rate R243.63 per day', 'error': None},
                 'https://b.test': {'text': None, 'error': 'http 403'}}
        self.assertEqual(sv.check_figure(243.63, ['https://a.test'], pages), (True, 'checked on source page'))
        self.assertEqual(sv.check_figure(300, ['https://a.test'], pages), (False, sv.REASON_NOT_FOUND))
        self.assertEqual(sv.check_figure(243.63, ['https://b.test'], pages), (False, sv.REASON_UNREADABLE))
        self.assertEqual(sv.check_figure(243.63, [], pages), (False, sv.REASON_NO_SOURCE))
        # One readable page is enough; an unreadable sibling doesn't block it.
        self.assertTrue(sv.check_figure(243.63, ['https://b.test', 'https://a.test'], pages)[0])


class PublicUrlTests(SimpleTestCase):
    def test_scheme_must_be_http(self):
        self.assertFalse(sv.is_public_url('file:///etc/passwd'))
        self.assertFalse(sv.is_public_url('ftp://example.com/x'))

    def test_private_and_loopback_hosts_are_blocked(self):
        for ip in ('127.0.0.1', '10.0.0.5', '192.168.1.1', '169.254.169.254', '172.16.0.1', '0.0.0.0'):
            with mock.patch('socket.getaddrinfo', return_value=_addrinfo(ip)):
                self.assertFalse(sv.is_public_url('https://looks-public.test/'), ip)

    def test_any_private_address_among_several_blocks(self):
        infos = _addrinfo('93.184.216.34') + _addrinfo('10.0.0.1')
        with mock.patch('socket.getaddrinfo', return_value=infos):
            self.assertFalse(sv.is_public_url('https://mixed.test/'))

    def test_public_host_is_allowed(self):
        with mock.patch('socket.getaddrinfo', return_value=_addrinfo('93.184.216.34')):
            self.assertTrue(sv.is_public_url('https://example.com/'))

    def test_only_standard_web_ports(self):
        with mock.patch('socket.getaddrinfo', return_value=_addrinfo('93.184.216.34')):
            for url in ('http://1.1.1.1:22/', 'https://example.com:8443/', 'http://example.com:443/',
                        'https://example.com:80/'):
                self.assertFalse(sv.is_public_url(url), url)
            for url in ('https://example.com:443/tariffs', 'http://example.com:80/', 'https://example.com/'):
                self.assertTrue(sv.is_public_url(url), url)

    def test_unresolvable_host_is_blocked(self):
        with mock.patch('socket.getaddrinfo', side_effect=socket.gaierror):
            self.assertFalse(sv.is_public_url('https://nope.test/'))


def _session_get(**kwargs):
    return mock.patch.object(requests.Session, 'get', **kwargs)


class FetchTests(SimpleTestCase):
    def _public(self, hostname_to_ip):
        def resolver(host, *a, **kw):
            return _addrinfo(hostname_to_ip.get(host, '93.184.216.34'))
        return mock.patch('socket.getaddrinfo', side_effect=resolver)

    def test_html_page_text_is_extracted(self):
        html = b'<html><script>var x=999;</script><body><p>Class 4: R 950.00</p></body></html>'
        with self._public({}), _session_get(return_value=FakeResponse(body=html)):
            result = sv._fetch_uncached('https://sanral.test/tariffs')
        self.assertIsNone(result['error'])
        self.assertIn('950.00', result['text'])
        self.assertNotIn('999', result['text'])

    def test_redirect_to_private_ip_is_blocked(self):
        redirect = FakeResponse(status=302, location='http://internal.test/admin')
        with self._public({'internal.test': '10.0.0.8'}), \
                _session_get(return_value=redirect) as get:
            result = sv._fetch_uncached('https://public.test/start')
        self.assertEqual(result, {'text': None, 'error': 'blocked host'})
        self.assertEqual(get.call_count, 1)  # never requested the private host
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_redirect_to_public_host_is_followed(self):
        responses = [FakeResponse(status=301, location='/new'), FakeResponse(body=b'<p>R243.63</p>')]
        with self._public({}), _session_get(side_effect=responses) as get:
            result = sv._fetch_uncached('https://public.test/old')
        self.assertIn('243.63', result['text'])
        self.assertEqual(get.call_args.args[0], 'https://93.184.216.34:443/new')
        self.assertEqual(get.call_args.kwargs['headers']['Host'], 'public.test')

    def test_too_many_redirects(self):
        loop = FakeResponse(status=302, location='/again')
        with self._public({}), _session_get(return_value=loop):
            self.assertEqual(sv._fetch_uncached('https://public.test/')['error'], 'too many redirects')

    def test_non_200_and_oversized_pages_fail(self):
        with self._public({}), _session_get(return_value=FakeResponse(status=403)):
            self.assertEqual(sv._fetch_uncached('https://public.test/')['error'], 'http 403')
        big = FakeResponse(body=b'x' * (sv.MAX_BYTES + 10), content_type='text/plain')
        with self._public({}), _session_get(return_value=big):
            self.assertEqual(sv._fetch_uncached('https://public.test/')['error'], 'page too large')

    def test_network_error_is_reported_not_raised(self):
        with self._public({}), _session_get(side_effect=requests.Timeout):
            self.assertEqual(sv._fetch_uncached('https://public.test/')['error'], 'fetch failed: Timeout')

    def test_pdf_text_is_extracted(self):
        fake_reader = mock.Mock()
        fake_reader.pages = [mock.Mock(extract_text=mock.Mock(return_value='Grasmere Class 4 R 950.00'))]
        pdf = FakeResponse(body=b'%PDF-1.7 fake', content_type='application/pdf')
        with self._public({}), _session_get(return_value=pdf), \
                mock.patch('pypdf.PdfReader', return_value=fake_reader) as reader:
            result = sv._fetch_uncached('https://sanral.test/tariffs.pdf')
        self.assertTrue(reader.called)
        self.assertTrue(sv.number_on_page(950, result['text']))

    def test_connection_is_pinned_to_the_checked_ip(self):
        with self._public({'sanral.test': '41.78.143.133'}), \
                _session_get(return_value=FakeResponse(body=b'<p>R 950.00</p>')) as get:
            sv._fetch_uncached('https://sanral.test/tariffs?year=2026')
        self.assertEqual(get.call_args.args[0], 'https://41.78.143.133:443/tariffs?year=2026')
        self.assertEqual(get.call_args.kwargs['headers']['Host'], 'sanral.test')

    def test_url_parsed_differently_by_the_http_client_is_blocked(self):
        # urlparse sees example.com; urllib3 would connect to 127.0.0.1:8000.
        with self._public({}), _session_get(return_value=FakeResponse(body=b'secret')) as get:
            self.assertFalse(sv.is_public_url('http://127.0.0.1:8000\\@example.com/'))
            self.assertEqual(sv._fetch_uncached('http://127.0.0.1:8000\\@example.com/')['error'], 'blocked host')
            self.assertFalse(sv.is_public_url('https://user:pw@example.com/'))
        self.assertEqual(get.call_count, 0)

    def test_redirect_with_a_parser_trick_is_blocked(self):
        redirect = FakeResponse(status=302, location='http://127.0.0.1:8000\\@example.com/')
        with self._public({}), _session_get(return_value=redirect) as get:
            self.assertEqual(sv._fetch_uncached('https://public.test/start')['error'], 'blocked host')
        self.assertEqual(get.call_count, 1)

    def test_a_page_that_drips_bytes_hits_the_total_deadline(self):
        slow = FakeResponse(body=b'x' * 200_000, content_type='text/plain')
        with self._public({}), _session_get(return_value=slow), mock.patch.object(sv, 'FETCH_TOTAL_SECONDS', -1):
            self.assertEqual(sv._fetch_uncached('https://public.test/')['error'], 'timed out')

    def test_real_pdf_round_trip(self):
        """A genuine (blank) PDF built by pypdf parses without error."""
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        self.assertEqual(sv._extract_text(buf.getvalue(), 'application/pdf').strip(), '')


class FetchBatchCacheTests(TestCase):  # the default cache backend is DB-backed
    def setUp(self):
        cache.clear()
        caches[sv.SOURCE_CACHE_ALIAS].clear()
        self.addCleanup(caches[sv.SOURCE_CACHE_ALIAS].clear)

    def test_pages_never_go_into_the_shared_default_cache(self):
        ok = {'text': 'R29.11 ' * 1000, 'error': None}
        with mock.patch.object(sv, '_fetch_uncached', return_value=ok):
            sv.SourceFetchBatch(['https://a.test']).result()
        self.assertIsNone(cache.get(sv._cache_key('https://a.test')))
        self.assertEqual(caches[sv.SOURCE_CACHE_ALIAS].get(sv._cache_key('https://a.test')), ok)

    def test_no_source_cache_configured_means_no_caching_not_the_default_cache(self):
        from django.conf import settings
        from django.test import override_settings
        ok = {'text': 'R29.11', 'error': None}
        only_default = {'default': settings.CACHES['default']}
        with override_settings(CACHES=only_default), \
                mock.patch.object(sv, '_fetch_uncached', return_value=ok) as fetch:
            sv.SourceFetchBatch(['https://b.test']).result()
            sv.SourceFetchBatch(['https://b.test']).result()
            self.assertIsNone(cache.get(sv._cache_key('https://b.test')))
        self.assertEqual(fetch.call_count, 2)

    def test_results_are_cached_and_reused(self):
        ok = {'text': 'R29.11', 'error': None}
        with mock.patch.object(sv, '_fetch_uncached', return_value=ok) as fetch:
            first = sv.SourceFetchBatch(['https://a.test', 'https://a.test']).result()
            second = sv.SourceFetchBatch(['https://a.test']).result()
        self.assertEqual(fetch.call_count, 1)  # de-duplicated, then served from cache
        self.assertEqual(first, second)
        self.assertEqual(second['https://a.test'], ok)

    def test_empty_batch(self):
        self.assertEqual(sv.SourceFetchBatch([]).result(), {})

    def test_a_page_still_loading_at_the_deadline_is_unreadable_and_not_cached(self):
        import threading
        release = threading.Event()

        def slow(url):
            release.wait(5)
            return {'text': 'R29.11', 'error': None}
        try:
            with mock.patch.object(sv, '_fetch_uncached', side_effect=slow):
                pages = sv.SourceFetchBatch(['https://slow.test']).result(timeout=0.05)
        finally:
            release.set()
        self.assertEqual(pages['https://slow.test'], {'text': None, 'error': 'timed out'})
        self.assertIsNone(caches[sv.SOURCE_CACHE_ALIAS].get(sv._cache_key('https://slow.test')))
        self.assertEqual(sv.check_figure(29.11, ['https://slow.test'], pages)[1], sv.REASON_TIMED_OUT)


class DatedFigureTests(SimpleTestCase):
    def test_figure_must_share_its_page_with_its_schedule_date(self):
        pages = {'https://a.test': {'text': 'Tariffs 1 March 2025 - 28 February 2026: Mooi R324.00. (c) 2026',
                                    'error': None},
                 'https://b.test': {'text': 'New toll tariffs from 1 MARCH 2026. Mooi R324.00', 'error': None}}
        self.assertEqual(sv.check_figure(324, ['https://a.test'], pages, near_any=('March 2026',)),
                         (False, sv.REASON_DATE_NOT_FOUND))
        self.assertTrue(sv.check_figure(324, ['https://a.test', 'https://b.test'], pages, near_any=('March 2026',))[0])

    def test_near_chars_limits_how_far_the_date_may_be(self):
        text = 'From 1 March 2026. ' + 'filler ' * 80 + 'allowance R243.63 per day'
        pages = {'https://a.test': {'text': text, 'error': None}}
        self.assertTrue(sv.check_figure(243.63, ['https://a.test'], pages, near_any=('March 2026',))[0])
        self.assertFalse(sv.check_figure(243.63, ['https://a.test'], pages, near_any=('March 2026',),
                                         near_chars=300)[0])

    def test_preceding_year_picks_the_right_table_row(self):
        pages = {'https://sars.test': {'text': 'Year | meals | incidentals 2027 R595 R184 2026 R570 R176', 'error': None}}
        self.assertTrue(sv.check_figure(595, ['https://sars.test'], pages, preceding_year=2027)[0])
        self.assertEqual(sv.check_figure(570, ['https://sars.test'], pages, preceding_year=2027),
                         (False, sv.REASON_DATE_NOT_FOUND))
