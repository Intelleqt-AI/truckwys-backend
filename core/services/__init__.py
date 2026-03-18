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
