"""
TruckWys Integrations Package
Handles third-party integrations (Xero, Fleet software, Credit bureaus, etc.)
"""
from .xero import XeroClient
from .fleet import FleetIntegrationBase, ManualFleetIntegration
from .cartrack import CartrackClient, CartrackIntegration
from .credit_bureau import CreditBureauService

__all__ = [
    'XeroClient',
    'FleetIntegrationBase',
    'ManualFleetIntegration',
    'CartrackClient',
    'CartrackIntegration',
    'CreditBureauService',
]
