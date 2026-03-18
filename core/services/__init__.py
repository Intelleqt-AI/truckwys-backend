"""Services module for business logic."""

from .risk_engine import RiskEngine, RiskScoreResult
from .invoice_generator import InvoiceGenerator

__all__ = [
    'RiskEngine',
    'RiskScoreResult',
    'InvoiceGenerator',
]

# Quote ML — imported lazily to avoid hard dependency on lightgbm at import time
# Use: from core.services.quote_ml import QuoteMLModel, predict_optimal_margin

# Vehicle cost — RFA VCI benchmark defaults and company overrides
# Use: from core.services.vehicle_cost import get_cost_profile, calculate_total_cpk
