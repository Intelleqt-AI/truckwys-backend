# Merge migration: unifies the two 0070-numbered leaf branches that diverged
# when `main` (0070_seed_toll_plazas) was merged onto a branch that already had
# its own untracked 0070_quoteoutcome_company_and_more -> 0071_quoteoutcome_route_popularity
# chain. No-op — same shape as `manage.py makemigrations --merge` would produce.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0070_seed_toll_plazas'),
        ('core', '0071_quoteoutcome_route_popularity'),
    ]

    operations = []
