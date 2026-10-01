"""The full IntegrationAPIKey secret must only ever appear in the response to
the request that created it. Before the fix, IntegrationAPIKeySerializer
returned the real 'key' value on every list/retrieve/update too — anyone who
opened that page later (or any logged network capture) saw the live secret,
not just the operator at creation time.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Company
from core.models.integration_api_key import IntegrationAPIKey

User = get_user_model()


class APIKeyMaskingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(company_name='Key Co')
        self.user = User.objects.create_user(
            username='op', email='op@example.com', password='Pass-123!',
            role='ADMIN', company=self.company,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_full_key_returned_once_on_create(self):
        resp = self.client.post('/api/v1/integrations/api-keys/', {'name': 'Fleet link'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        key = IntegrationAPIKey.objects.get(pk=resp.json()['id'])
        self.assertEqual(resp.json()['key'], key.key)
        self.assertNotIn('•', resp.json()['key'])

    def test_key_masked_on_list(self):
        key = IntegrationAPIKey.objects.create(name='Fleet link', operator=self.user)
        resp = self.client.get('/api/v1/integrations/api-keys/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        body = resp.json()
        results = body if isinstance(body, list) else body.get('results', body)
        returned = next(r for r in results if r['id'] == key.id)
        self.assertNotEqual(returned['key'], key.key)
        self.assertTrue(returned['key'].endswith(key.key[-4:]))
        self.assertIn('•', returned['key'])

    def test_key_masked_on_retrieve(self):
        key = IntegrationAPIKey.objects.create(name='Fleet link', operator=self.user)
        resp = self.client.get(f'/api/v1/integrations/api-keys/{key.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertNotEqual(resp.json()['key'], key.key)

    def test_key_masked_on_update(self):
        key = IntegrationAPIKey.objects.create(name='Fleet link', operator=self.user)
        resp = self.client.patch(f'/api/v1/integrations/api-keys/{key.id}/', {'name': 'Renamed'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertNotEqual(resp.json()['key'], key.key)
