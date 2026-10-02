"""Replay the golden ledger dataset (core/tests/fixtures/golden_ledger.json)
into TruckWys through the real services.

Reusable: the Xero / QuickBooks sync tests load the same dataset with
load_golden_dataset() and then compare what the accounting system reports
against dataset['expected'].

    company = create_golden_company()
    golden = load_golden_dataset(company)
    golden.invoices['INV03']        # -> core.models.Invoice
    golden.lines['INV03-L2']        # -> core.models.InvoiceLine
    golden.payments['P04']          # -> core.models.Payment (None if deleted)
    golden.credit_notes['CN04']     # -> core.models.CreditNote
    golden.expenses['E11']          # -> core.models.Expense

Every write goes through the same code path a user would hit:
invoices are drafted with apply_lines() and issued with mark_as_sent();
payments use record_payment / update_payment / reverse_payment; credit notes
and voids use core.services.credit_notes; expenses use ExpenseSerializer (so
input VAT is derived by the app unless the receipt states it).
"""
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from unittest import mock

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), 'fixtures', 'golden_ledger.json')


def load_dataset(path=FIXTURE_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


@dataclass
class GoldenLedger:
    dataset: dict
    company: object
    user: object
    customers: dict = field(default_factory=dict)
    suppliers: dict = field(default_factory=dict)
    invoices: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)
    payments: dict = field(default_factory=dict)
    credit_notes: dict = field(default_factory=dict)
    expenses: dict = field(default_factory=dict)

    @property
    def expected(self):
        return self.dataset['expected']

    def refresh(self):
        """Re-read every created row (services save fresh copies)."""
        for m in (self.invoices, self.payments, self.credit_notes, self.expenses):
            for k, obj in m.items():
                if obj is not None:
                    obj.refresh_from_db()
        return self


@contextmanager
def frozen_ledger_today(day: date):
    """Invoice status (SENT vs OVERDUE) is derived on save from
    date.today() in core.services.ledger. Freeze that clock so the derived
    statuses are reproducible whatever day the tests run."""
    real = date

    class _FrozenDate(real):
        @classmethod
        def today(cls):
            return day

    with mock.patch('core.services.ledger.date', _FrozenDate):
        yield


def create_golden_company(dataset=None, **overrides):
    from core.models import Company
    ds = dataset or load_dataset()
    c = ds['company']
    kw = dict(company_name=c['name'], vat_number=c['vat_number'], vat_registered=c['vat_registered'])
    kw.update(overrides)
    return Company.objects.create(**kw)


def _default_user(company):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    u = User.objects.create_user(username=f'golden_admin_{company.pk}', email=f'golden{company.pk}@golden.test',
                                 password='golden-test-only')
    u.role = 'ADMIN'
    u.company = company
    u.save()
    return u


def load_golden_dataset(company, user=None, *, dataset=None, today=None) -> GoldenLedger:
    """Build the whole dataset for `company`. `today` (default: the
    dataset's status_as_of) is the frozen ledger clock for derived statuses."""
    from core.models import Customer, Invoice, Payment, Supplier
    from core.serializers import ExpenseSerializer
    from core.services.credit_notes import create_credit_note, void_credit_note, void_invoice
    from core.services.invoice_lines import apply_lines, due_date_for, terms_days_for
    from core.services.numbering import provisional_number
    from core.services.payments import record_payment, reverse_payment, update_payment

    ds = dataset or load_dataset()
    user = user or _default_user(company)
    g = GoldenLedger(dataset=ds, company=company, user=user)
    today = today or date.fromisoformat(ds['status_as_of'])

    with frozen_ledger_today(today):
        for c in ds['customers']:
            g.customers[c['id']] = Customer.objects.create(
                company=company, name=c['name'], email=c['email'], payment_terms_default=c['payment_terms'],
                credit_score=80)
        for s in ds['suppliers']:
            g.suppliers[s['id']] = Supplier.objects.create(
                company=company, name=s['name'], vat_number=s['vat_number'], category=s['category'])

        for spec in ds['invoices']:
            issue = date.fromisoformat(spec['issue_date'])
            inv = Invoice(company=company, customer=g.customers[spec['customer']],
                          invoice_number=provisional_number(), issue_date=issue,
                          payment_terms=spec['payment_terms'], terms_days=terms_days_for(spec['payment_terms']),
                          due_date=due_date_for(issue, spec['payment_terms']), status='DRAFT',
                          subtotal=0, total_amount=0, balance=0, notes=spec.get('note', ''))
            apply_lines(inv, [{k: v for k, v in l.items() if k != 'id'} for l in spec['lines']])
            for line_spec, row in zip(spec['lines'], inv.lines.order_by('position', 'id')):
                g.lines[line_spec['id']] = row
            if spec['status'] != 'draft':
                inv.mark_as_sent()
            g.invoices[spec['id']] = inv

        for ev in ds['events']:
            t = ev['type']
            if t == 'payment':
                s = record_payment(company, user, {
                    'invoice': g.invoices[ev['invoice']].pk, 'amount': ev['amount'],
                    'payment_date': ev['payment_date'], 'payment_method': ev.get('method', 'EFT'),
                    'source': ev.get('source', 'MANUAL'), 'external_id': ev.get('external_id', ''),
                    'reference': ev['id'],
                }, allow_overpayment=ev.get('allow_overpayment', False))
                g.payments[ev['id']] = s.instance
            elif t == 'payment_edit':
                changes = {k: ev[k] for k in ('amount', 'payment_date') if k in ev}
                s = update_payment(company, user, Payment.objects.get(pk=g.payments[ev['payment']].pk), changes,
                                   allow_overpayment=ev.get('allow_overpayment', False))
                g.payments[ev['payment']] = s.instance
            elif t == 'payment_delete':
                reverse_payment(company, Payment.objects.get(pk=g.payments[ev['payment']].pk), user)
                g.payments[ev['payment']] = None
            elif t == 'credit_note':
                inv = g.invoices[ev['invoice']]
                if ev.get('full'):
                    cn = create_credit_note(inv, user=user, reason=ev['reason'], full=True,
                                            issue_date=ev['issue_date'])
                else:
                    lines = []
                    for l in ev['lines']:
                        raw = {k: v for k, v in l.items() if k != 'invoice_line'}
                        if l.get('invoice_line'):
                            raw['invoice_line'] = g.lines[l['invoice_line']].pk
                        lines.append(raw)
                    cn = create_credit_note(inv, user=user, reason=ev['reason'], lines=lines,
                                            issue_date=ev['issue_date'])
                g.credit_notes[ev['id']] = cn
            elif t == 'credit_note_void':
                g.credit_notes[ev['credit_note']] = void_credit_note(
                    g.credit_notes[ev['credit_note']], user=user, reason=ev['reason'])
            elif t == 'invoice_void':
                g.invoices[ev['invoice']] = void_invoice(g.invoices[ev['invoice']], user=user, reason=ev['reason'])
            else:
                raise ValueError(f'Unknown golden event type {t!r}')

        request = SimpleNamespace(user=user)
        for e in ds['expenses']:
            body = {'category': e['category'], 'description': e['description'], 'amount': e['amount'],
                    'expense_date': e['expense_date'], 'tax_code': e['tax_code'],
                    'expense_number': f'GOLD-{company.pk}-{e["id"]}'}
            if e.get('vat_amount') not in (None, ''):
                body['vat_amount'] = e['vat_amount']
            if e.get('supplier'):
                body['supplier'] = g.suppliers[e['supplier']].pk
            ser = ExpenseSerializer(data=body, context={'request': request, 'company': company})
            ser.is_valid(raise_exception=True)
            exp = ser.save(company=company, created_by=user)
            if e['status'] == 'APPROVED':
                exp.approve(user)
            elif e['status'] == 'REJECTED':
                exp.reject(user)
            g.expenses[e['id']] = exp

    return g.refresh()
