# -*- coding: utf-8 -*-
# Generated by Django 1.9.10 on 2017-01-04 14:06
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('amcat', '0003_auto_20170103_2240'),
    ]

    operations = [
        migrations.RenameField(
            model_name='project',
            old_name='r_plugin_enabled',
            new_name='r_plugins_enabled',
        ),
    ]
