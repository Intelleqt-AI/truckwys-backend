from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_company_annual_turnover_company_b_bbee_level_and_more'),
    ]

    operations = [
        # Step 1: Rename Customer.company (CharField) to company_name
        migrations.RenameField(
            model_name='customer',
            old_name='company',
            new_name='company_name',
        ),
        # Step 2: Add company FK to Customer
        migrations.AddField(
            model_name='customer',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='customers', to='core.company'),
        ),
        # Step 3: Add company FK to User
        migrations.AddField(
            model_name='user',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='users', to='core.company'),
        ),
        # Step 4: Add company FK to Load
        migrations.AddField(
            model_name='load',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='loads', to='core.company'),
        ),
        # Step 5: Add company FK to Invoice
        migrations.AddField(
            model_name='invoice',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='invoices', to='core.company'),
        ),
        # Step 6: Add company FK to Vehicle
        migrations.AddField(
            model_name='vehicle',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='vehicles', to='core.company'),
        ),
        # Step 7: Add company FK to Expense
        migrations.AddField(
            model_name='expense',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='expenses', to='core.company'),
        ),
        # Step 8: Add company FK to Quote
        migrations.AddField(
            model_name='quote',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='quotes', to='core.company'),
        ),
        # Step 9: Add company FK to Payment
        migrations.AddField(
            model_name='payment',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='core.company'),
        ),
        # Step 10: Add company FK to Settlement
        migrations.AddField(
            model_name='settlement',
            name='company',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='settlements', to='core.company'),
        ),
    ]
