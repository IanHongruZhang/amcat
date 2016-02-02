# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2016-02-01 21:34
from __future__ import unicode_literals
from django.core.management import call_command
from django.db import migrations

fixture = '_initial_data'


def load_fixture(apps, schema_editor):
    call_command('loaddata', fixture, app_label='amcat')


class Migration(migrations.Migration):
    dependencies = [
        ('amcat', '0010_remove_query_private'),
    ]

    operations = [
        migrations.RunPython(load_fixture),
    ]