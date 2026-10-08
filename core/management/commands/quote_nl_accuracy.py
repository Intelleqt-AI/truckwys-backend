"""Print the deterministic voice/NL quote parser's accuracy on the phrase
fixtures (tuning set and held-out set). No LLM, no network."""
from django.core.management.base import BaseCommand

from core.services.quote_nl_eval import evaluate


class Command(BaseCommand):
    help = "Measure the deterministic quote pre-parser against the EN/AF phrase fixtures."

    def handle(self, *args, **options):
        from core.tests.voice_quote_fixtures import CASES, HELDOUT, TODAY, VERIFIER, VERIFIER2
        own = [c for c in HELDOUT if c not in VERIFIER and c not in VERIFIER2]
        for name, cases in (("tuning", CASES), ("held-out (own)", own), ("held-out (verifier 1)", VERIFIER),
                            ("held-out (verifier 2)", VERIFIER2)):
            r = evaluate(cases, TODAY)
            self.stdout.write(
                f"{name}: fields {r['fields_ok']}/{r['fields']} ({r['field_accuracy']:.1%}), "
                f"phrases fully right {r['cases_ok']}/{r['cases']} ({r['case_accuracy']:.1%})")
            for k, (ok, n) in sorted(r["per_field"].items()):
                if ok != n:
                    self.stdout.write(f"   {k}: {ok}/{n}")
            for f in r["failures"]:
                self.stdout.write(f"   FAIL {f['id']}: {f['text']}\n      " + "; ".join(f["errors"]))
