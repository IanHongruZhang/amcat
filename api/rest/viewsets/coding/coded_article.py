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
from rest_framework import serializers
from rest_framework.viewsets import ReadOnlyModelViewSet

from amcat.models import Sentence, CodedArticle, Article, Medium, ROLE_PROJECT_READER
from amcat.tools.caching import cached
from amcat.tools import sbd
from api.rest.mixins import DatatablesMixin
from api.rest.serializer import AmCATModelSerializer
from api.rest.viewset import AmCATViewSetMixin
from api.rest.viewsets.coding.codingjob import CodingJobViewSetMixin
from api.rest.viewsets.project import ProjectViewSetMixin, ProjectPermission
from api.rest.viewsets.sentence import SentenceSerializer, SentenceViewSetMixin


__all__ = (
    "CodedArticleSerializer", "CodedArticleViewSetMixin", "CodedArticleViewSet",
    "CodedArticleSentenceViewSet")

def article_property(property_name):
    def inner(self, coded_article):
        return getattr(self.get_article(coded_article), property_name)
    return inner

# Hack class to allow queryset to be set, which in turn allows the metadata (OPTIONS) generator
# to include this field in its models-property.
class PseudoSerializerMethodField(serializers.SerializerMethodField):
    queryset = Medium.objects.none()

class CodedArticleSerializer(AmCATModelSerializer):
    headline = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    pagenr = serializers.SerializerMethodField()
    length = serializers.SerializerMethodField()
    article_id = serializers.SerializerMethodField()
    medium = PseudoSerializerMethodField()
    get_headline = article_property("headline")
    get_date = article_property("date")
    get_pagenr = article_property("pagenr")
    get_length = article_property("length")
    get_article_id = article_property("id")

    @classmethod
    def get_metadata_field_name(self, field):
        if hasattr(field, "method_name") and field.method_name is self.base_fields["medium"].method_name:
            return "ModelChoiceField"

    def _get_coded_articles(self):
        view = self.context["view"]
        return CodedArticle.objects.filter(id__in=view.filter_queryset(view.get_queryset()))

    @cached
    def _get_articles(self):
        aids = self._get_coded_articles().values_list("article__id", flat=True)
        articles = Article.objects.filter(id__in=aids).only("headline", "date", "pagenr", "length")
        return {a.id: a for a in articles}

    def get_article(self, coded_article):
        return self._get_articles().get(coded_article.article_id)

    def get_medium(self, coded_article):
        return self.get_article(coded_article).medium_id


    class Meta:
        model = CodedArticle

class CodedArticleViewSetMixin(AmCATViewSetMixin):
    queryset = CodedArticle.objects.all()
    serializer_class = CodedArticleSerializer
    model_key = "coded_article"
    model = CodedArticle

    class Meta:
        model = CodedArticle

class CodedArticleViewSet(ProjectViewSetMixin, CodingJobViewSetMixin,
                          CodedArticleViewSetMixin, DatatablesMixin, ReadOnlyModelViewSet):
    serializer_class = CodedArticleSerializer
    extra_filters = ("article__pagenr",)
    queryset = CodedArticle.objects.all()
    model = CodedArticle

    ordering_mapping = {
        "headline": "article__headline",
        "medium": "article__medium__name",
        "date": "article__date",
        "pagenr": "article__pagenr",
        "length": "article__length",
        "article_id": "article__id",
        "status": "status__id"
    }

    ordering_fields = (('id', "article_id")
                        + tuple(ordering_mapping.keys())
                        + tuple(ordering_mapping.values()))

    def filter_queryset(self, queryset):
        qs = super(CodedArticleViewSet, self).filter_queryset(queryset)
        return qs.filter(id__in=self.codingjob.coded_articles.all())


class CodedArticleSentenceViewSet(ProjectViewSetMixin, CodingJobViewSetMixin,
                                  CodedArticleViewSetMixin, SentenceViewSetMixin,
                                  DatatablesMixin, ReadOnlyModelViewSet):
    permission_classes = (ProjectPermission,)
    permission_map = {'GET': ROLE_PROJECT_READER}
    serializer_class = SentenceSerializer
    queryset = Sentence.objects.all()
    model = Sentence

    def check_permissions(self, request):
        if request.method != 'GET' or request.user != self.codingjob.coder:
            super(CodedArticleSentenceViewSet, self).check_permissions(request)

    def filter_queryset(self, queryset):
        qs = super(CodedArticleSentenceViewSet, self).filter_queryset(queryset)
        article = Article.objects.get(id=self.coded_article.article_id)
        sentences = qs.filter(id__in=sbd.get_or_create_sentences(article))
        return sentences

