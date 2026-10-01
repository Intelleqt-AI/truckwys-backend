"""/api/v1/integrations/accounting/ (contract: docs/integrations/XERO.md#api)."""
from django.urls import path

from core.accounting import views as v

urlpatterns = [
    path('providers/', v.ProvidersView.as_view(), name='accounting-providers'),
    path('connection/', v.ConnectionView.as_view(), name='accounting-connection'),
    path('connection/select-org/', v.SelectOrgView.as_view(), name='accounting-select-org'),
    path('connection/disconnect/', v.DisconnectView.as_view(), name='accounting-disconnect'),
    path('connection/mapping/', v.MappingView.as_view(), name='accounting-mapping'),
    path('connection/refresh-options/', v.RefreshOptionsView.as_view(), name='accounting-refresh-options'),
    path('connection/contacts/', v.ContactsView.as_view(), name='accounting-contacts'),
    path('connection/contacts/run-matching/', v.ContactsRunMatchingView.as_view(), name='accounting-contacts-match'),
    path('connection/contacts/search/', v.ContactSearchView.as_view(), name='accounting-contacts-search'),
    path('connection/contacts/<int:link_id>/confirm/', v.ContactConfirmView.as_view(), name='accounting-contact-confirm'),
    path('connection/backfill/', v.BackfillView.as_view(), name='accounting-backfill'),
    path('connection/sync/', v.SyncStatusView.as_view(), name='accounting-sync'),
    path('connection/sync/<int:link_id>/retry/', v.RetryLinkView.as_view(), name='accounting-sync-retry'),
    path('connection/sync-now/', v.SyncNowView.as_view(), name='accounting-sync-now'),
    path('connection/reconciliation/', v.ReconciliationView.as_view(), name='accounting-reconciliation'),
    path('connection/reconciliation/run/', v.ReconciliationRunView.as_view(), name='accounting-reconciliation-run'),
    path('<slug:slug>/connect/', v.ConnectView.as_view(), name='accounting-connect'),
    path('<slug:slug>/start/', v.StartView.as_view(), name='accounting-start'),
    path('<slug:slug>/callback/', v.OAuthCallbackView.as_view(), name='accounting-callback'),
]
