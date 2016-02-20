###########################################################################
# (C) Vrije Universiteit, Amsterdam (the Netherlands)                     #
#                                                                         #
# This file is part of AmCAT - The Amsterdam Content Analysis Toolkit     #
#                                                                         #
# AmCAT is free software: you can redistribute it and/or modify it under  #
# the terms of the GNU Affero General Public License as published by the  #
# Free Software Foundation, either version 3 of the License, or (at your  #
# option) any later version.                                              #
#                                                                         #
# AmCAT is distributed in the hope that it will be useful, but WITHOUT    #
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or   #
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public     #
# License for more details.                                               #
#                                                                         #
# You should have received a copy of the GNU Affero General Public        #
# License along with AmCAT.  If not, see <http://www.gnu.org/licenses/>.  #
###########################################################################

import logging
import itertools
from datetime import datetime,timedelta
from amcat.models import ArticleSet, Medium

log = logging.getLogger(__name__)

def _get_column(column):
    if isinstance(column, Medium) or isinstance(column, ArticleSet):
        return "{column.name}".format(column=column)
    return column.label

def _get_pivot(row, column):
    column = _get_column(column)
    for c, value in row:
        if str(c) == column:
            return float(value)
    return 0.0


def get_relative(aggregation, column):
    pivots = (_get_pivot(row[1], column) for row in aggregation)
    for pivot, (row, row_values) in zip(pivots, aggregation):
        if not pivot:
            continue
        yield row, tuple((col, value / pivot) for col, value in row_values)


def _get_ids(aggregation, group_by, id_type):
    ids = set()
    if group_by.pop(0) == id_type:
        ids = set(medium_id for medium_id, _ in aggregation)

    if not group_by:
        return ids

    aggregations = [a.buckets for a in aggregation]
    nested_ids = [_get_ids(b, list(group_by), id_type) for b in aggregations]
    nested_ids = set(map(int, itertools.chain.from_iterable(nested_ids)))
    return ids | nested_ids


def _get_objects(aggregation, group_by, id_type, objects):
    if not aggregation:
        return ()

    ntuple = aggregation[0].__class__

    if group_by.pop(0) == id_type:
        aggregation = [(objects.get(int(id)), aggr) for id, aggr in aggregation]

    if group_by:
        aggregation = [
            (key, _get_objects(aggr, list(group_by), id_type, objects))
            for key, aggr in aggregation
        ]

    return [ntuple(*aggr) for aggr in aggregation]


def get_objects(aggregation, group_by, only, klass, id_type, select_related=()):
    objects = klass.objects.only(*only).select_related(*select_related)
    objects = objects.in_bulk(_get_ids(aggregation, list(group_by), id_type))
    return _get_objects(aggregation, group_by, id_type, objects)


def get_mediums(aggregation, group_by, only=("name",), select_related=()):
    """Given an aggregation, replace all medium ids with Medium objects"""
    from amcat.models import Medium
    return get_objects(aggregation, group_by, only, Medium, "mediumid", select_related)


def get_articlesets(aggregation, group_by, only=("name",), select_related=()):
    """Given an aggregation, replace all articleset ids with Articleset objects"""
    return get_objects(aggregation, group_by, only, ArticleSet, "sets", select_related)

def _iter_dates( start, stop, interval):
    date = start
    if interval == 'day':
        while date < stop:
            yield date
            date = date + timedelta(1)
    elif interval == 'week':
        while date < stop:
            yield date
            date = date + timedelta(7)
    elif interval == 'month':
        day = date.day
        while date < stop:
            yield date.replace(day=day)
            date = date.replace(day=2) + timedelta(31)
    elif interval == 'quarter':
        day = date.day
        while date < stop:
            yield date.replace(day=day)
            date = date.replace(day=2) + timedelta(31 * 3)
    elif interval == 'year':
        day = date.day
        month = date.month
        while date < stop:
            yield date.replace(day=day,  month=month)
            date = date.replace(day=2, month=1) + timedelta(366)

def fill_zeroes(aggregation, interval):
    if len(aggregation) < 2:
        return aggregation

    aggregation = list(aggregation)
    aggregation.sort(key=lambda aggr: aggr.date)
    dates = set(aggr.date for aggr in aggregation)
    if hasattr(aggregation[0], "buckets"):
        bucket_names = set(bucket[0] for aggr in aggregation for bucket in aggr.buckets )
    else:
        bucket_names = None

    tuple_class = aggregation[0].__class__

    min_date = aggregation[0][0]
    max_date = aggregation[-1][0]

    for date in _iter_dates(min_date, max_date, interval):
        if date not in dates:
            aggregation.append(tuple_class(date, [(name, 0) for name in bucket_names] if bucket_names else 0))

    aggregation.sort(key=lambda aggr: aggr.date)
    return aggregation

# Unittests: amcat.tools.tests.aggregate
