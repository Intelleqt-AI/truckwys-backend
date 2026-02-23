"""
Credit Bureau Integration Module
Handles credit score lookups from credit bureaus (Dun & Bradstreet, Experian, etc.)
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from django.core.cache import cache
from django.utils import timezone
from core.models import Customer


class CreditBureauService:
    """
    Credit bureau integration service.

    Supports:
    - Manual credit score override
    - Redis caching (24hr TTL)
    - Stubbed D&B API integration
    """

    CACHE_TTL = 60 * 60 * 24  # 24 hours in seconds
    CACHE_PREFIX = 'credit_score'

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize credit bureau service.

        Args:
            api_key: API key for credit bureau (D&B, etc.)
        """
        self.api_key = api_key or ''

    def get_score(self, customer: Customer) -> Dict[str, Any]:
        """
        Get credit score for a customer.

        Process:
        1. Check if customer has manual override score
        2. Check Redis cache
        3. Call credit bureau API (stubbed)

        Args:
            customer: Customer instance

        Returns:
            Credit score data
        """
        # Check manual override first
        if customer.credit_score is not None:
            return {
                'customer_id': customer.id,
                'customer_name': customer.name,
                'credit_score': customer.credit_score,
                'source': customer.credit_score_source,
                'updated_at': customer.credit_score_updated_at,
                'cached': False,
            }

        # Check cache
        cache_key = f"{self.CACHE_PREFIX}:{customer.id}"
        cached_score = cache.get(cache_key)

        if cached_score:
            return {
                **cached_score,
                'cached': True,
            }

        # Perform credit bureau lookup (stubbed)
        score_data = self._lookup_credit_score(customer)

        # Cache the result
        cache.set(cache_key, score_data, self.CACHE_TTL)

        # Update customer record
        customer.update_credit_score(
            score=score_data['credit_score'],
            source=score_data['source']
        )

        return {
            **score_data,
            'cached': False,
        }

    def _lookup_credit_score(self, customer: Customer) -> Dict[str, Any]:
        """
        Lookup credit score from credit bureau API.

        TODO: Implement actual D&B or Experian API integration

        Args:
            customer: Customer instance

        Returns:
            Credit score data
        """
        # Stubbed D&B API call
        # import requests
        #
        # headers = {
        #     'Authorization': f'Bearer {self.api_key}',
        #     'Content-Type': 'application/json',
        # }
        #
        # payload = {
        #     'company_name': customer.company or customer.name,
        #     'registration_number': customer.registration_number,  # If available
        # }
        #
        # response = requests.post(
        #     'https://api.dnb.com/v1/credit/score',
        #     headers=headers,
        #     json=payload,
        # )
        # response.raise_for_status()
        #
        # data = response.json()
        #
        # return {
        #     'customer_id': customer.id,
        #     'customer_name': customer.name,
        #     'credit_score': data['score'],  # 0-100
        #     'source': 'DNB',
        #     'updated_at': timezone.now(),
        #     'details': {
        #         'rating': data.get('rating'),
        #         'risk_class': data.get('risk_class'),
        #         'payment_index': data.get('payment_index'),
        #     }
        # }

        # Stubbed response - generate mock score based on customer data
        mock_score = self._generate_mock_score(customer)

        return {
            'customer_id': customer.id,
            'customer_name': customer.name,
            'credit_score': mock_score,
            'source': 'TRUCKWYS',  # Internal scoring (stub)
            'updated_at': timezone.now(),
            'details': {
                'rating': self._score_to_rating(mock_score),
                'risk_class': self._score_to_risk_class(mock_score),
                'note': 'This is a stubbed score. Connect to D&B for real credit data.',
            }
        }

    def _generate_mock_score(self, customer: Customer) -> int:
        """
        Generate mock credit score based on customer characteristics.
        Real implementation would call credit bureau API.

        Args:
            customer: Customer instance

        Returns:
            Mock credit score (0-100)
        """
        # Base score
        score = 50

        # Relationship length bonus (max +20)
        relationship_months = customer.relationship_months
        score += min(20, relationship_months)

        # Active status bonus
        if customer.is_active:
            score += 10

        # Has credit limit (shows trust)
        if customer.credit_limit and customer.credit_limit > 0:
            score += 10

        # Cap at 100
        return min(100, max(0, score))

    def _score_to_rating(self, score: int) -> str:
        """
        Convert credit score to rating.

        Args:
            score: Credit score (0-100)

        Returns:
            Credit rating (AAA, AA, A, BBB, BB, B, CCC, CC, C, D)
        """
        if score >= 90:
            return 'AAA'
        elif score >= 80:
            return 'AA'
        elif score >= 70:
            return 'A'
        elif score >= 60:
            return 'BBB'
        elif score >= 50:
            return 'BB'
        elif score >= 40:
            return 'B'
        elif score >= 30:
            return 'CCC'
        elif score >= 20:
            return 'CC'
        elif score >= 10:
            return 'C'
        else:
            return 'D'

    def _score_to_risk_class(self, score: int) -> str:
        """
        Convert credit score to risk class.

        Args:
            score: Credit score (0-100)

        Returns:
            Risk class (LOW, MEDIUM, HIGH, VERY_HIGH)
        """
        if score >= 70:
            return 'LOW'
        elif score >= 50:
            return 'MEDIUM'
        elif score >= 30:
            return 'HIGH'
        else:
            return 'VERY_HIGH'

    def invalidate_cache(self, customer: Customer) -> None:
        """
        Invalidate cached credit score for a customer.

        Args:
            customer: Customer instance
        """
        cache_key = f"{self.CACHE_PREFIX}:{customer.id}"
        cache.delete(cache_key)
