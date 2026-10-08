"""CIPC / bureau adapters (fake / null / live) and ExternalCheck caching. Never touches the network."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.capital import adapters
from core.capital.adapters import BureauResult, CIPCResult, lookup_bureau, lookup_cipc
from core.capital.adapters.bureau import get_bureau_adapter
from core.capital.adapters.cipc import get_cipc_adapter
from core.integrations import bureau_adapter as integration
from core.models import DebtorIdentity, ExternalCheck

FAKE = dict(CAPITAL_CIPC_ADAPTER='fake', CAPITAL_BUREAU_ADAPTER='fake')


def debtor(reg='2004/111111/07', **kw):
    return DebtorIdentity.objects.create(registration_number=reg, **kw)


class FakeAdapterTests(TestCase):
    def test_fixture_rows(self):
        a = get_cipc_adapter('fake')
        ok = a.lookup('2004/111111/07')
        self.assertTrue(ok.available and ok.is_fake)
        self.assertEqual(ok.status, 'IN_BUSINESS')
        self.assertEqual(ok.incorporation_date, date(2004, 3, 15))
        self.assertEqual(a.lookup('2011/222222/07').status, 'BUSINESS_RESCUE')
        self.assertEqual(a.lookup('2009/333333/07').status, 'LIQUIDATION')
        young = a.lookup('2025/444444/07')
        self.assertEqual(young.incorporation_date.year, 2025)
        b = get_bureau_adapter('fake')
        self.assertEqual(b.lookup(registration_number='2004/111111/07').score, 78)
        y = b.lookup(registration_number='2025/444444/07')
        self.assertEqual((y.score, y.judgments), (45, 2))
        self.assertFalse(b.lookup(registration_number='2015/555555/07').available)

    def test_unknown_number_is_deterministic_and_marked_fake(self):
        a, b = get_cipc_adapter('fake'), get_bureau_adapter('fake')
        one, two = a.lookup('2019/987654/07'), a.lookup('2019/987654/07')
        self.assertEqual(one.to_payload(), two.to_payload())
        self.assertTrue(one.is_fake and one.raw['derived'])
        self.assertEqual(one.status, 'IN_BUSINESS')
        s1 = b.lookup(registration_number='2019/987654/07')
        self.assertEqual(s1.score, b.lookup(registration_number='2019/987654/07').score)
        self.assertTrue(35 <= s1.score <= 84 and s1.is_fake)
        self.assertNotEqual(a.lookup('2018/111222/07').incorporation_date, None)


class NullAndLiveAdapterTests(TestCase):
    def test_null(self):
        self.assertFalse(get_cipc_adapter('null').lookup('2004/111111/07').available)
        self.assertFalse(get_bureau_adapter('null').lookup(registration_number='2004/111111/07').available)
        # unknown adapter name falls back to null
        self.assertEqual(get_cipc_adapter('nonsense').name, 'null')

    def test_live_cipc_is_unavailable_and_never_raises(self):
        res = get_cipc_adapter('live').lookup('2004/111111/07')
        self.assertFalse(res.available)
        self.assertIn('not contracted', res.note)

    def test_live_bureau_maps_the_integration(self):
        stub = mock.Mock()
        stub.lookup.return_value = integration.BureauResult(
            available=True, score=66, source='EXPERIAN', raw={'judgments': 1, 'score': 66})
        with mock.patch('core.integrations.bureau_adapter.get_bureau_provider', return_value=stub):
            res = get_bureau_adapter('live').lookup(registration_number='2004/111111/07', name='Highveld')
        self.assertEqual((res.available, res.score, res.judgments), (True, 66, 1))
        self.assertEqual(res.source, 'live:EXPERIAN')
        self.assertFalse(res.is_fake)
        stub.lookup.assert_called_once_with(registration_number='2004/111111/07', name='Highveld', vat_number=None)

    def test_live_bureau_unconfigured_is_honest_no_data(self):
        with mock.patch('core.integrations.bureau_adapter.get_bureau_provider',
                        return_value=integration.NullProvider()):
            res = get_bureau_adapter('live').lookup(registration_number='2004/111111/07')
        self.assertFalse(res.available)
        self.assertIsNone(res.score)

    def test_live_bureau_error_degrades(self):
        with mock.patch('core.integrations.bureau_adapter.get_bureau_provider', side_effect=RuntimeError('x')):
            res = get_bureau_adapter('live').lookup(registration_number='2004/111111/07')
        self.assertFalse(res.available)


class LookupCachingTests(TestCase):
    @override_settings(**FAKE)
    def test_cipc_lookup_writes_one_row_and_updates_identity(self):
        d = debtor()
        res = lookup_cipc(d)
        self.assertEqual(res.status, 'IN_BUSINESS')
        d.refresh_from_db()
        self.assertEqual(d.cipc_status, 'IN_BUSINESS')
        self.assertIsNotNone(d.cipc_checked_at)
        self.assertEqual(d.incorporation_date, date(2004, 3, 15))
        self.assertEqual(d.legal_name, 'Highveld Fresh Retail (Pty) Ltd')
        row = ExternalCheck.objects.get(debtor=d, provider='CIPC')
        self.assertEqual((row.adapter, row.is_fake, row.status, row.available), ('fake', True, 'IN_BUSINESS', True))
        self.assertEqual(row.cost_zar, Decimal('0.00'))
        # second call within 30 days reuses the stored check
        again = lookup_cipc(d)
        self.assertEqual(again.status, 'IN_BUSINESS')
        self.assertEqual(again.incorporation_date, date(2004, 3, 15))
        self.assertEqual(ExternalCheck.objects.filter(debtor=d, provider='CIPC').count(), 1)
        # older than 30 days -> a fresh lookup
        ExternalCheck.objects.filter(pk=row.pk).update(fetched_at=timezone.now() - timedelta(days=31))
        lookup_cipc(d)
        self.assertEqual(ExternalCheck.objects.filter(debtor=d, provider='CIPC').count(), 2)

    @override_settings(**FAKE)
    def test_existing_legal_name_is_not_overwritten(self):
        d = debtor('2011/222222/07', legal_name='Karoo Build (desk name)')
        lookup_cipc(d)
        d.refresh_from_db()
        self.assertEqual(d.legal_name, 'Karoo Build (desk name)')
        self.assertEqual(d.cipc_status, 'BUSINESS_RESCUE')

    @override_settings(**FAKE)
    def test_bureau_lookup_cached(self):
        d = debtor('2025/444444/07')
        res = lookup_bureau(d)
        self.assertEqual((res.score, res.judgments, res.is_fake), (45, 2, True))
        self.assertEqual(lookup_bureau(d).score, 45)
        row = ExternalCheck.objects.get(debtor=d, provider='BUREAU')
        self.assertEqual((row.score, row.is_fake, row.adapter), (45, True, 'fake'))

    def test_no_registration_number_never_calls_adapter(self):
        d = DebtorIdentity.objects.create(vat_number='4111111111')
        with override_settings(**FAKE), \
                mock.patch.object(adapters, 'get_cipc_adapter') as gc, \
                mock.patch.object(adapters, 'get_bureau_adapter') as gb:
            self.assertFalse(lookup_cipc(d).available)
            self.assertFalse(lookup_bureau(d).available)
        gc.assert_not_called()
        gb.assert_not_called()
        self.assertFalse(ExternalCheck.objects.filter(debtor=d).exists())

    def test_null_adapter_result_cached_for_a_day_and_identity_untouched(self):
        d = debtor()
        with override_settings(CAPITAL_CIPC_ADAPTER='null'):
            self.assertFalse(lookup_cipc(d).available)
            lookup_cipc(d)
            self.assertEqual(ExternalCheck.objects.filter(debtor=d, provider='CIPC').count(), 1)
            ExternalCheck.objects.filter(debtor=d).update(fetched_at=timezone.now() - timedelta(days=2))
            lookup_cipc(d)
            self.assertEqual(ExternalCheck.objects.filter(debtor=d, provider='CIPC').count(), 2)
        d.refresh_from_db()
        self.assertEqual(d.cipc_status, 'UNKNOWN')
        self.assertIsNone(d.cipc_checked_at)

    def test_switching_adapter_does_not_reuse_other_adapters_rows(self):
        d = debtor()
        with override_settings(CAPITAL_CIPC_ADAPTER='null'):
            lookup_cipc(d)
        with override_settings(CAPITAL_CIPC_ADAPTER='fake'):
            self.assertTrue(lookup_cipc(d).available)
        self.assertEqual(ExternalCheck.objects.filter(debtor=d, provider='CIPC').count(), 2)

    def test_payload_round_trip(self):
        c = CIPCResult(available=True, status='LIQUIDATION', incorporation_date=date(2009, 5, 20), source='x')
        self.assertEqual(CIPCResult.from_payload(c.to_payload()), c)
        b = BureauResult(available=True, score=50, judgments=3, source='x')
        self.assertEqual(BureauResult.from_payload(b.to_payload()), b)
        self.assertNotIn('raw', c.summary())
