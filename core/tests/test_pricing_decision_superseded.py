"""A quote whose price changes without a new pricing analysis no longer
matches its stored pricing decision: the decision is marked superseded, its
chance and margin stop being shown, and a fresh decision clears the mark
(core.services.pricing_decisions.supersede_if_price_changed)."""
from decimal import Decimal

from core.models import Quote, QuotePricingDecision
from core.tests.test_pricing_analysis import _Base, _DecisionHelpers


class SupersededDecisionTests(_DecisionHelpers, _Base):
    def create(self, **decision):
        resp = self.api.post('/api/v1/quotes/', self.quote_payload(pricing_decision=self.decision(**decision)),
                             format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()['id']

    def detail(self, qid):
        return self.api.get(f'/api/v1/quotes/{qid}/').json()

    def row(self, qid):
        return QuotePricingDecision.objects.get(quote_id=qid)

    def test_new_decision_is_current(self):
        qid = self.create()
        self.assertIsNone(self.row(qid).superseded_at)
        self.assertFalse(self.detail(qid)['pricing_decision']['stale'])
        self.assertIsNotNone(self.detail(qid)['pricing_margin_pct'])

    def test_price_changed_without_analysis_supersedes(self):
        qid = self.create()
        Quote.objects.filter(id=qid).update(win_probability=Decimal('61'))   # a chance saved at the old price
        resp = self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '27000'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNotNone(self.row(qid).superseded_at)
        detail = self.detail(qid)
        self.assertTrue(detail['pricing_decision']['stale'])
        self.assertIsNone(detail['pricing_margin_pct'])
        self.assertIsNone(detail['agreed_margin'])
        self.assertIsNone(Quote.objects.get(id=qid).win_probability)
        self.assertEqual(Decimal(detail['total_amount']), Decimal('27000'))   # the quote's own price is untouched

    def test_new_decision_with_the_price_clears_the_mark(self):
        qid = self.create()
        self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '27000'}, format='json')
        self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '27000',
                                                  'pricing_decision': self.decision(final_price=27000)}, format='json')
        self.assertIsNone(self.row(qid).superseded_at)
        self.assertFalse(self.detail(qid)['pricing_decision']['stale'])

    def test_same_price_or_other_fields_keep_it_current(self):
        qid = self.create()
        self.api.patch(f'/api/v1/quotes/{qid}/', {'notes': 'Call before loading'}, format='json')
        self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '22700.00'}, format='json')
        self.api.patch(f'/api/v1/quotes/{qid}/', {'total_amount': '22700.40'}, format='json')   # within 50 cents
        self.assertIsNone(self.row(qid).superseded_at)

    def test_decision_sent_for_another_price_is_stored_superseded(self):
        # A client that sends figures from an earlier price (a save during a refresh).
        qid = self.create(final_price=25000)
        self.assertIsNotNone(self.row(qid).superseded_at)
        self.assertTrue(self.detail(qid)['pricing_decision']['stale'])
        self.assertIsNone(Quote.objects.get(id=qid).win_probability)

    def test_model_saves_outside_the_api(self):
        qid = self.create()
        quote = Quote.objects.get(id=qid)
        quote.status = 'SENT'
        quote.save(update_fields=['status'])          # price untouched: no check
        self.assertIsNone(self.row(qid).superseded_at)
        quote.total_amount = Decimal('19999')
        quote.save()                                  # e.g. the admin
        self.assertIsNotNone(self.row(qid).superseded_at)
