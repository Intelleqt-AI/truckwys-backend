# Wave 0 backend changes: integration and merge order

Five independently reviewed changes, combined and tested together on this branch. Each has its own change document in this folder with evidence, tests and rollback.

| Order | Change | Doc | Migration |
|---|---|---|---|
| 1 | Company bank details on invoices | `2026-09-company-bank-details.md` | `0127_company_bank_details` (nullable columns) |
| 2 | API data correctness, limits and honest errors | `2026-09-api-data-correctness.md` | none |
| 3 | Fuel price pipeline | `2026-09-fuel-pipeline.md` | `0128_fuelprice_provenance` (nullable columns) |
| 4 | Toll class and VAT | `2026-09-toll-class-vat.md` | `0129_vehicletype_sanral_toll_class` (nullable column + seeded-default class) |
| 5 | Tenant isolation | `2026-09-tenant-isolation.md` | none |

## Why one integration branch

Three of the original PRs each added a migration numbered `0127` on top of `0126`. Merged separately, Django would find three conflicting leaf migrations and refuse to migrate. Here they are chained in a single line: `0126 → 0127 → 0128 → 0129`. All migrations are additive and nullable; existing data is unchanged.

## Rolling back

Migrations roll back newest first. To remove a middle change, first roll back the ones after it:

- Tolls only: `migrate core 0128`
- Fuel and tolls: `migrate core 0127`
- All three: `migrate core 0126_customer_contact_optional`

Revert the code after rolling back the schema.

## Deploy notes for production

- Diesel moves from the 500ppm to the 50ppm price at the first 06:00 refresh after deploy (about +R0,44/L inland). Intended; the field was always documented as 50ppm.
- Tolls entering new quotes become VAT-exclusive; saved quotes, loads and invoices are not re-priced.
- Check production for logins with `is_superuser` or `is_staff`: superusers who belong to a company are now scoped to that company on normal app pages (admin pages unchanged).
- Rate limits rise (reads 600/min, writes 120/min; env `USER_READ_THROTTLE_RATE`, `USER_WRITE_THROTTLE_RATE`). Login and OTP limits are unchanged.
- Frontend follow-ups that pair with this: live diesel in the quote builder (frontend PR #119) and the bank details settings card (frontend PR #118).
