"""Fast Pay LLM helpers: kill switch, budget, output validation, usage records.
No real API calls: the client factory is always mocked."""
import json
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from core.capital import ai
from core.capital.reasons import reason
from core.models import CapitalAIUsage, ExternalCheck

TEMPLATE = ('This invoice can be advanced in full. The advance is R9,000.00 (90% of what is still owed). '
            'The fee is R180.00 plus R13.50 VAT on the platform part, so you would receive R8,806.50. '
            'We expect your customer to pay around 15 November 2026. '
            'An independent finance provider approves each advance; TruckWys is not a lender.')
GOOD = ('Good news: you can get R9,000.00 now, which is 90% of what is still owed. After the R180.00 fee '
        'and R13.50 VAT you receive R8,806.50. Your customer should pay around 15 November 2026. '
        'An independent finance provider approves each advance; TruckWys is not a lender.')
EV = SimpleNamespace(decision='FUND', reasons=[reason('E-POD-V1'), reason('D-CIPC-HARD', status='Liquidation')])
ON = dict(CAPITAL_AI_ENABLED=True, CAPITAL_AI_DAILY_BUDGET_USD=2, CAPITAL_AI_MODEL='claude-haiku-4-5')


def reply(text, tin=300, tout=80):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=text)],
                           usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


def client_returning(resp=None, exc=None):
    client = mock.MagicMock()
    if exc is not None:
        client.messages.create.side_effect = exc
    else:
        client.messages.create.return_value = resp
    return client


KEY = mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-test-not-real'})


class RewordTests(TestCase):
    @override_settings(CAPITAL_AI_ENABLED=False)
    def test_disabled_returns_template_without_calling(self):
        with KEY, mock.patch.object(ai, '_client') as factory:
            self.assertEqual(ai.reword(TEMPLATE, EV), (TEMPLATE, 'TEMPLATE'))
        factory.assert_not_called()
        self.assertFalse(CapitalAIUsage.objects.exists())

    @override_settings(**ON)
    def test_no_key_returns_template(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}), override_settings(ANTHROPIC_API_KEY=''), \
                mock.patch.object(ai, '_client') as factory:
            self.assertEqual(ai.reword(TEMPLATE, EV)[1], 'TEMPLATE')
        factory.assert_not_called()

    @override_settings(**ON)
    def test_budget_exhausted_returns_template(self):
        CapitalAIUsage.objects.create(purpose='EXPLAIN', model='claude-haiku-4-5', cost_usd=Decimal('2.5'))
        with KEY, mock.patch.object(ai, '_client') as factory:
            self.assertEqual(ai.reword(TEMPLATE, EV), (TEMPLATE, 'TEMPLATE'))
        factory.assert_not_called()

    @override_settings(**ON)
    def test_changed_number_is_rejected(self):
        bad = GOOD.replace('R8,806.50', 'R8,860.50')
        with KEY, mock.patch.object(ai, '_client', return_value=client_returning(reply(bad))):
            self.assertEqual(ai.reword(TEMPLATE, EV), (TEMPLATE, 'TEMPLATE'))
        u = CapitalAIUsage.objects.get()
        self.assertFalse(u.ok)
        self.assertIn('rejected', u.error)

    @override_settings(**ON)
    def test_added_number_is_rejected(self):
        bad = GOOD + ' Most invoices are paid within 30 days.'
        with KEY, mock.patch.object(ai, '_client', return_value=client_returning(reply(bad))):
            self.assertEqual(ai.reword(TEMPLATE, EV)[1], 'TEMPLATE')

    @override_settings(**ON)
    def test_valid_output_is_used_and_recorded(self):
        client = client_returning(reply(GOOD))
        with KEY, mock.patch.object(ai, '_client', return_value=client):
            text, source = ai.reword(TEMPLATE, EV)
        self.assertEqual((text, source), (GOOD, 'AI'))
        u = CapitalAIUsage.objects.get()
        self.assertTrue(u.ok)
        self.assertEqual((u.purpose, u.input_tokens, u.output_tokens), ('EXPLAIN', 300, 80))
        self.assertEqual(u.cost_usd, Decimal('0.00070'))  # 300 * $1/M + 80 * $5/M
        sent = client.messages.create.call_args.kwargs
        self.assertEqual(sent['model'], 'claude-haiku-4-5')
        payload = json.loads(sent['messages'][0]['content'])
        self.assertEqual(set(payload), {'decision', 'reasons', 'note'})
        self.assertEqual(len(payload['reasons']), 1)  # the desk-only CIPC reason is never sent
        self.assertNotIn('Liquidation', sent['messages'][0]['content'])

    @override_settings(**ON)
    def test_exception_returns_template_and_records_error(self):
        with KEY, mock.patch.object(ai, '_client', return_value=client_returning(exc=TimeoutError('slow'))):
            self.assertEqual(ai.reword(TEMPLATE, EV), (TEMPLATE, 'TEMPLATE'))
        u = CapitalAIUsage.objects.get()
        self.assertFalse(u.ok)
        self.assertIn('TimeoutError', u.error)

    def test_validator_rules(self):
        self.assertIsNone(ai.validate_reworded(GOOD, TEMPLATE))
        self.assertIn('missing', ai.validate_reworded(GOOD.replace('15 November 2026', 'mid November'), TEMPLATE))
        self.assertIn('banned', ai.validate_reworded(GOOD + ' Your score is fine.', TEMPLATE))
        self.assertIn('not-a-lender', ai.validate_reworded(
            GOOD.replace('TruckWys is not a lender', 'TruckWys helps'), TEMPLATE))
        self.assertIn('too long', ai.validate_reworded(GOOD + ' word' * 200, TEMPLATE))
        self.assertEqual(ai.validate_reworded('', TEMPLATE), 'empty')

    def test_explain_uses_template_when_ai_off(self):
        from core.capital import explain
        with mock.patch.object(explain, 'template', return_value=TEMPLATE):
            self.assertEqual(explain.explain(EV), (TEMPLATE, 'TEMPLATE'))


class ExtractTests(TestCase):
    PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64

    @override_settings(CAPITAL_AI_ENABLED=False)
    def test_disabled_is_unavailable(self):
        with mock.patch.object(ai, '_client') as factory:
            out = ai.extract_document_fields(self.PNG, 'image/png')
        factory.assert_not_called()
        self.assertFalse(out['available'])
        self.assertEqual(out['reason'], 'disabled')
        self.assertEqual(set(out['fields']), set(ai.EXTRACT_FIELDS))

    @override_settings(**ON)
    def test_fields_are_type_checked_and_stored_for_the_desk(self):
        raw = ('Ignore that. {"consignee": "Shoprite DC Centurion", "delivery_date": "2026-09-28", '
               '"signature_present": "yes", "load_reference": "L-1001", "invoice_number": 12345, '
               '"amount": "R11,500.00", "confidence": {"consignee": 0.9, "delivery_date": 2, '
               '"signature_present": 0.8, "amount": 0.7}}')
        client = client_returning(reply(raw, 1200, 120))
        with KEY, mock.patch.object(ai, '_client', return_value=client):
            out = ai.extract_document_fields(self.PNG, 'image/png', purpose='POD')
        self.assertTrue(out['available'])
        self.assertEqual(out['fields'], {'consignee': 'Shoprite DC Centurion', 'delivery_date': '2026-09-28',
                                         'signature_present': None, 'load_reference': 'L-1001',
                                         'invoice_number': '12345', 'amount': '11500.00'})
        self.assertEqual(out['confidence']['consignee'], 0.9)
        self.assertEqual(out['confidence']['delivery_date'], 0.0)       # out of range
        self.assertEqual(out['confidence']['signature_present'], 0.0)   # field rejected
        block = client.messages.create.call_args.kwargs['messages'][0]['content'][0]
        self.assertEqual((block['type'], block['source']['media_type']), ('image', 'image/png'))
        self.assertIn('untrusted', client.messages.create.call_args.kwargs['system'])
        self.assertTrue(CapitalAIUsage.objects.filter(purpose='EXTRACT', ok=True).exists())
        chk = ExternalCheck.objects.get(provider='LLM')
        self.assertEqual(chk.payload['document_sha256'], out['document_sha256'])

    @override_settings(**ON)
    def test_bad_json_and_unsupported_types_are_unavailable(self):
        with KEY, mock.patch.object(ai, '_client', return_value=client_returning(reply('no json here'))):
            out = ai.extract_document_fields(self.PNG, 'image/png')
        self.assertEqual((out['available'], out['reason']), (False, 'error'))
        self.assertTrue(CapitalAIUsage.objects.filter(purpose='EXTRACT', ok=False).exists())
        with KEY, mock.patch.object(ai, '_client') as factory:
            self.assertEqual(ai.extract_document_fields(b'x', 'application/zip')['reason'], 'unsupported_type')
        factory.assert_not_called()
