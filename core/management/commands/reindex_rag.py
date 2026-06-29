"""Build/refresh per-account invoice embeddings for the Copilot RAG.

    python manage.py reindex_rag            # all companies
    python manage.py reindex_rag --company 3

Skips invoices whose data is unchanged (matched by source_hash), so it's safe to
run repeatedly / on a schedule. No-op (with a warning) when OPENAI_API_KEY is unset.
"""
from django.core.management.base import BaseCommand

from core.models import Company
from core.services import rag


class Command(BaseCommand):
    help = "Index company invoices into the Copilot RAG vector store (OpenAI embeddings)."

    def add_arguments(self, parser):
        parser.add_argument('--company', type=int, default=None, help='Only index this company id.')

    def handle(self, *args, **options):
        if not rag.rag_enabled():
            self.stderr.write(self.style.WARNING(
                'RAG not enabled: set OPENAI_API_KEY (and ensure openai + numpy are installed). Nothing indexed.'
            ))
            return

        qs = Company.objects.all()
        cid = options.get('company')
        if cid:
            qs = qs.filter(id=cid)

        total = 0
        for company in qs:
            n = rag.index_company_invoices(company)
            total += n
            self.stdout.write(f'  company {company.id} ({getattr(company, "company_name", "?")}): {n} invoices (re)embedded')

        self.stdout.write(self.style.SUCCESS(f'Done. {total} invoice embeddings written across {qs.count()} companies.'))
