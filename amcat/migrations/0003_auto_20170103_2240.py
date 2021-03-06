# -*- coding: utf-8 -*-
# Generated by Django 1.9.10 on 2017-01-03 22:40
from __future__ import unicode_literals

import amcat.models.article
import django.contrib.postgres.fields.jsonb
from django.db import migrations, models
import jsonfield.fields


class Migration(migrations.Migration):

    dependencies = [
        ('amcat', '0002_initial_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='r_plugin_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='article',
            name='properties',
            field=amcat.models.article.PropertyField(default='{}'),
        ),
        migrations.AlterField(
            model_name='query',
            name='parameters',
            field=django.contrib.postgres.fields.jsonb.JSONField(default={}),
        ),
        migrations.AlterField(
            model_name='task',
            name='arguments',
            field=jsonfield.fields.JSONField(default=dict),
        ),
    ]
