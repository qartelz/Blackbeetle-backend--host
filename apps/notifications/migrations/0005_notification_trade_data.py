# Generated by Django 5.1.4 on 2025-02-21 08:46

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0004_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='notification',
            name='trade_data',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
