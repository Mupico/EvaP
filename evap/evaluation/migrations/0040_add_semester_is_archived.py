# Generated by Django 1.9.1 on 2016-02-15 19:13

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evaluation', '0039_auto_20160104_1726'),
    ]

    operations = [
        migrations.AddField(
            model_name='semester',
            name='is_archived',
            field=models.BooleanField(default=False, verbose_name='is archived'),
        ),
    ]
