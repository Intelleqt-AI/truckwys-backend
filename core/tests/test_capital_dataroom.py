"""Fast Pay monthly data room: files, columns, sums, hash stability, period
validation, registration numbers instead of tenant customer names."""
import csv
import io
import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone

from core.capital import dataroom
from core.models import AuditLog, CapitalLimit, DataRoomExport
from core.tests.test_capital_monitoring import expose, make_book, make_funder

D = Decimal


def read_csv(path):
    with default_storage.open(path, 'rb') as fh:
        return list(csv.DictReader(io.StringIO(fh.read().decode('utf-8'))))


class DataRoomTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix='fp-dataroom-')
        cls._override = override_settings(MEDIA_ROOT=cls._media, CAPITAL_DATA_ROOM_PREFIX='test-dr')
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.b = make_book(reg='2001/777777/07', customer_name='Tenant Secret Name')
        self.f = self.b['funder']
        self.period = timezone.localdate().strftime('%Y-%m')

    def test_files_columns_and_sums(self):
        x = dataroom.generate(self.f, period=self.period)
        self.assertEqual(set(x.files), set(dataroom.FILES))
        for name, path in x.files.items():
            self.assertTrue(default_storage.exists(path), name)
            self.assertTrue(path.startswith(f'test-dr/{self.f.code}/{self.period}/'))

        tape = read_csv(x.files['loan_tape.csv'])
        self.assertEqual(list(tape[0].keys()), dataroom.LOAN_TAPE_COLUMNS)
        self.assertEqual(len(tape), 1)
        row = tape[0]
        adv = self.b['advance']
        self.assertEqual(row['reference'], f'FP-{adv.pk:06d}')
        self.assertEqual(row['debtor_registration_number'], '2001/777777/07')
        self.assertEqual(row['invoice_face_incl_vat'], '115000.00')
        self.assertEqual(row['invoice_face_excl_vat'], '100000.00')
        self.assertEqual(row['advance_amount'], '90000.00')
        self.assertEqual(row['fee_amount'], '1800.00')
        self.assertEqual(row['status'], 'DISBURSED')
        self.assertEqual(row['disbursed_date'], timezone.localdate().isoformat())

        ledger_rows = read_csv(x.files['ledger.csv'])
        self.assertEqual(list(ledger_rows[0].keys()), dataroom.LEDGER_COLUMNS)
        by_type = {}
        for r in ledger_rows:
            by_type[r['entry_type']] = by_type.get(r['entry_type'], D('0')) + D(r['amount'])
        self.assertEqual(by_type['DISBURSE'], D('90000.00'))
        self.assertEqual(by_type['FEE'], D('1800.00'))
        self.assertEqual(sum(D(r['outstanding_delta']) for r in ledger_rows), D('90000.00'))

        exp = read_csv(x.files['exposures.csv'])
        self.assertEqual(list(exp[0].keys()), dataroom.EXPOSURE_COLUMNS)
        debtor_row = next(r for r in exp if r['dimension'] == 'debtor')
        self.assertEqual((debtor_row['label'], debtor_row['committed']), ('2001/777777/07', '90000.00'))
        self.assertEqual({r['dimension'] for r in exp}, {'debtor', 'transporter', 'sector'})

        for name in ('decisions.csv', 'alerts.csv'):
            with default_storage.open(x.files[name], 'rb') as fh:
                header = fh.read().decode().splitlines()[0].split(',')
            self.assertEqual(header, dataroom.DECISION_COLUMNS if name == 'decisions.csv' else dataroom.ALERT_COLUMNS)

        with default_storage.open(x.files['summary.json'], 'rb') as fh:
            summary = json.loads(fh.read())
        self.assertEqual(summary, x.summary)
        self.assertEqual(summary['disbursed'], {'count': 1, 'amount': '90000.00'})
        self.assertEqual(summary['fees'], '1800.00')
        self.assertEqual(summary['outstanding_at_period_end']['outstanding'], '90000.00')
        self.assertEqual(summary['top10_share_at_period_end'], '1.0000')
        self.assertTrue(summary['reconciliation_at_export']['ok'])
        self.assertEqual(summary['advances']['in_loan_tape'], 1)

    def test_loan_tape_never_carries_tenant_customer_names(self):
        x = dataroom.generate(self.f, period=self.period)
        for name, path in x.files.items():
            with default_storage.open(path, 'rb') as fh:
                self.assertNotIn(b'Tenant Secret Name', fh.read(), name)

    def test_hash_is_stable_and_regeneration_overwrites(self):
        x1 = dataroom.generate(self.f, period=self.period)
        x2 = dataroom.generate(self.f, period=self.period)
        self.assertEqual(x1.content_hash, x2.content_hash)
        self.assertEqual(x1.files, x2.files)  # same paths, overwritten (no _abc123 suffixes)
        self.assertEqual(DataRoomExport.objects.filter(funder=self.f, period=self.period).count(), 2)
        expose(self.f, '1000', debtor=self.b['debtor'])
        self.assertNotEqual(dataroom.generate(self.f, period=self.period).content_hash, x1.content_hash)

    def test_period_validation(self):
        for bad in ('2026-13', '2026-1', 'abc', '1999-01'):
            with self.assertRaises(ValueError):
                dataroom.generate(self.f, period=bad)
        future = (timezone.localdate().replace(day=1) + timedelta(days=40)).strftime('%Y-%m')
        with self.assertRaisesMessage(ValueError, 'future'):
            dataroom.generate(self.f, period=future)
        x = dataroom.generate(self.f)  # default: previous month, nothing happened yet
        self.assertEqual(x.period, dataroom.previous_period())
        self.assertEqual(x.summary['advances']['in_loan_tape'], 0)
        self.assertEqual(x.summary['outstanding_at_period_end']['committed'], '0.00')

    def test_overrides_are_listed_and_export_is_audited(self):
        lim = CapitalLimit.objects.create(funder=self.f, scope='DEBTOR', debtor=self.b['debtor'],
                                          amount=D('5000'), reason='test')
        AuditLog.objects.create(action='OVERRIDE', resource_type='CapitalLimit', resource_id=str(lim.pk),
                                details={'funder_id': self.f.pk, 'scope': 'DEBTOR'})
        other = make_funder()
        AuditLog.objects.create(action='OVERRIDE', resource_type='CapitalLimit', resource_id='999',
                                details={'funder_id': other.pk})
        x = dataroom.generate(self.f, period=self.period)
        overrides = x.summary['eligibility_exceptions_and_overrides']
        self.assertEqual([o['resource_id'] for o in overrides], [str(lim.pk)])
        self.assertTrue(AuditLog.objects.filter(action='EXPORT', resource_type='DataRoomExport',
                                                resource_id=str(x.pk)).exists())
