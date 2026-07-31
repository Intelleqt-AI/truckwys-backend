from django.db import models


class PendingSignup(models.Model):
    """A registration that has NOT yet become a real User/Company.

    No free tier: signup requires a successful Paystack payment, so account
    creation (User + Company + Facility + default vehicle types) is deferred
    until CompleteSignupView confirms the charge — see core/views.py. This is
    a real DB row (not the 10-minute cache entry the old flow used) because a
    redirect-based checkout can reasonably take longer than that, and losing
    someone's signup details after they've already paid would be bad.
    """
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    password_hash = models.CharField(max_length=255, help_text='Already hashed (make_password) — never plaintext')
    company_name = models.CharField(max_length=200)

    otp_code = models.CharField(max_length=6, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    email_verified = models.BooleanField(default=False)

    # Set once email is verified and the Paystack checkout is started; a
    # failed/abandoned payment can retry against this same pending row.
    paystack_reference = models.CharField(max_length=200, blank=True)

    # Stamped the moment a "payment could not be completed" email has been
    # sent for the CURRENT paystack_reference — guards against sending it
    # twice when both the charge.failed webhook and the browser's own
    # return-to-app redirect notice the same failed attempt. Explicitly
    # cleared back to None every time a fresh checkout starts (see
    # EmailVerifyView / RetrySignupPaymentView), so a later retry's own
    # failure is still eligible for its own notification.
    payment_failed_notified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pending_signups'

    def __str__(self):
        return f"PendingSignup {self.email} (verified={self.email_verified})"
