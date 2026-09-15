"""Tracks trained win-probability model artifacts (per-user and global) and
per-user retrain debounce state — the bookkeeping layer around the .pkl/.json
files core.services.quote_ml.WinProbabilityModel actually reads/writes.

The file on disk is always the source of truth for what predict_proba() uses;
these rows are for observability (what got trained, when, how it scored) and
for the debounce/concurrency guards in core.services.ml_training_queue and
core.services.quote_training. See PLAN doc section 3.2/3.5 for the full design.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class MLModelVersion(models.Model):
    SCOPE_CHOICES = [
        ('user', 'Per-User'),
        ('global', 'Global'),
    ]
    STATUS_CHOICES = [
        ('candidate', 'Candidate'),
        ('active', 'Active'),
        ('rejected', 'Rejected'),
        ('superseded', 'Superseded'),
        ('failed', 'Failed'),
    ]

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, db_index=True)
    # Null iff scope == 'global'.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ml_model_versions',
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='candidate', db_index=True)

    algorithm = models.CharField(max_length=50, blank=True, help_text="e.g. logistic_regression, gradient_boosting, lightgbm")
    feature_version = models.CharField(max_length=20, blank=True)
    # Exact ordered feature-name list this artifact was trained on — the
    # schema-compatibility check at load time compares against this, not
    # against whatever quote_features.py currently produces.
    feature_names = models.JSONField(default=list, blank=True)
    model_version = models.CharField(max_length=40, blank=True, help_text="e.g. user:123:v7, global:v42")

    training_sample_count = models.PositiveIntegerField(default=0)
    accepted_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    training_period_start = models.DateTimeField(null=True, blank=True)
    training_period_end = models.DateTimeField(null=True, blank=True)

    evaluation_metrics = models.JSONField(default=dict, blank=True)
    hyperparameters = models.JSONField(default=dict, blank=True)

    rejection_reason = models.TextField(blank=True)
    trained_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ml_model_versions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['scope', 'user', 'status']),
            models.Index(fields=['scope', 'status', '-created_at']),
        ]
        constraints = [
            # Enforces one active model per user at the DB level. Postgres
            # never treats two NULLs as equal in a unique index, so this does
            # NOT protect the scope='global' (user IS NULL) case — that's
            # guarded at the application level instead, via select_for_update()
            # inside the activation transaction (see quote_training.py).
            models.UniqueConstraint(
                fields=['scope', 'user'],
                condition=Q(status='active', user__isnull=False),
                name='unique_active_model_per_user',
            ),
        ]

    def __str__(self):
        who = f'user={self.user_id}' if self.scope == 'user' else 'global'
        return f'MLModelVersion({who}, {self.status}, n={self.training_sample_count})'


class MLUserRetrainQueue(models.Model):
    """One row per user who has ever had an outcome recorded — pure debounce
    bookkeeping so a burst of outcomes for the same user coalesces into one
    scheduled retrain instead of racing/queuing redundant Celery tasks."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ml_retrain_state',
    )
    is_queued = models.BooleanField(default=False)
    queued_at = models.DateTimeField(null=True, blank=True)
    last_retrain_started_at = models.DateTimeField(null=True, blank=True)
    last_retrain_finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ml_user_retrain_queue'

    def __str__(self):
        return f'MLUserRetrainQueue(user={self.user_id}, queued={self.is_queued})'
