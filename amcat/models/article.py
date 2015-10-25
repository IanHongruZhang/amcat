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

"""
Model module containing the Article class representing documents in the
articles database table.
"""

from __future__ import unicode_literals, print_function, absolute_import
from copy import copy
from itertools import chain, count
from operator import attrgetter
import logging
import collections

from django.template.loader import get_template
from django.template import Context
from django.db import models, transaction
from django.db.utils import IntegrityError, DatabaseError
from django.core.exceptions import ValidationError
from django.template.defaultfilters import escape as escape_filter
from amcat.tools.djangotoolkit import bulk_insert_returning_ids

from amcat.tools.model import AmcatModel, PostgresNativeUUIDField
from amcat.tools import amcates
from amcat.models.authorisation import Role
from amcat.models.medium import Medium
from amcat.tools.toolkit import splitlist
from amcat.tools.tree import Tree
from amcat.tools.progress import ProgressMonitor

from amcat.tools.amcates import Result

log = logging.getLogger(__name__)

import re

WORD_RE = re.compile('[{L}{N}]+')  # {L} --> All (unicode) letters
# {N} --> All numbers


def word_len(txt):
    """Count words in `txt`

    @type txt: str or unicode"""
    if not txt: return 0  # Safe handling of txt=None
    return len(re.sub(WORD_RE, ' ', txt).split())


def unescape_em(txt):
    """
    @param txt: text to be unescaped
    @type txt: unicode
    """
    return (txt
            .replace("&lt;em&gt;", "<em>")
            .replace("&lt;/em&gt;", "</em>"))


class ArticleTree(Tree):
    @property
    def article(self):
        return self.obj

    def get_ids(self):
        return self.traverse(func=lambda t: t.obj.id)

    def get_html(self, active=None, articleset=None):
        """
        Returns tree represented as HTML.

        @param active: highlight this article (wrap in em-tags)
        @type active: amcat.models.Article

        @param articleset: for all articles in this tree which are also in this
                           articleset, created a hyperlink.
        @type articleset: amcat.models.ArticleSet
        @rtype: compiled Template object
        """
        articles = set()
        if articleset is not None:
            articles = articleset.articles.filter(id__in=self.get_ids())
            articles = set(articles.values_list("id", flat=True))

        context = Context(dict(locals(), tree=self))
        return get_template("amcat/article_tree_root.html").render(context)


class Article(AmcatModel):
    """
    Class representing a newspaper article
    """
    __label__ = 'headline'

    id = models.AutoField(primary_key=True, db_column="article_id")

    date = models.DateTimeField(db_index=True)
    section = models.CharField(blank=True, null=True, max_length=500)
    pagenr = models.IntegerField(blank=True, null=True)
    headline = models.TextField()
    byline = models.TextField(blank=True, null=True)
    length = models.IntegerField(blank=True)
    metastring = models.TextField(null=True, blank=True)
    url = models.TextField(null=True, blank=True, db_index=True, max_length=750)
    externalid = models.IntegerField(blank=True, null=True)
    author = models.TextField(blank=True, null=True, max_length=100)
    addressee = models.TextField(blank=True, null=True, max_length=100)
    uuid = PostgresNativeUUIDField(db_index=True, unique=True)

    #sets = models.ManyToManyField("amcat.Set", db_table="sets_articles")

    text = models.TextField()

    parent = models.ForeignKey("self", null=True, db_column="parent_article_id",
                               db_index=True, blank=True, related_name="children")
    project = models.ForeignKey("amcat.Project", db_index=True, related_name="articles")
    medium = models.ForeignKey(Medium, db_index=True)

    insertscript = models.CharField(blank=True, null=True, max_length=500)
    insertdate = models.DateTimeField(blank=True, null=True, auto_now_add=True)

    def __init__(self, *args, **kwargs):
        super(Article, self).__init__(*args, **kwargs)
        self._highlighted = False

    class Meta():
        db_table = 'articles'
        app_label = 'amcat'

    def highlight(self, query, escape=True, keep_em=True):
        """
        Highlight headline and text property by inserting HTML tags (em). You won't be able to
        call save() after calling this method.

        @param query: elastic query used for highlighting
        @type query: unicode

        @param escape: escape html entities in result
        @type escape: bool

        @param keep_em: has no effect if escape is False. Will unescape em tags, which
                        are used for highlighting.
        @type keep_em: bool
        """
        if self._highlighted: return
        self._highlighted = True

        highlighted = amcates.ES().highlight_article(self.id, query)

        if not highlighted:
            # No hits for this search query
            return

        self.text = highlighted.get("text", self.text)
        self.headline = highlighted.get("headline", self.headline)

        if escape:
            self.text = escape_filter(self.text)
            self.headline = escape_filter(self.headline)

            if keep_em:
                self.text = unescape_em(self.text)
                self.headline = unescape_em(self.headline)

        return highlighted

    @property
    def children(self):
        """Return a sequence of all child articles (eg reactions to a post)"""
        return Article.objects.filter(parent=self)

    def save(self, *args, **kwargs):
        if self._highlighted:
            raise ValueError("Cannot save a highlighted article.")

        if self.length is None:
            self.length = word_len(self.text)

        super(Article, self).save(*args, **kwargs)


    def words(self):
        """@return: a generator yielding all words in all sentences"""
        for sentence in self.sentences:
            for word in sentence.words:
                yield word

    def get_sentence(self, parnr, sentnr):
        """@return: a Sentence object with the given paragraph and sentence number"""
        for s in self.sentences:
            if s.parnr == parnr and s.sentnr == sentnr:
                return s

    def getSentence(self, parnr, sentnr):
        return self.get_sentence(parnr, sentnr)

    ## Auth ##
    def can_read(self, user):
        if user.is_superuser:
            return True

        # Check default role on project
        read_meta = Role.objects.get(label='metareader', projectlevel=True)
        if self.project.guest_role.id >= read_meta.id:
            return True

        # Check users role on project
        if user.projectrole_set.filter(project__articles__article=self, role__id__gt=read_meta.id):
            return True

        return False

    def __repr__(self):
        return "<Article %s: %r>" % (self.id, self.headline)

    @classmethod
    def exists(cls, article_ids, batch_size=500):
        """
        Filters the given articleids to remove non-existing ids
        """
        for batch in splitlist(article_ids, itemsperbatch=batch_size):
            for aid in Article.objects.filter(pk__in=batch).values_list("pk", flat=True):
                yield aid

    @classmethod
    def create_articles(cls, articles, articleset=None, articlesets=None, create_id=False,
                        monitor=ProgressMonitor()):
        """
        Add the given articles to the database, the index, and the given set

        Article objects can contain a 'custom' nested_articles attribute. In that case,
        this should be a list of article-like objects that will also be saved, and will
        have the .parent set to the containing article

        @param articles: a collection of objects with the necessary properties (.headline etc)
        @param articleset: an articleset object
        @param articlesets: a sequence of articleset object
        @param create_id: if True, also create articles that have an .id (for testing)
        (the 'existing' article *is* added to the set.
        """
        # TODO: test parent logic (esp. together with hash/dupes)
        if articlesets is None:
            articlesets = [articleset] if articleset else []
        
        es = amcates.ES()
        for a in articles:
            if a.length is None:
                a.length = word_len(a.text) + word_len(a.headline) + word_len(a.byline)
        # existing / duplicate article ids to add to set
        add_existing_to_set = collections.defaultdict(set) # aset : [aid]
        # add dict (+hash) as property on articles so we know who is who
        sets = {aset.id: aset for aset in articlesets}
        to_create = [] 
        for a in articles:
            if a.id and not create_id:
                # article already exists, only add to set
                add_existing_to_set.add(a.id)
            else:
                a.es_dict = amcates.get_article_dict(a, sets=list(sets))
                to_create.append(a)

        if True:
            hashes = [a.es_dict['hash'] for a in to_create]
            results = es.query_all(filters={'hashes': hashes}, fields=["hash", "sets"], score=False)
            dupes = {r.hash: r for r in results}

        else:
            dupes = {}

        uuids = [a.es_dict['uuid'] for a in to_create if a.es_dict['uuid']]
        if uuids:
            results = es.query_all(filters={'uuid': uuids}, fields=["uuid", "sets"], score=False)
            dupes.update({r.uuid: r for r in results})

        monitor.update(10, "Creating {} articles, {} dupes, {} existing"
                       .format(len(to_create), len(dupes), len(add_existing_to_set)))
        
        # add all non-dupes to the db, needed actions
        add_new_to_set = set()  # new article ids to add to set
        add_to_index = []  # es_dicts to add to index
        result = []  # return result
        errors = []  # return errors
        for a in to_create:
            dupe = dupes.get(a.es_dict['hash'], None)
            if a.uuid and not dupe:
                dupe = dupes.get(a.uuid, None)
            a.duplicate = bool(dupe)
            if a.duplicate:
                a.id = dupe.id
                for aset in set(sets) - set(dupe.sets or []):
                    add_existing_to_set[aset].add(dupe.id)
                result.append(dupe)
            else:
                if a.parent:
                    a.parent_id = a.parent.id

                sid = transaction.savepoint()
                try:
                    a.save()
                    transaction.savepoint_commit(sid)
                except (IntegrityError, ValidationError, DatabaseError) as e:
                    log.warning(str(e))
                    transaction.savepoint_rollback(sid)
                    errors.append(e)
                    continue
                a.es_dict['id'] = a.pk
                a.es_dict['uuid'] = a.uuid
                add_to_index.append(a.es_dict)
                add_new_to_set.add(a.pk)
                result.append(a)

        monitor.update(50, "Considered {} articles: {} saved to db, {} new to add to index, {} existing/duplicates to add to set"
                       .format(len(articles), len(add_new_to_set), len(add_to_index), len(add_existing_to_set)))

        # add to index
        if add_to_index:
            es.bulk_insert(add_to_index, batch_size=100)
        monitor.update(10, "Added {} articles to index".format(len(add_to_index)))

        for setid, articles in add_existing_to_set.iteritems():
            sets[setid].add_articles(articles, add_to_index=True)
        monitor.update(10, "Added existing articles to articlesets")
            
        if add_new_to_set:
            for setid in sets:
                sets[setid].add_articles(add_new_to_set, add_to_index=False)
        monitor.update(10, "Added {} new articles to articlesets, done creating articles!"
                       .format(len(add_new_to_set)))

        # if there are any 'dupe' placeholders, convert them to article objects
        placeholders = [res.id for res in result if isinstance(res, Result)]
        placeholders = list(Article.objects.filter(pk__in=placeholders).only("pk"))
        result = [res for res in result if not isinstance(res, Result)] + placeholders
        
        return result, errors

    @classmethod
    def save_trees(cls, trees):
        """
        Saves a list of article trees efficiently to database.

        @type trees: [ArticleTree]
        """
        #trees = map(copy, trees)

        for level in count():
            level_trees = list(chain.from_iterable(tree.get_level(level) for tree in trees))

            if not level_trees:
                break

            for tree in level_trees:
                if tree.parent is None:
                    continue
                tree.obj.parent = tree.parent.obj

            articles = bulk_insert_returning_ids(t.obj for t in level_trees)
            for tree, article in zip(level_trees, articles):
                tree.obj = article


    def get_tree(self, include_parents=True, fields=("id",)):
        """
        Returns a deterministic (sorted by id) tree of articles, based on their parent
        property. It runs O(2n) database queries, where `n` depth of tree queries.

        @param include_parents: start at root of tree, instead of this node
        @type include_parents: bool

        @param fields:
        @type fields: tuple of string

        @rtype: ArticleTree
        """
        # Do we need to render the complete tree?
        if include_parents and self.parent and self.parent.id != self.id:
            return self.parent.get_tree(include_parents=True)

        children = self.children.order_by("id").only(*fields)
        return ArticleTree(self, [c.get_tree(include_parents=False) for c in children
                                  if c.id != self.id])

