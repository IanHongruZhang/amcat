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
Parser to generate a elastic DSL from a lucene-style query string

Decided to roll my own parser since elastic does not support complex phrases
Also paves the way for more customization, i.e. allowing Lexis style queries
"""

from __future__ import unicode_literals, print_function, absolute_import
from pyparsing import ParseResults
import itertools

class ParseError(ValueError):
    pass

class FieldTerm(object):
    def __init__(self, field):
        self.field = field
    @property
    def qfield(self):
        return self.field or "_all"
    def __str__(self):
        return unicode(self).encode('utf-8')
        

class BaseTerm(FieldTerm):
    def __init__(self, text, field):
        super(BaseTerm, self).__init__(field=field)
        self.text = text.replace("!", "*")

class Term(BaseTerm):
    def __unicode__(self):
        return "{self.qfield}::{self.text}".format(**locals())
    def __str__(self):
        return unicode(self).encode('utf-8')
    def get_dsl(self):
        qtype = "wildcard" if '*' in self.text else "term"
        return {qtype : {self.qfield : self.text}}

class Quote(BaseTerm):
    def __unicode__(self):
        return u'{self.qfield}::QUOTE[{self.text}]'.format(**locals())
    def __str__(self):
        return unicode(self).encode('utf-8')
    def get_dsl(self):
        if self.text.endswith("*"):
            return {"match_phrase_prefix" : {self.qfield : self.text[:-1]}}
        else:
            return {"match_phrase" : {self.qfield : self.text}}

class Boolean(object):
    def __init__(self, operator, terms, implicit=False):
        self.operator = operator
        self.terms = terms
        self.implicit = implicit
                
    def __unicode__(self):
        terms = " ".join(unicode(t) for t in self.terms)
        return '{self.operator}[{terms}]'.format(**locals())
    def __str__(self):
        return unicode(self).encode('utf-8')
    def get_dsl(self):
        if self.operator == "NOT":
            # in lucene, NOT is binary rather than unary, so
            # x NOT y means x AND (NOT y) or +x -y.
            # We interpret x NOT y NOT z  as x AND (NOT y) AND (NOT z) or +x -y -z
            return {"bool" : {"must" : [self.terms[0].get_dsl()],
                              "must_not" : [t.get_dsl() for t in self.terms[1:]]}}
        else:
            op = dict(OR="should", AND="must", NOT="must_not")[self.operator]
            return {"bool" : {op : [term.get_dsl() for term in self.terms]}}

def _check_span(terms, field=None, allow_boolean=True):
    """
    Checks whether a span contains terms using the same field and
    consists of a single implcit list of terms or simple disjunctions
    @param field: if given and 'True', all terms must use this field
    @return: the shared field
    """
    f = field
    for term in terms:
        if isinstance(term, Term):
            fld = term.field
            term.field = None
        elif allow_boolean and isinstance(term, Boolean) and term.operator == "OR":
            fld = _check_span(term.terms, allow_boolean = False)
        else:
            raise ParseError("Proximity queries cannot contain: {term}".format(**locals()))

        if fld:
            if not f:
                f = fld
            elif f != fld:
                raise ParseError("Proximity queries should refer to a unique field, found {f!r} and {fld!r}"
                                 .format(**locals()))                
    return f
            
class Span(Boolean, FieldTerm):
    def __init__(self, terms, slop, field=None, in_order=False):
        Boolean.__init__(self, "SPAN", terms)
        FieldTerm.__init__(self, _check_span(terms, field))
        self.slop = slop
        self.in_order = in_order
        
    def __unicode__(self):
        terms = " ".join(unicode(t) for t in self.terms)
        terms = terms.replace("_all::","")
        return u'{self.qfield}::PROX/{self.slop}[{terms}]'.format(**locals())
    def __str__(self):
        return unicode(self).encode('utf-8')

    def get_dsl(self):
        # we cannot directly use disjunctions in a span query, but we can put the disjunction outside the span
        # e.g. (a OR b) w/10 c is the same as (a w/10 c) OR (b w/10 c)
        # So, the get_clause returns a list of clauses, and we take the product of those lists as a disjunction

        def get_clause(term, field):
            if isinstance(term, Term):
                if term.text.endswith("*"):
                    text = term.text[:-1]
                    return [{"span_multi":{"match":{"prefix" : { field :  { "value" : text } }}}}]
                else:
                    return [{"span_term" : {field : term.text}}]
            else:
                # term is a disjunction: return a list of clauses
                # index [0] because get_clause returns a list again, which we don't want here
                return [get_clause(t, field)[0] for t in term.terms]
        clauses = (get_clause(t, self.qfield) for t in self.terms)
        #import json; clauses = list(clauses); print("\n", json.dumps(clauses, indent=4))
        clauses = itertools.product(*clauses)
        clauses = [{"span_near" : {"slop": self.slop, "in_order" : self.in_order, "clauses" : list(c)}}
                   for c in clauses]
        if len(clauses) == 1:
            return clauses[0]
        else:
            return {"bool" : {"should" : clauses}}


def lucene_span(quote, field, slop):
    '''Create a span query from a lucene style string, i.e. "terms"~10'''
    clause = parse_to_terms(quote)
    if not (isinstance(clause, Boolean) and clause.operator == "OR" and clause.implicit):
        raise ParseError("Lucene-style proximity queries must contain a list of terms, not {clause!r}"
                         .format(**locals()))
    return Span(clause.terms, slop, field)
        
def get_term(tokens):
    if 'slop' in tokens:
        return lucene_span(tokens.quote, tokens.field, tokens.slop)
    elif 'quote' in tokens:
        # this is where it gets weird: phrase queries don't support general
        # prefixes, but span (=slop) queries do. So, make a span query
        # with slop=0 and in_order=True if a non-final wildcard is present
        if "*" in tokens.quote[:-1]:
            return Span(tokens.quote, tokens.field, 0, in_order=True)
        else:
            return Quote(tokens.quote, tokens.field)
    else:
        t = Term(tokens.term, tokens.field)
        return t

def get_boolean_or_term(tokens):
    token = tokens[0]
    if isinstance(token, (Boolean, BaseTerm)):
        return token
    else:
        op = token['operator']
        terms = [t for t in token if isinstance(t, (Boolean, BaseTerm))]
        if op[0] == 'W/':
            # create span query
            op, slop = op
            return Span(terms, slop)
        else:
            implicit = op.startswith("implicit_")
            if implicit: op = op.replace("implicit_", "")
            return Boolean(op, terms, implicit)
    

def pprint(q, indent=0):
    i = "  "*indent
    if isinstance(q, Boolean):
        print (i, "Boolean", q.operator, "[")
        for t in q.terms:
            pprint(t, indent+1)
        print (i, "]")
    else:
        print (i, type(q).__name__, q)

from amcat.tools.caching import cached

_grammar = None
def get_grammar():
    global _grammar
    if _grammar is None:
        from pyparsing import Literal, Word, QuotedString, Optional, operatorPrecedence, nums, alphas, opAssoc, ParseResults


        # literals
        AND = Literal("AND").setResultsName("operator")
        OR = Literal("OR").setResultsName("operator")
        NOT = Literal("NOT").setResultsName("operator")
        SPAN = (Literal("W/") + Word(nums)).setResultsName("operator")
        COLON = Literal(":").suppress()
        TILDE = Literal("~").suppress()
        LETTERS = u''.join(unichr(c) for c in xrange(65536) 
                           if not unichr(c).isspace() and unichr(c) not in '":()~')

        # terms
        term = Word(LETTERS)
        slop = Word(nums).setResultsName("slop")
        quote = QuotedString('"').setResultsName("quote") + Optional(TILDE + slop)
        #quote.setParseAction(Quote)

        field = Word(alphas).setResultsName("field")
        fterm = Optional(field + COLON) + (quote | term).setResultsName("term")
        fterm.setParseAction(get_term)

        # boolean combination
        boolean_expr = operatorPrecedence(fterm, [
            (AND, 2, opAssoc.LEFT),
            (NOT, 2, opAssoc.LEFT),
            (SPAN, 2, opAssoc.LEFT),
            (Optional(OR, default="implicit_OR"),  2, opAssoc.LEFT),
        ])
        boolean_expr.setParseAction(get_boolean_or_term)
        _grammar = boolean_expr
    return _grammar

def parse_to_terms(s):
    return get_grammar().parseString(s, parseAll=True)[0]
    
def parse(s):
    return parse_to_terms(s).get_dsl()


###########################################################################
#                          U N I T   T E S T S                            #
###########################################################################

from amcat.tools import amcattest

class TestQueryParser(amcattest.PolicyTestCase):
    def test_parse(self):
        q = lambda s : unicode(parse_to_terms(s))

        self.assertEqual(q('a'), '_all::a')
        self.assertEqual(q('a b'), 'OR[_all::a _all::b]')
        self.assertEqual(q('a OR b'), 'OR[_all::a _all::b]')
        self.assertEqual(q('a AND b'), 'AND[_all::a _all::b]')
        self.assertEqual(q('a NOT b'), 'NOT[_all::a _all::b]')
        self.assertEqual(q('a AND (b c)'), 'AND[_all::a OR[_all::b _all::c]]')
        self.assertEqual(q('(a AND b) c'), 'OR[AND[_all::a _all::b] _all::c]')

        # quotes
        self.assertEqual(q('"a b"'), '_all::QUOTE[a b]')
        self.assertEqual(q('"a b" c'), 'OR[_all::QUOTE[a b] _all::c]')
        
        # proximity queries
        self.assertEqual(q('a W/10 b'), '_all::PROX/10[a b]')
        self.assertEqual(q('(a b) W/10 c'), '_all::PROX/10[OR[a b] c]')
        self.assertEqual(q('(x:a b) W/10 c'), 'x::PROX/10[OR[a b] c]')
        
        # proximity queries must have unique field and can only contain wildcards and disjunctions
        self.assertRaises(ParseError, q, 'a W/10 (b OR (c OR d))')
        self.assertRaises(ParseError, q, 'x:a W/10 y:b')
        self.assertRaises(ParseError, q, 'a W/10 (b AND c)')
        self.assertRaises(ParseError, q, 'a W/10 (b W/5 c)')

        # lucene notation
        self.assertEqual(q('"a b"~5'), '_all::PROX/5[a b]')
        self.assertEqual(q('x:"a b"~5'), 'x::PROX/5[a b]')
        self.assertEqual(q('"a (b c)"~5'), '_all::PROX/5[a OR[b c]]')

        # disallow AND in lucene notation
        self.assertRaises(ParseError, q, '"a (b AND c)"~5')

    def test_dsl(self):
        q = parse

        
        self.assertEqual(q('a'), {'term' : {'_all' : 'a'}})
        self.assertEqual(q('a*'), {'wildcard' : {'_all' : 'a*'}})
        self.assertEqual(q('a!'), {'wildcard' : {'_all' : 'a*'}})

        self.assertEqual(q('a AND b'), {'bool' : {'must' : [{'term' : {'_all' : 'a'}},
                                                            {'term' : {'_all' : 'b'}},
                                                            ]}})
                                                            
        self.assertEqual(q('a W/10 b'), {"span_near" : {"clauses" : [
            {"span_term" : {"_all" : "a"}},
            {"span_term" : {"_all" : "b"}},
            ], "slop" : "10", "in_order": False}})
        self.assertEqual(q('a* W/10 b'), {"span_near" : {"clauses" : [
            {"span_multi" : {"match" : {"prefix" : {"_all" : {"value" : "a"}}}}},
            {"span_term" : {"_all" : "b"}},
            ], "slop" : "10", "in_order": False}})
        
        expected = {u'bool': {u'should': [
            {u'span_near': {u'in_order': False, u'clauses': [
                {u'span_term': {u'_all': u'a'}}, 
                {u'span_term': {u'_all': u'b'}}
            ], u'slop': u'10'}}, 
            {u'span_near': {u'in_order': False, u'clauses': [
                {u'span_term': {u'_all': u'a'}}, 
                {u'span_term': {u'_all': u'c'}}
            ], u'slop': u'10'}}
        ]}}

        self.assertEqual(q('a W/10 (b c)'), expected)
