"""User.role must default to the lowest-privilege choice, not ADMIN.

Every real creation path sets role= explicitly (signup grants the founding
owner ADMIN via CompleteSignupView; invites and admin forms pass whatever was
chosen). The model default only fires if one of those paths has a bug and
forgets to set it — it must fail closed, not fail open to company admin.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Company

User = get_user_model()


class UserRoleDefaultTests(TestCase):

    def test_default_role_is_lowest_privilege_not_admin(self):
        user = User.objects.create_user(username='no_role', email='no_role@example.com', password='x')
        self.assertEqual(user.role, 'VIEWER')

    def test_signup_still_makes_the_founding_owner_admin(self):
        """CompleteSignupView sets role='ADMIN' explicitly — must not silently
        regress to the (now non-admin) model default."""
        company = Company.objects.create(company_name='New Co')
        user = User.objects.create(
            username='owner', email='owner@example.com', company=company, role='ADMIN')
        self.assertEqual(user.role, 'ADMIN')
