# -*- coding: utf-8 -*-
# Generated by Django 1.9.10 on 2016-12-25 17:15
from __future__ import unicode_literals

from django.db import migrations
from django.conf import settings

from amcat.tools.amcates import ES


def set_default_similarity(*args, **kwargs):
    # Make sure index exists
    es = ES()
    es.check_index()
    es.refresh()

    # Push new settings to indices
    indices = es.es.indices
    indices.close(es.index)
    indices.put_settings(settings.ES_SETTINGS, es.index)
    indices.open(es.index)


class Migration(migrations.Migration):

    dependencies = [
        ('amcat', '0002_initial_data'),
    ]

    operations = {
        migrations.RunPython(set_default_similarity),
    }
