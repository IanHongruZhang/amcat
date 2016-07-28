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


import logging
import collections
import binascii

from django.db import models
from django.template import Context

from django.core.exceptions import PermissionDenied
from django.template.defaultfilters import escape as escape_filter
from django.template.loader import get_template

from django.contrib.postgres.fields import JSONField

from amcat.models.authorisation import Role
from amcat.tools import amcates
from amcat.tools.djangotoolkit import bulk_insert_returning_ids
from amcat.tools.model import AmcatModel, PostgresNativeUUIDField
from amcat.tools.progress import ProgressMonitor
from amcat.tools.toolkit import splitlist
from amcat.tools.tree import Tree
from amcat.models.authorisation import ROLE_PROJECT_READER

log = logging.getLogger(__name__)

import re

WORD_RE_STRING = re.compile('[{L}{N}]+')  # {L} --> All letters
WORD_RE_BYTES = re.compile(b'[{L}{N}]+')  # {L} --> All letters
# {N} --> All numbers


def word_len(txt):
    """Count words in `txt`

    @type txt: str"""
    if not txt: return 0  # Safe handling of txt=None
    word_re = WORD_RE_STRING if isinstance(txt, str) else WORD_RE_BYTES
    return len(re.sub(word_re, ' ', txt).split())


def unescape_em(txt):
    """
    @param txt: text to be unescaped
    @type txt: str
    """
    return (txt
            .replace("&lt;em&gt;", "<em>")
            .replace("&lt;/em&gt;", "</em>"))


class Article(AmcatModel):
    """
    Class representing a newspaper article
    """
    __label__ = 'title'

    id = models.AutoField(primary_key=True, db_column="article_id")
    
    date = models.DateTimeField()
    title = models.TextField()

    url = models.TextField(null=True, blank=True, max_length=750)
    externalid = models.TextField(blank=True, null=True)
    hash = models.BinaryField(unique=True, max_length=64)

    #sets = models.ManyToManyField("amcat.Set", db_table="sets_articles")

    text = models.TextField()

    parent_hash = models.BinaryField(null=True, blank=True, max_length=64)

    project = models.ForeignKey("amcat.Project", db_index=True, related_name="articles")

    properties = JSONField(null=True, blank=True)
    
    def __init__(self, *args, **kwargs):
        super(Article, self).__init__(*args, **kwargs)
        self._highlighted = False

    class Meta():
        db_table = 'articles'
        app_label = 'amcat'

    @property
    def hexhash(self):
        """Returns a hex string representing this article's hash"""
        if self.hash:
            return binascii.hexlify(self.hash).decode("ascii")
        
    def highlight(self, query, escape=True, keep_em=True):
        """
        Highlight headline and text property by inserting HTML tags (em). You won't be able to
        call save() after calling this method.

        @param query: elastic query used for highlighting
        @type query: str

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
        self.title = highlighted.get("title", self.title)

        if escape:
            self.text = escape_filter(self.text)
            self.title = escape_filter(self.title)

            if keep_em:
                self.text = unescape_em(self.text)
                self.title = unescape_em(self.title)

        return highlighted

    @property
    def children(self):
        """Return a sequence of all child articles (eg reactions to a post)"""
        return Article.objects.filter(parent_hash = self.hash)

    @property
    def parent(self):
        if self.parent_hash:
            try:
                return Article.objects.get(hash=self.parent_hash)
            except Article.DoesNotExist:
                pass
    
    def save(self, *args, **kwargs):
        if self._highlighted:
            raise ValueError("Cannot save a highlighted article.")

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
        return "<Article %s: %r>" % (self.id, self.title)

    @classmethod
    def exists(cls, article_ids, batch_size=500):
        """
        Filters the given articleids to remove non-existing ids
        """
        for batch in splitlist(article_ids, itemsperbatch=batch_size):
            for aid in Article.objects.filter(pk__in=batch).values_list("pk", flat=True):
                yield aid

    @classmethod
    def create_articles(cls, articles, articleset=None, articlesets=None, deduplicate=True,
                        monitor=ProgressMonitor()):
        """
        Add the given articles to the database, the index, and the given set

        Duplicates are detected and have .duplicate and .id set (and are added to sets)

        @param articles: a collection of objects with the necessary properties (.headline etc)
        @param articleset(s): articleset object(s), specify either or none
        """
        cls._create_articles(articles, deduplicate=deduplicate)
        if articlesets is None:
            articlesets = [articleset] if articleset else []
        es = amcates.ES()
        dupes, new = [], []
        for a in articles:
            if a.duplicate:
                dupes.append(a)
            else:
                a.es_dict.update({'sets': [aset.id for aset in articlesets],
                                  'id': a.id})
                new.append(a)

        if new:
            es.bulk_insert([a.es_dict for a in new], batch_size=100)
            for aset in articlesets:
                aset.add_articles(new, add_to_index=False)
        if dupes:
            for aset in articlesets:
                aset.add_articles(dupes, add_to_index=True)
            
    
    @classmethod
    def _create_articles(cls, articles, deduplicate=True):
        """Check duplicates and save the articles to db.
        Does *not* save to elastic or add to articlesets
        Assumes that if .parent is given, it has an id
        (because parent is part of hash)
        modifies all articles in place with .hash, .id, .uuid, and .duplicate (None or Article)
        """
        es = amcates.ES()
        hashes = {} # {hash: article}
        
        # Iterate over articles, mark duplicates within addendum and build hashes dictionaries
        for a in articles:
            if a.id:
                raise ValueError("Specifying explicit article ID in save not allowed")
            a.es_dict = amcates.get_article_dict(a)
            hash_bytes = binascii.unhexlify(a.es_dict['hash'])
            if a.hash and bytes(a.hash) != hash_bytes:
                raise ValueError("Incorrect hash specified")
            a.hash = hash_bytes
            a.duplicate, a.internal_duplicate = None, None # innocent until proven guilty
            if not deduplicate:
                continue
            if a.hash in hashes:
                a.internal_duplicate = hashes[a.hash]
            else:
                hashes[a.hash] = a
            
        # check dupes based on hash
        if hashes:
            results = Article.objects.filter(hash__in=hashes.keys()).only("hash")
            for orig in results:
                dupe = hashes[bytes(orig.hash)]
                dupe.duplicate = orig
                dupe.id = orig.id

        # now we can save the articles and set id
        to_insert = [a for a in articles if not (a.duplicate or a.internal_duplicate)]
        result = bulk_insert_returning_ids(to_insert)
        if to_insert:
            for a, inserted in zip(to_insert, result):
                a.id = inserted.id
            for a in articles:
                if a.internal_duplicate and not a.id:
                    a.id = a.internal_duplicate.id
        return to_insert


def _check_read_access(user, aids):
    """Raises PermissionDenied if the user does not have full read access on all given articles"""
    # get article set memberships
    from amcat.models import ArticleSet, Project

    sets = list(ArticleSet.articles.through.objects.filter(article_id__in=aids).values_list("articleset_id", "article_id"))
    setids = {setid for (setid, aid) in sets}
    
    # get project memberships
    ok_sets = set()
    project_cache = {} # pid : True / False
    def project_ok(pid):
        if pid not in project_cache:
            role = Project.objects.get(pk=pid).get_role_id(user)
            project_cache[pid] = role and (role >= ROLE_PROJECT_READER)
        return project_cache[pid]

    asets = [ArticleSet.objects.filter(pk__in=setids).values_list("pk", "project_id"),
             Project.articlesets.through.objects.filter(articleset_id__in=setids)
             .values_list("articleset_id", "project_id")]
    
    for aset in asets:
        for sid, pid in aset:
            if project_ok(pid):
                ok_sets.add(sid)


    ok_articles = set()
    for sid, aid in sets:
        if sid in ok_sets:
            ok_articles.add(aid)
    aids = {aid for (setid, aid) in sets}
    if aids - ok_articles:
        logging.info("Permission denied for {user}, articles {}".format(aids - ok_articles, **locals()))
        raise PermissionDenied("User does not have full read access on (some) of the selected articles")
    
    
    
