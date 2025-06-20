# Generated by Django 5.1.4 on 2025-01-20 22:04

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trades', '0008_alter_insight_actual_image_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='insight',
            name='actual_image',
            field=models.ImageField(blank=True, help_text='Actual outcome chart', null=True, upload_to='trade_insights/actuals/'),
        ),
        migrations.AlterField(
            model_name='insight',
            name='prediction_image',
            field=models.ImageField(help_text='Technical analysis prediction chart', upload_to='trade_insights/predictions/'),
        ),
        migrations.AlterField(
            model_name='trade',
            name='image',
            field=models.ImageField(blank=True, help_text='Technical analysis chart or related image', null=True, upload_to='trade_images/%Y/%m/'),
        ),
    ]
