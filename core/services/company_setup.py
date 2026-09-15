"""Helpers run when a new company/tenant is created.

Vehicle type defaults used to be recreated as a separate copy for every new
company here — retired in favour of a single shared (company=None) pool
managed centrally from the platform admin dashboard (core/views_admin.py's
AdminVehicleTypesView), so a rate/description fix applies to every company at
once instead of needing a migration to backfill each tenant's own copy (see
core/migrations/0109_consolidate_vehicle_type_defaults.py for the one-off
merge of pre-existing per-company copies into that shared pool). A company
can still add its own custom types via Settings > Vehicle Types on top of the
shared defaults.
"""
