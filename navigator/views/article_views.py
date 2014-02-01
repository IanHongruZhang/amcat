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

from .articleset_views import ArticleSetDetailsView
from amcat.models import Article, ArticleSet, Sentence
from navigator.views.projectview import ProjectViewMixin, HierarchicalViewMixin, BreadCrumbMixin, ProjectFormView, ProjectActionRedirectView
from django.views.generic.detail import DetailView
from django import forms
from amcat.forms import widgets
from amcat.nlp import sbd
from itertools import chain
from django.shortcuts import render
from django.core.urlresolvers import reverse
from amcat.models import authorisation
from django.core.exceptions import PermissionDenied
from django.template.defaultfilters import escape
from navigator.views.project_views import ProjectDetailsView
from amcat.tools import amcates


def escape_keepem(text):
    # hack, escape everything except for em
     text = escape(text)
     text = text.replace("&lt;em&gt;", "<em class='highlight'>")
     text = text.replace("&lt;/em&gt;", "</em>")
     return text

class ArticleDetailsView(HierarchicalViewMixin, ProjectViewMixin, BreadCrumbMixin, DetailView):
    model = Article

    def can_view_text(self):
        """Checks if the user has the right to edit this project"""
        return self.request.user.get_profile().has_role(authorisation.ROLE_PROJECT_READER, self.object.project)
    
    def get_highlight(self):
        if not self.last_query: return None
        try:
            return self._highlight
        except AttributeError:
             self._highlight = amcates.ES().highlight_article(self.object.id, self.last_query)
             return self._highlight

        
    def get_headline(self):
        hl = self.get_highlight()
        if hl and "headline" in hl:
            return escape_keepem(hl["headline"])
        return escape(self.object.headline)
        
    def get_text(self):
        hl = self.get_highlight()
        if hl and "text" in hl:
            return escape_keepem(hl["text"])
        return escape(self.object.text)

    
    def get_context_data(self, **kwargs):
        context = super(ArticleDetailsView, self).get_context_data(**kwargs)
        context['text'] = self.get_text()
        context['headline'] = self.get_headline()
        # HACK: put query back on session to allow viewing more articles
        self.request.session["query"] = self.last_query
        return context
    
    
class ArticleSetArticleDetailsView(ArticleDetailsView):
    parent = ArticleSetDetailsView
    model = Article

    def get_context_data(self, **kwargs):
        context = super(ArticleSetArticleDetailsView, self).get_context_data(**kwargs)
        context['articleset_id'] = self.kwargs['articleset_id']
        context['text'] = escape(self.object.text)
        return context

    

    
class ProjectArticleDetailsView(ArticleDetailsView):
    model = Article
    parent = ProjectDetailsView
    context_category = 'Articles'
    template_name = 'project/article_details.html'
    url_fragment = "articles/(?P<article_id>[0-9]+)"
    
    @classmethod
    def _get_breadcrumb_name(cls, kwargs, view):
        aid = kwargs['article_id']
        a = Article.objects.get(pk=aid)
        return "Article {a.id} : {a}".format(**locals())
    @classmethod
    def get_view_name(cls):
        return "project-article-details"

 
    
class ArticleRemoveFromSetView(ProjectActionRedirectView):
    parent = ProjectArticleDetailsView
    url_fragment = "removefromset"
    def action(self, **kwargs):
        remove_set = int(self.request.GET["remove_set"])
        # user needs to have writer+ on the project of the articleset
        project = ArticleSet.objects.get(pk=remove_set).project
        if not self.request.user.get_profile().has_role(authorisation.ROLE_PROJECT_WRITER, project):
            raise PermissionDenied("User {self.request.user} has insufficient rights on project {project}".format(**locals()))

            
        articles = [int(kwargs["article_id"])]
        ArticleSet.objects.get(pk=remove_set).remove_articles(articles)
    
        
    def get_redirect_url(self, project_id, article_id):
        remove_set = int(self.request.GET["remove_set"])
        return_set = self.request.GET.get("return_set")
        if return_set:
            return_set = int(return_set)
            if remove_set != return_set:
                return reverse(ArticleSetArticleDetailsView.get_view_name(), args=(project_id, return_set, article_id))
        return super(ArticleRemoveFromSetView, self).get_redirect_url(project_id=project_id, article_id=article_id)

    def success_message(self, result=None):
        article = self.kwargs["article_id"]
        remove_set =  int(self.request.GET["remove_set"])
        return "Removed the current article ({article}) from set {remove_set}".format(**locals())
        
        
################################################################################
# Splitting. Yes, it was that much work                                        #
################################################################################
        
def get_articles(article, sentences):
    """
    Split `article` with `sentences` as delimeters. For each sentence the text
    before it, including itself, is copied to a new article with is yield.

    @param sentences: delimeters for splitting
    @type sentences: QuerySet

    @param article: article which contains sentences
    @type article: models.Article

    @requires: ordering of sentences ("parnr", "sentnr")
    @requires: sbd.get_or_create_sentences() called on `article`
    @requires: all(a in article.sentences.all() for a in sentences)

    @returns: generator with newly splitted articles (not saved)
    @raises: ValueError if a sentence in `sentences` is not in article.sentences
    """
    new_article = copy_article(article)

    # Get sentence, skipping the headline 
    all_sentences = list(article.sentences.all()[1:])

    not_in_article = set(sentences) - set(all_sentences)
    if not_in_article:
        raise ValueError(
            "Sentences specified as delimters, but not in article: {not_in_article}. Did you try to split on a headline?"
            .format(**locals())
        )

    prev_parnr = 1 
    for parnr, sentnr in chain(sentences.values_list("parnr", "sentnr"), ((None, None),)):
        # Skip headline paragraph
        if parnr == 1: continue

        while True:
            try: sent = all_sentences.pop(0)
            except IndexError:
                new_article.text = new_article.text.strip()
                yield new_article
                break

            if sent.parnr != prev_parnr:
                new_article.text += "\n\n"

            new_article.text += sent.sentence
            new_article.text += ". "
            prev_parnr = sent.parnr

            if (sent.sentnr == sentnr and sent.parnr == parnr):
                new_article.text = new_article.text.strip()
                yield new_article
                new_article = copy_article(article)
                break


def handle_split(form, project, article, sentences):
    articles = list(get_articles(article, sentences))

    # We won't use bulk_create yet, as it bypasses save() and doesn't
    # insert ids
    for art in articles:
        art.save()
        sbd.create_sentences(art)

    # Context variables for template
    form_data = form.cleaned_data 
    all_sets = list(project.all_articlesets().filter(articles=article))

    # Keep a list of touched sets, so we can invalidate their indices
    dirty_sets = ArticleSet.objects.none()

    # Add splitted articles to existing sets
    ArticleSet.articles.through.objects.bulk_create([
        ArticleSet.articles.through(articleset=aset, article=art) for
            art in articles for aset in form_data["add_splitted_to_sets"]
    ])

    # Collect changed sets
    for field in ("add_splitted_to_sets", "remove_from_sets", "add_to_sets"):
        dirty_sets |= form_data[field]

    # Add splitted articles to sets wherin the original article live{d,s}
    if form_data["add_splitted_to_all"]:
        articlesetarts = ArticleSet.articles.through.objects.filter(article=article, articleset__project=project)

        ArticleSet.articles.through.objects.bulk_create([
            ArticleSet.articles.through(articleset=asetart.articleset, article=art)
                for art in articles for asetart in articlesetarts
        ])

        dirty_sets |= project.all_articlesets().filter(articles=article).only("id")

    if form_data["remove_from_sets"]:
        for aset in form_data["remove_from_sets"]:
            aset.remove_articles([article])
        
    if form_data["remove_from_all_sets"]:
        for aset in ArticleSet.objects.filter(project=project, articles=article).distinct():
            aset.remove_articles([article])

    if form_data["add_splitted_to_new_set"]:
        new_splitted_set = ArticleSet.create_set(project, form_data["add_splitted_to_new_set"], articles)

    if form_data["add_to_sets"]:
        for articleset in form_data["add_to_sets"]:
            articleset.add_articles([article])

    if form_data["add_to_new_set"]:
        new_set = ArticleSet.create_set(project, form_data["add_to_new_set"], [article])

    return locals()

def copy_article(article):
    new = Article.objects.get(id=article.id)
    new.id = None
    new.uuid = None
    new.text = ""
    new.length = None
    new.byline = None
    return new

def _get_sentences(sentences, prev_parnr=1):
    """
    Yields (sentence, bool) where bool indicates whether this sentences starts
    a new paragraph.
    """
    for sentence in sentences:
        yield (sentence, prev_parnr != sentence.parnr)
        prev_parnr = sentence.parnr

def parse_sentence_name(name):
    if not name.startswith("sentence-"): return

    try:
        return int(name.split("-")[1])
    except IndexError, ValueError:
        pass

def get_sentence_ids(post):
    for name, checked in post.items():
        if checked != "on": continue
        yield parse_sentence_name(name)

class ArticleSplitView(ProjectFormView):
    parent = ProjectArticleDetailsView
    url_fragment = "split"
    template_name = "project/article_split.html"


    @classmethod
    def _get_breadcrumb_name(cls, kwargs, view):
         return cls.url_fragment
    
    class form_class(forms.Form):
        add_to_new_set = forms.CharField(required=False) 
        add_to_sets = forms.ModelMultipleChoiceField(queryset=ArticleSet.objects.none(), widget=widgets.JQueryMultipleSelect, required=False)
        
        remove_from_sets = forms.ModelMultipleChoiceField(queryset=ArticleSet.objects.none(), widget=widgets.JQueryMultipleSelect, required=False)
        remove_from_all_sets = forms.BooleanField(initial=True, required=False, help_text="Remove all instances of the original article in this project")
    
        add_splitted_to_sets = forms.ModelMultipleChoiceField(queryset=ArticleSet.objects.none(), widget=widgets.JQueryMultipleSelect, required=False)
        add_splitted_to_new_set = forms.CharField(required=False)
        add_splitted_to_all = forms.BooleanField(initial=False, required=False, help_text="Add new (splitted) articles to all sets containing the original article")

    def get_form(self, form_class):
        form = super(ArticleSplitView, self).get_form(form_class)
        form.fields["add_splitted_to_sets"].queryset = self.project.all_articlesets()
        form.fields["remove_from_sets"].queryset = self.project.all_articlesets().filter(articles=self.article)
        form.fields["add_to_sets"].queryset = self.project.all_articlesets()
        return form

    @property
    def article(self):
        return Article.objects.get(pk=self.kwargs['article_id'])
        
    def form_valid(self, form):
        selected_sentence_ids = set(get_sentence_ids(self.request.POST)) - {None,}
        if selected_sentence_ids:
            sentences = Sentence.objects.filter(id__in=selected_sentence_ids)
            context = handle_split(form, self.project, self.article, sentences)
            return render(self.request, "project/article_split_done.html", context)

    def get_context_data(self, **kwargs):
        ctx = super(ArticleSplitView, self).get_context_data(**kwargs)
        sentences = sbd.get_or_create_sentences(self.article).only("sentence", "parnr")
        ctx["sentences"] = _get_sentences(sentences)
        ctx["sentences"].next() # skip headline
        return ctx
