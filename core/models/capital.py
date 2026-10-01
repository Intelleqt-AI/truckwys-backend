"""Fast Pay (Capital) book models: funder, policy, limits, scores, decisions, ledger.

Design: docs/capital-risk/03-design.md (approved 2026-10-01); what is built and
the defaults chosen: docs/capital/IMPLEMENTATION.md.

Three rules hold for everything in this module:

* **Append-only where money or decisions are recorded.** ``CapitalLedgerEntry``,
  ``InvoiceAssessment``, ``CapitalScore`` and ``CapitalLimit`` rows are never
  updated or deleted. ``save()`` on an existing row and ``delete()`` raise, the
  queryset ``update()``/``delete()`` raise, and on Postgres a trigger refuses
  UPDATE/DELETE on the ledger table (migration 0139). A correction is a new row.
* **Balances are derived.** Reserved/outstanding exposure per funder, debtor,
  transporter, pair and sector are sums over the ledger (``core.capital.ledger``).
  ``Facility.outstanding``/``reserved`` remain as a cache the facility ledger
  writes in the same transaction; reconciliation proves the two agree.
* **The funder decides credit.** TruckWys scores, checks limits and records
  every decision. In Mode A (the default) a funder approver approves each
  advance; LLM text never decides anything.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

ZERO = Decimal('0.00')


class ImmutableRowError(Exception):
    """An append-only capital row was about to be changed or deleted."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRowError(f'{self.model.__name__} rows are append-only; write a new row instead')

    def delete(self):
        raise ImmutableRowError(f'{self.model.__name__} rows are append-only and cannot be deleted')


class AppendOnlyModel(models.Model):
    """Base for rows that are written once.

    ``_allow_update_fields`` lists the only fields a later save may touch (for
    example the approval stamp on a policy version). Everything else is frozen.
    """
    _allow_update_fields: tuple[str, ...] = ()

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            fields = kwargs.get('update_fields')
            if not fields or not set(fields) <= set(self._allow_update_fields):
                raise ImmutableRowError(
                    f'{type(self).__name__} #{self.pk} is append-only; write a new row instead')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRowError(f'{type(self).__name__} rows are append-only and cannot be deleted')


# ---------------------------------------------------------------------------
# Funder, membership, policy, limits
# ---------------------------------------------------------------------------

class Funder(models.Model):
    """The finance provider whose pot funds advances.

    TruckWys is not a lender: the funder owns the credit decision (Mode A) and
    the money. ``SANDBOX`` is the placeholder created by migration 0139 for the
    pre-existing per-company facilities; it moves no real money and keeps the
    old behaviour (staff may approve) so nothing changes before launch.
    """
    STATUS_SANDBOX = 'SANDBOX'
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_PAUSED = 'PAUSED'
    STATUS_CLOSED = 'CLOSED'
    STATUS_CHOICES = [
        (STATUS_SANDBOX, 'Sandbox (not signed, no real money)'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_PAUSED, 'Paused (no new advances)'),
        (STATUS_CLOSED, 'Closed'),
    ]
    MODE_A = 'A'
    MODE_B = 'B'
    MODE_CHOICES = [
        (MODE_A, 'A: funder approves every advance'),
        (MODE_B, 'B: auto-approve inside a funder-signed envelope'),
    ]
    RECOURSE_CHOICES = [
        ('TBD', 'To be decided by the owner and funder'),
        ('RECOURSE', 'With recourse'),
        ('NON_RECOURSE', 'Without recourse (debtor insolvency)'),
    ]

    name = models.CharField(max_length=200, help_text='Internal name. Never shown to transporters.')
    code = models.SlugField(max_length=50, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SANDBOX, db_index=True)
    pot_limit = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO,
                                    validators=[MinValueValidator(ZERO)],
                                    help_text='Facility size (the pot) in ZAR')
    cost_of_funds_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('13.500'),
                                            help_text='Annual cost of funds, % (assumption until signed)')
    operating_mode = models.CharField(max_length=1, choices=MODE_CHOICES, default=MODE_A)
    # Mode B switch. Even with it on, auto-approval needs settings.CAPITAL_AUTO_APPROVE_ENABLED too.
    auto_approve_enabled = models.BooleanField(default=False)
    # Written delegation letting the TruckWys capital desk approve in Mode A.
    staff_may_approve = models.BooleanField(default=False)
    recourse = models.CharField(max_length=15, choices=RECOURSE_CHOICES, default='TBD')
    first_loss_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    insurance_cover = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'capital_funders'
        ordering = ['id']
        constraints = [
            models.CheckConstraint(condition=Q(pot_limit__gte=0), name='funder_pot_non_negative'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_status_display()})'

    @property
    def accepts_new_advances(self) -> bool:
        return self.status in (self.STATUS_SANDBOX, self.STATUS_ACTIVE)


class FunderMembership(models.Model):
    """A person acting for a funder in the dashboard (capital desk views, approvals)."""
    ROLE_VIEWER = 'VIEWER'
    ROLE_APPROVER = 'APPROVER'
    ROLE_CHOICES = [
        (ROLE_VIEWER, 'Viewer (book, ledger, data room)'),
        (ROLE_APPROVER, 'Approver (also approves or declines advances and policy)'),
    ]
    funder = models.ForeignKey(Funder, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='funder_memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'capital_funder_memberships'
        constraints = [
            models.UniqueConstraint(fields=['funder', 'user'], name='uniq_funder_membership'),
        ]

    def __str__(self):
        return f'{self.user} @ {self.funder} ({self.role})'


class CreditPolicy(AppendOnlyModel):
    """A versioned set of policy parameters (caps, advance grid, eligibility toggles, pricing).

    ``params`` holds only overrides; ``core.capital.policy.DEFAULT_POLICY`` fills
    the rest, so a new parameter never breaks an old version. The version in
    force is the newest one the funder approved (maker/checker); a SANDBOX
    funder uses its newest version, approved or not.
    """
    _allow_update_fields = ('approved_by', 'approved_by_funder_at')

    funder = models.ForeignKey(Funder, on_delete=models.PROTECT, related_name='policies')
    version = models.PositiveIntegerField()
    params = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')
    approved_by_funder_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'capital_credit_policies'
        ordering = ['funder_id', '-version']
        constraints = [
            models.UniqueConstraint(fields=['funder', 'version'], name='uniq_policy_version'),
        ]

    def __str__(self):
        return f'Policy v{self.version} ({self.funder.code})'


class CapitalLimit(AppendOnlyModel):
    """A limit or hold set by a person, overriding the policy default for one scope.

    Append-only: the newest row for a (funder, scope, subject) is in force; the
    rows before it are the change history (who, why, when). ``amount=None``
    means "back to the policy default".
    """
    SCOPE_DEBTOR = 'DEBTOR'
    SCOPE_TRANSPORTER = 'TRANSPORTER'
    SCOPE_PAIR = 'PAIR'
    SCOPE_SECTOR = 'SECTOR'
    SCOPE_CHOICES = [
        (SCOPE_DEBTOR, 'Debtor'),
        (SCOPE_TRANSPORTER, 'Transporter'),
        (SCOPE_PAIR, 'Transporter-debtor pair'),
        (SCOPE_SECTOR, 'Sector'),
    ]
    funder = models.ForeignKey(Funder, on_delete=models.PROTECT, related_name='limits')
    scope = models.CharField(max_length=12, choices=SCOPE_CHOICES)
    debtor = models.ForeignKey('DebtorIdentity', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='capital_limits')
    company = models.ForeignKey('Company', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='capital_limits')
    sector = models.CharField(max_length=20, blank=True, default='')
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(ZERO)])
    hold = models.BooleanField(default=False, help_text='No new exposure in this scope')
    reason = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'capital_limits'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['funder', 'scope', 'debtor', 'company', 'sector', '-created_at']),
        ]


class CapitalApplication(models.Model):
    """A transporter's Fast Pay application, KYC checklist and consents (server-side).

    Replaces the browser-only "applied" flag. The funder performs FICA due
    diligence; this records what TruckWys collected as its agent.
    """
    STATUS_CHOICES = [
        ('NOT_STARTED', 'Not started'),
        ('SUBMITTED', 'Submitted'),
        ('APPROVED', 'Approved by the funder'),
        ('REJECTED', 'Not approved'),
    ]
    company = models.OneToOneField('Company', on_delete=models.CASCADE, related_name='capital_application')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='NOT_STARTED')
    juristic_person = models.BooleanField(default=False, help_text='A company or close corporation, not a sole proprietor')
    declared_annual_turnover = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    turnover_evidenced = models.BooleanField(default=False)
    git_insurer = models.CharField(max_length=200, blank=True, default='')
    git_insurance_expiry = models.DateField(null=True, blank=True)
    tax_compliance_verified = models.BooleanField(default=False)
    bank_account_verified = models.BooleanField(default=False)
    # [{purpose, text_version, granted_at, granted_by}], purposes in core.capital.policy.REQUIRED_CONSENTS
    consents = models.JSONField(default=list, blank=True)
    on_hold = models.BooleanField(default=False)
    hold_reason = models.TextField(blank=True, default='')
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'capital_applications'

    def consent_purposes(self) -> set[str]:
        return {c.get('purpose') for c in (self.consents or []) if c.get('granted_at') and not c.get('revoked_at')}


# ---------------------------------------------------------------------------
# Scores, decisions, external checks
# ---------------------------------------------------------------------------

GRADE_CHOICES = [(g, g) for g in 'ABCDE']


class CapitalScore(AppendOnlyModel):
    """One debtor or transporter score (history kept; newest valid one is current).

    Every score carries its grade, PD, plain-language reason codes, the inputs
    it used (snapshot + hash) and the model version, so any decision that used
    it can be explained and reproduced.
    """
    KIND_DEBTOR = 'DEBTOR'
    KIND_TRANSPORTER = 'TRANSPORTER'
    KIND_CHOICES = [(KIND_DEBTOR, 'Debtor'), (KIND_TRANSPORTER, 'Transporter')]

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, db_index=True)
    debtor = models.ForeignKey('DebtorIdentity', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='capital_scores')
    company = models.ForeignKey('Company', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='capital_scores')
    grade = models.CharField(max_length=1, choices=GRADE_CHOICES)
    points = models.IntegerField(help_text='Scorecard points 0-100 (higher = safer)')
    pd_12m = models.DecimalField(max_digits=7, decimal_places=5, help_text='12-month probability of default (0-1)')
    expected_dtp_days = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    hard_stop = models.BooleanField(default=False)
    cold_start = models.BooleanField(default=False)
    reason_codes = models.JSONField(default=list, blank=True)
    inputs = models.JSONField(default=dict, blank=True)
    inputs_hash = models.CharField(max_length=64)
    model_version = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    valid_until = models.DateTimeField()

    class Meta:
        db_table = 'capital_scores'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['kind', 'debtor', '-created_at']),
            models.Index(fields=['kind', 'company', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(kind='DEBTOR', debtor__isnull=False, company__isnull=True)
                           | Q(kind='TRANSPORTER', company__isnull=False, debtor__isnull=True)),
                name='capital_score_subject_matches_kind'),
        ]


class InvoiceAssessment(AppendOnlyModel):
    """The immutable decision record for one Fast Pay evaluation of one invoice.

    Every number a transporter, the capital desk or the funder sees for an
    invoice comes from one of these rows; no client recomputes a fee.
    """
    DECISION_FUND = 'FUND'
    DECISION_PART_FUND = 'PART_FUND'
    DECISION_QUEUE = 'QUEUE'
    DECISION_REFER = 'REFER'
    DECISION_DECLINE = 'DECLINE'
    DECISION_CHOICES = [
        (DECISION_FUND, 'Fund in full'),
        (DECISION_PART_FUND, 'Fund part now'),
        (DECISION_QUEUE, 'Queue until capacity frees'),
        (DECISION_REFER, 'Refer to a person'),
        (DECISION_DECLINE, 'Decline'),
    ]
    PURPOSE_CHOICES = [
        ('OFFER', 'Offer preview (no money reserved)'),
        ('REQUEST', 'Transporter request'),
        ('QUEUE', 'Queue re-evaluation'),
        ('LENDER', 'Funder API request'),
    ]

    invoice = models.ForeignKey('Invoice', on_delete=models.PROTECT, related_name='capital_assessments')
    company = models.ForeignKey('Company', on_delete=models.PROTECT, related_name='capital_assessments')
    funder = models.ForeignKey(Funder, on_delete=models.PROTECT, null=True, blank=True,
                               related_name='assessments')
    debtor = models.ForeignKey('DebtorIdentity', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='capital_assessments')
    debtor_score = models.ForeignKey(CapitalScore, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='+')
    transporter_score = models.ForeignKey(CapitalScore, on_delete=models.PROTECT, null=True, blank=True,
                                          related_name='+')
    purpose = models.CharField(max_length=8, choices=PURPOSE_CHOICES, default='OFFER')
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES, db_index=True)
    eligible = models.BooleanField(default=False)
    eligibility = models.JSONField(default=list, blank=True, help_text='[{rule, passed, code, text}]')
    checks = models.JSONField(default=dict, blank=True, help_text='Duplicate / fraud / verification checks')
    verification_tier = models.CharField(max_length=2, default='V0')
    fraud_score = models.DecimalField(max_digits=4, decimal_places=3, default=ZERO)
    invoice_total = models.DecimalField(max_digits=14, decimal_places=2)
    invoice_balance = models.DecimalField(max_digits=14, decimal_places=2)
    requested_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    advance_rate_pct = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    eligible_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO,
                                          help_text='advance rate x balance, before headroom')
    fundable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO,
                                          help_text='What can be advanced now, after every limit')
    queued_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    binding_limit = models.CharField(max_length=30, blank=True, default='')
    headroom = models.JSONField(default=dict, blank=True)
    expected_dtp_days = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    expected_payment_date = models.DateField(null=True, blank=True)
    pd_horizon = models.DecimalField(max_digits=7, decimal_places=5, default=ZERO)
    el_pct = models.DecimalField(max_digits=7, decimal_places=4, default=ZERO,
                                 help_text='Expected loss as % of the advance')
    invoice_grade = models.CharField(max_length=3, blank=True, default='')
    fee_pct = models.DecimalField(max_digits=6, decimal_places=3, default=ZERO)
    fee_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    fee_vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    fee_breakdown = models.JSONField(default=dict, blank=True)
    net_payout = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    holdback_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    reason_codes = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True, default='')
    explanation_source = models.CharField(max_length=10, default='TEMPLATE')
    policy_version = models.PositiveIntegerField(default=0)
    model_version = models.CharField(max_length=40, default='')
    content_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    actor_label = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'capital_invoice_assessments'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['invoice', '-created_at']),
            models.Index(fields=['funder', 'decision', '-created_at']),
        ]


class ExternalCheck(models.Model):
    """Raw snapshot of a CIPC / bureau lookup (fake or live), kept for audit and cost."""
    PROVIDER_CHOICES = [('CIPC', 'CIPC'), ('BUREAU', 'Credit bureau'), ('LLM', 'LLM document extraction')]
    provider = models.CharField(max_length=10, choices=PROVIDER_CHOICES, db_index=True)
    adapter = models.CharField(max_length=30, help_text='fake | null | <vendor>')
    debtor = models.ForeignKey('DebtorIdentity', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='external_checks')
    company = models.ForeignKey('Company', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='capital_external_checks')
    available = models.BooleanField(default=False)
    status = models.CharField(max_length=30, blank=True, default='')
    score = models.IntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    is_fake = models.BooleanField(default=False)
    cost_zar = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO)
    fetched_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'capital_external_checks'
        ordering = ['-fetched_at']


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class CapitalLedgerEntry(AppendOnlyModel):
    """One movement in the Fast Pay book. Every rand is one row; nothing is edited.

    ``reserved_delta`` / ``outstanding_delta`` carry the effect on exposure, so
    any balance is a SUM over a filter. ``amount`` is the gross rand figure of
    the event (always >= 0); informational events (FEE, RELEASE_HOLDBACK,
    DILUTION) move no exposure and have zero deltas.

    =================  ==============================  ===================
    entry_type         meaning                          reserved / outstanding
    =================  ==============================  ===================
    OPENING            balance carried in at 0139       +r / +o
    RESERVE            capacity held for an advance     +a / 0
    CANCEL_RESERVE     held capacity let go             -a / 0
    DISBURSE           money paid out                   -held / +a
    FEE                fee earned on an advance         0 / 0
    COLLECTION         debtor paid; advance recovered   0 / -a
    RELEASE_HOLDBACK   holdback owed to transporter     0 / 0
    DILUTION           credit note on a funded invoice  0 / 0
    BUYBACK            transporter repurchased          0 / -a
    WRITE_OFF          loss recognised                  0 / -a
    ADJUSTMENT         reconciliation correction        +-r / +-o
    =================  ==============================  ===================
    """
    OPENING = 'OPENING'
    RESERVE = 'RESERVE'
    CANCEL_RESERVE = 'CANCEL_RESERVE'
    DISBURSE = 'DISBURSE'
    FEE = 'FEE'
    COLLECTION = 'COLLECTION'
    RELEASE_HOLDBACK = 'RELEASE_HOLDBACK'
    DILUTION = 'DILUTION'
    BUYBACK = 'BUYBACK'
    WRITE_OFF = 'WRITE_OFF'
    ADJUSTMENT = 'ADJUSTMENT'
    TYPE_CHOICES = [(t, t.replace('_', ' ').title()) for t in (
        OPENING, RESERVE, CANCEL_RESERVE, DISBURSE, FEE, COLLECTION, RELEASE_HOLDBACK,
        DILUTION, BUYBACK, WRITE_OFF, ADJUSTMENT)]

    id = models.BigAutoField(primary_key=True)
    entry_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)
    funder = models.ForeignKey(Funder, on_delete=models.PROTECT, null=True, blank=True,
                               related_name='ledger_entries')
    facility = models.ForeignKey('Facility', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='ledger_entries')
    company = models.ForeignKey('Company', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='capital_ledger_entries')
    debtor = models.ForeignKey('DebtorIdentity', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='capital_ledger_entries')
    invoice = models.ForeignKey('Invoice', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='capital_ledger_entries')
    advance = models.ForeignKey('AdvanceRequest', on_delete=models.PROTECT, null=True, blank=True,
                                related_name='ledger_entries')
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    reserved_delta = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    outstanding_delta = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                              related_name='+')
    actor_label = models.CharField(max_length=120, blank=True, default='')
    reference = models.CharField(max_length=200, blank=True, default='')
    memo = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'capital_ledger_entries'
        ordering = ['id']
        verbose_name_plural = 'Capital ledger entries'
        indexes = [
            models.Index(fields=['funder', 'debtor']),
            models.Index(fields=['funder', 'company']),
            models.Index(fields=['advance']),
            models.Index(fields=['facility']),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name='capital_ledger_amount_non_negative'),
        ]


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

class CapitalAlert(models.Model):
    """An early-warning or limit alert for the capital desk and funder.

    ``dedupe_key`` + an open-alert partial unique index stop a nightly job from
    raising the same alert twice; resolving it lets it fire again later.
    """
    SEVERITY_CHOICES = [('INFO', 'Info'), ('AMBER', 'Amber'), ('RED', 'Red')]
    KIND_CHOICES = [
        ('LIMIT', 'Limit near or at cap'),
        ('CONCENTRATION', 'Concentration'),
        ('RISK_INDEX', 'Book risk index'),
        ('OVERDUE', 'Funded invoice overdue'),
        ('DEBTOR_WARNING', 'Debtor early warning'),
        ('TRANSPORTER_WARNING', 'Transporter early warning'),
        ('RECONCILIATION', 'Ledger reconciliation'),
        ('QUEUE', 'Queue'),
        ('SETTLEMENT', 'Paid invoice awaiting settlement'),
    ]
    funder = models.ForeignKey(Funder, on_delete=models.CASCADE, null=True, blank=True, related_name='alerts')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    severity = models.CharField(max_length=5, choices=SEVERITY_CHOICES, default='AMBER')
    dedupe_key = models.CharField(max_length=200)
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')

    class Meta:
        db_table = 'capital_alerts'
        ordering = ['-opened_at']
        constraints = [
            models.UniqueConstraint(fields=['dedupe_key'], condition=Q(resolved_at__isnull=True),
                                    name='uniq_open_capital_alert'),
        ]


class BookSnapshot(models.Model):
    """Daily book metrics (risk index, concentration, stress) for trend and the data room."""
    funder = models.ForeignKey(Funder, on_delete=models.CASCADE, related_name='snapshots')
    as_of = models.DateField()
    metrics = models.JSONField(default=dict)
    risk_index = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    band = models.CharField(max_length=5, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'capital_book_snapshots'
        ordering = ['-as_of']
        constraints = [
            models.UniqueConstraint(fields=['funder', 'as_of'], name='uniq_book_snapshot_per_day'),
        ]


class DataRoomExport(models.Model):
    """A monthly funder data-room pack (loan tape, ledger, exposures, summary)."""
    funder = models.ForeignKey(Funder, on_delete=models.CASCADE, related_name='data_room_exports')
    period = models.CharField(max_length=7, help_text='YYYY-MM')
    files = models.JSONField(default=dict, help_text='{name: storage path}')
    summary = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'capital_data_room_exports'
        ordering = ['-period', '-created_at']


class CapitalAIUsage(models.Model):
    """One LLM call made for Fast Pay (document extraction or explanation): cost cap and audit."""
    PURPOSE_CHOICES = [('EXTRACT', 'Document field extraction'), ('EXPLAIN', 'Decision explanation')]
    purpose = models.CharField(max_length=8, choices=PURPOSE_CHOICES)
    model = models.CharField(max_length=60, blank=True, default='')
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=5, default=Decimal('0'))
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'capital_ai_usage'
        ordering = ['-created_at']
