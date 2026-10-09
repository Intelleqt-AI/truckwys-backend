# Tonnage quotes, weighbridge invoicing and volume contracts

Branch `truckwys/tonnage-quotes`. It is stacked on `truckwys/trip-economics`. Web and app PRs use the same branch name. The pricing rules are in `docs/QUOTE-RULES.md` under "Tonnage quotes".

## What it does
- **Per tonne pricing.**
  - `pricing_basis` is `per_load` (default) or `per_tonne`. Per tonne takes tonnes per load, a minimum tonnes per load and a rate in R/t.
  - With no truck chosen, it prices on the **safest** eligible truck, which is the one with the highest cost per tonne. Trucks too small for the tonnes per load are excluded. The saved `vehicle_type` is set to the name of `priced_vehicle_type`.
  - A rate below cost is a warning, never a block: "this loses R x", plus "Price at target · R x/t".
- **Weighbridge invoicing.**
  - `Load.actual_tonnes` and `weighbridge_slip` can be entered on delivery or sent by the TMS (`actual_tonnes`, `weighbridge_slip`).
  - The invoice bills the weighed tonnes, never less than the minimum. The slip number goes on the invoice line and PDF.
  - Draft invoices are re-priced. An issued invoice is never re-priced: the load gets `invoice_mismatch.code = weighed_after_invoicing`, which clears once the amounts agree again. A CANCELLED load ignores TMS tonnes.
- **Volume contracts** (one lane per contract).
  - A quote with `total_tonnes` and `contract_start`/`contract_end` is a contract. List contracts with `GET quotes/?contract=true`.
  - Call-off loads book through the trip-economics booking (`convert_to_load` with `tonnes`, and `booking-preview?tonnes=`).
  - Each call-off is capped at the remaining tonnes and at the largest eligible truck's payload (`volume_contract.max_tonnes_per_load`), with a minimum of 0,1 t. The cap is checked under a row lock, so parallel call-offs can't overbook.
  - A one-consignment quote books once, at most at its quoted tonnes.
  - Call-offs are costed at booking from the contract's priced truck.

## Deploy
- **Migration** `0177_quote_load_tonnage`. It adds columns only. Every new NOT NULL column has a database default, so containers still on the old image keep creating quotes and loads during a rolling deploy. It is reversible (`migrate core 0176`).
- No new env vars and no new Celery tasks.
- **Validation:** call-off and TMS tonnes must be finite and above 0, with at most 3 decimals. Bad input returns 400, or a per-record sync error. It never returns a 500.

## Tests
- `core/tests/test_tonnage_quotes.py` (33 tests).
- 12 tonnage golden cases in `core/tests/fixtures/quote_golden.json`. The file is byte-identical in backend, web and app.
- Full suite run serially: the same failures as the trip-economics base, none new.

## Not in v1
Multi-lane contracts, a win chance for per-tonne quotes, and a company default for minimum tonnes.
