###########################################################################
#          (C) Vrije Universiteit, Amsterdam (the Netherlands)            #
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
import json
from datetime import datetime
from time import mktime

from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.db.models import Q
from django.forms import ChoiceField, CharField, Select

from amcat.models import Medium, ArticleSet, CodingSchemaField, Code
from amcat.scripts.query import QueryAction, QueryActionForm
from amcat.tools.aggregate import get_relative
from amcat.tools.keywordsearch import SelectionSearch, SearchQuery

X_AXES = tuple((c, c.title()) for c in ("date", "medium", "term", "set"))
Y_AXES = tuple((c, c.title()) for c in ("medium", "term", "set", "total"))

INTERVALS = tuple((c, c.title()) for c in ("day", "week", "month", "quarter", "year"))


class AggregationEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return int(mktime(obj.timetuple())) * 1000
        if isinstance(obj, Medium) or isinstance(obj, ArticleSet):
            return {"id": obj.id, "label": obj.name}
        if isinstance(obj, SearchQuery):
            return {"id": obj.label, "label": obj.query}
        if isinstance(obj, (CodingSchemaField, Code)):
            return {"id": obj.id, "label": obj.label}
        return super(AggregationEncoder, self).default(obj)


MEDIUM_ERR = "Could not find medium with id={column} or name={column}"

class AggregationActionForm(QueryActionForm):
    x_axis = ChoiceField(label="X-axis (rows)", choices=X_AXES, initial="date")
    y_axis = ChoiceField(label="Y-axis (columns)", choices=Y_AXES, initial="medium")
    interval = ChoiceField(choices=INTERVALS, required=False, initial="day")
    relative_to = CharField(widget=Select, required=False)

    def __init__(self, *args, **kwargs):
        super(AggregationActionForm, self).__init__(*args, **kwargs)

        self.fields["relative_to"].widget.attrs = {
            "class": "depends-will-be-added-by-query-js",
            "data-depends-on": json.dumps(["y_axis", "query", "mediums"]),
            "data-depends-url": "/api/v4/query/statistics/?project={project}&format=json",
            "data-depends-value": "{id}",
            "data-depends-label": "{label}",
        }
        assert not self.codingjobs

    def clean_relative_to(self):
        column = self.cleaned_data['relative_to']

        if not column:
            return None

        y_axis = self.cleaned_data['y_axis']

        if y_axis == "medium":
            if int(column) not in (m.id for m in self.cleaned_data["mediums"]):
                raise ValidationError(MEDIUM_ERR.format(column=column))
            return Medium.objects.get(id=int(column))

        if y_axis == "term":
            queries = SelectionSearch(self).get_queries()
            queries = {q.label: q for q in queries}
            if column not in queries:
                raise ValidationError("Term '{column}' not found in search terms.".format(column=column))
            return queries[column]

        if y_axis == "set":
            if int(column) not in (aset.id for aset in self.articlesets):
                raise ValidationError("Set '{column}' not available.".format(column=column))
            return ArticleSet.objects.get(id=int(column))

        raise ValidationError("Not a valid column name.")


class AggregationAction(QueryAction):
    """
    Aggregate articles based on their properties. Make sure x_axis != y_axis.
    """
    output_types = (
        ("text/json+aggregation+table", "Table"),
        ("text/json+aggregation+barplot", "Bar plot"),
        ("text/json+aggregation+scatter", "Scatter plot"),
        ("text/json+aggregation+line", "Line plot"),
        ("text/json+aggregation+heatmap", "Heatmap"),
        ("text/csv", "CSV (Download)"),
    )
    form_class = AggregationActionForm

    def run(self, form):
        self.monitor.update(1, "Executing query..")
        selection = SelectionSearch(form)
        narticles = selection.get_count()
        self.monitor.update(10, "Found {narticles} articles. Aggregating..".format(**locals()))

        # Get aggregation
        x_axis = form.cleaned_data['x_axis']
        y_axis = form.cleaned_data['y_axis']
        interval = form.cleaned_data['interval']
        aggregation = selection.get_aggregate(x_axis, y_axis, interval)

        self.monitor.update(20, "Calculating relative values..".format(**locals()))
        column = form.cleaned_data['relative_to']

        if column is not None:
            aggregation = list(get_relative(aggregation, column))

        self.monitor.update(60, "Serialising..".format(**locals()))
        return json.dumps(list(aggregation), cls=AggregationEncoder, check_circular=False)


class AggregationColumnAction(QueryAction):
    pass
