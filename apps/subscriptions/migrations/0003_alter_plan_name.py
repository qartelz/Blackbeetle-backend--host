# Generated by Django 5.1.4 on 2025-01-14 17:49

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0002_alter_plan_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='plan',
            name='name',
            field=models.CharField(choices=[('BASIC', 'Basic'), ('PREMIUM', 'Premium'), ('SUPER_PREMIUM', 'Super Premium')], max_length=30),
        ),
    ]
