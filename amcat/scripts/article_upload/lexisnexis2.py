import datetime
import re
import sys
from contextlib import contextmanager
from logging import getLogger

import pyparsing as pp
from django.contrib.postgres.forms import JSONField

from amcat.scripts.article_upload import fileupload
from amcat.scripts.article_upload.csv_import import REQUIRED
from amcat.scripts.article_upload.upload import ARTICLE_FIELDS, UploadScript, ParseError
from amcat.scripts.article_upload.upload_formtools import FileInfo, get_fieldmap_form_set, FieldMapMixin
from amcat.tools.toolkit import read_date
from amcat.tools.wizard import WizardStepFormMixin
from navigator.views.articleset_upload_views import UploadWizard

log = getLogger(__name__)
QUARTERS = dict(spring=3,
                summer=6,
                fall=9,
                winter=12,
                )

# number of articles to be read for file info
N_FILE_INFO_ARTS = 5

DOC_NR = re.compile(r"(FOCUS - *)?\d* (of|OF) \d* DOCUMENTS?")


@contextmanager
def unicode_white():
    "Contextmanager that sets the default whitespace to all unicode characters for which .isspace() returns True"
    default_white = pp.ParserElement.DEFAULT_WHITE_CHARS
    white = default_white
    white += ''.join(chr(c) for c in range(sys.maxunicode)
                     if chr(c).isspace())

    white += "\u00a0\ufeff"  # nbsp, zwnbsp
    pp.ParserElement.setDefaultWhitespaceChars(white)
    yield
    pp.ParserElement.setDefaultWhitespaceChars(default_white)


with unicode_white():
    blank_line = pp.White("\n", 2).suppress().setName("blank line")
    text_line = pp.Regex(".+").setName("line of text")
    paragraph = pp.Optional(blank_line) + pp.OneOrMore(
        ~blank_line + pp.Optional(pp.White("\n", max=1)) + text_line) + pp.FollowedBy(blank_line | pp.StringEnd())

    doc_nr = (pp.Regex(DOC_NR)).setName("doc_nr")
    article_meta_key = pp.Regex("^[^0-9a-z: ]+", re.MULTILINE).setName("metadata line") + ~pp.White("\n") + pp.Suppress(
        ":")


    def header():
        header_meta_key = (~doc_nr + pp.Regex("^[\w -]+", re.MULTILINE) + pp.Suppress(":"))
        header_meta_value = pp.OneOrMore(~doc_nr + ~header_meta_key + pp.Regex('.+')).setParseAction(lambda x: tuple(x))
        header_meta = (header_meta_key + header_meta_value).setParseAction(lambda x: tuple(x))

        header = pp.ZeroOrMore(header_meta | (~doc_nr + pp.Regex(".+")).suppress()).setParseAction(
            lambda x: dict(list(x)))

        return header.setResultsName("file_header")


    skip_names = ("PCM Uitgevers B.V.", "De Persgroep Nederland BV")


    def action_unpack_source_name(s, loc, toks):
        source_lines = toks.copy()
        source_lines[0] = re.sub("Copyright (© )?\d{4} ", "", source_lines[0])
        if source_lines[0] in skip_names:
            return source_lines[2]
        else:
            return source_lines[0]


    def action_parse_date(s, loc, toks):
        result = read_date(toks[0], lax=True)
        if result is None:
            raise pp.ParseException(toks[0], loc)
        return result


    def action_flatten_article(s, loc, toks):
        data = dict(((k.lower(), v[0] if len(v) > 0 else None)
                     for k, v in list(toks.pop('meta_header', [])) + list(toks.pop('meta_footer', []))),
                    text=toks.get('text'),
                    medium=toks.get('source'),
                    date=toks.pop('date', None),
                    url=toks.pop('url', None)
                    )
        if 'length' not in data:
            data['length'] = toks.get('length')
        if 'date' is None and 'load-date' in data:
            data['date'] = read_date(data['load-date'], lax=True)

        return data


    def date():
        season_line = (
            pp.MatchFirst([pp.CaselessLiteral(k) for k in QUARTERS.keys()]) + pp.Suppress(",") + pp.Word(pp.nums,
                                                                                                         exact=4)) \
            .setParseAction(lambda m_y: datetime.date(int(m_y[1]), int(QUARTERS[m_y[0].lower()]), 1),
                            callDuringTry=True)

        date_line = pp.Regex("(?!http://).*\d{4}.*").copy() \
            .setName("date") \
            .setParseAction(lambda x, loc, toks: action_parse_date(x, loc, toks), callDuringTry=True)

        return (season_line | date_line) \
            .setName("date or quarter") \
            .setResultsName("date")


    def article_header():
        """parses the bit from "# OF # DOCUMENTS" to (but excluding) the title"""

        after_date = pp.ZeroOrMore(pp.White(" \t").suppress() + pp.Regex(".+")).setResultsName("after_date")
        title = (~pp.White(" \t") + ~article_meta_key + pp.Regex(r"^([^\s].+\n)+\n", re.MULTILINE)) \
            .setResultsName("title") \
            .setName("title")

        source = pp.OneOrMore(pp.FollowedBy(pp.Regex(".+")) + ~date() + pp.Regex(".+")) \
            .setResultsName("source") \
            .setParseAction(action_unpack_source_name)

        url = pp.Optional((pp.FollowedBy(pp.Regex("https?://")) + paragraph).setResultsName('url'))

        header = ((doc_nr + pp.White(" \t") + ~title + source + title + url + date() + after_date)
                  | (doc_nr + source + url + pp.Optional(date() + after_date) + pp.Optional(title)))

        return header


    def article_body():
        """parses the metadata tags, and the main text"""

        # parses text paragraphs starting with a seemingly valid meta tag, eg. "DOBBS:"
        non_meta_tag = pp.OneOrMore(pp.Optional(pp.White()) + pp.Regex("[^0-9a-z: ]+:") + paragraph) + pp.FollowedBy(
            ~article_meta_key + ~pp.CaselessLiteral("copyright") + ~pp.StringEnd() + paragraph)

        text_paragraph = (~article_meta_key + paragraph) | non_meta_tag
        text = pp.ZeroOrMore(text_paragraph + pp.White()) \
                   .setResultsName("text") \
                   .setParseAction(lambda x: "".join(x)) + pp.FollowedBy(meta_block_footer())

        return pp.Optional(~article_meta_key + text_line) + meta_block_header() + pp.Optional(
            pp.pyparsing_common.integer.setResultsName("length") + "words") + text + meta_block_footer()


    def article():
        copyright_note = pp.ZeroOrMore(~article_meta_key + pp.Regex(".+")).setResultsName("copyright_note")
        article = article_header() + article_body() + pp.Optional(copyright_note) + pp.StringEnd().setName(
            "end of article")
        article.setParseAction(action_flatten_article)
        return pp.Group(article).setResultsName("article")


    def meta_block_header():
        meta_value = pp.Optional(~blank_line + paragraph.setResultsName("meta_value"))
        meta_line = (article_meta_key + meta_value).setParseAction(lambda x: (x[0], tuple(x[1:])))
        return pp.Group(pp.ZeroOrMore(meta_line)).setResultsName("meta_header")


    def meta_block_footer():
        meta_value = pp.Optional(~article_meta_key + paragraph.setResultsName("meta_value"))
        meta_line = (
            article_meta_key + meta_value + ~(~article_meta_key + ~pp.Literal("Copyright") + paragraph)
        ).setParseAction(
            lambda x: (x[0], tuple(x[1:]))
        )
        return pp.Group(pp.ZeroOrMore(meta_line)).setResultsName("meta_footer")


    article_fulltext = (
        doc_nr + pp.White() + pp.ZeroOrMore(~doc_nr + pp.Regex(".*") + pp.White()) + pp.FollowedBy(
            doc_nr | pp.StringEnd())) \
        .setResultsName("article_fulltext") \
        .setParseAction(lambda x: "".join(x))

    split_file_text = header() + pp.Group(pp.OneOrMore(article_fulltext)).setResultsName("articles")

header_parser = header()
article_parser = article()


def parse_error_as_dict(e, **kwargs):
    return {
        "exception": dict({
            "type": type(e).__name__,
            "msg": e.msg,
            "line": e.line,
            "loc": e.loc,
            "col": e.col,
            "lineno": e.lineno
        }, **kwargs)
    }


def parse_article(article_txt):
    try:
        return article_parser.parseString(article_txt).article[0]
    except pp.ParseException as e:
        log.warning(e)
        log.warning(parse_error_as_dict(e), article_txt.split("\n", 1)[0])
        raise ParseError(str(e))


def parse_articles(article_txts):
    for article_txt in article_txts:
        yield parse_article(article_txt)


def get_file_info(file):
    first_n_arts = []
    lines = (line for line in file)
    for i in range(N_FILE_INFO_ARTS + 1):
        for line in lines:
            first_n_arts.append(line)
            if "DOCUMENT" in line:
                break
    first_n_arts_str = "".join(first_n_arts[:-1])
    log.info("Fetching file info for file")
    header, article_txts = split_file_text.parseString(first_n_arts_str)
    log.info("{} articles in file".format(len(article_txts)))
    articles = list(parse_articles(article_txts[:10]))
    print(articles[0])

    header_keys = set(key for key, value in header.items() if value)
    article_keys = set(key for article in articles for key, value in article.items() if value)
    return FileInfo(file.name, (header_keys | article_keys))


class LexisNexisForm(UploadScript.options_form, fileupload.ZipFileUploadForm, FieldMapMixin):
    field_map = JSONField(max_length=2048,
                          help_text='Dictionary consisting of "<field>":{"column":"<column name>"} and/or "<field>":{"value":"<value>"} mappings.')

    @classmethod
    def as_wizard_form(cls):
        return LexisNexisWizardForm


class LexisNexisWizardForm(UploadWizard):
    def get_form_list(self):
        upload_form = self.get_upload_step_form()
        field_form = get_fieldmap_form_set(REQUIRED, ARTICLE_FIELDS)
        return [upload_form, field_form]

    class CSVUploadStepForm(WizardStepFormMixin, UploadScript.options_form, fileupload.ZipFileUploadForm): pass

    @classmethod
    def get_upload_step_form(cls):
        return cls.CSVUploadStepForm

    @classmethod
    def get_file_info(cls, upload_form: fileupload.CSVUploadForm):
        file = list(upload_form.get_entries())[0]
        return get_file_info(file)


class LexisNexis(UploadScript):
    """
    Script for importing files from Lexis Nexis. The files should be in plain text
    format with a 'cover page'. The script will extract the metadata (headline, source,
    date etc.) from the file automatically.
    """

    options_form = LexisNexisForm

    name = 'Lexis Nexis (wizard)'

    query_keys = ["zoektermen", "query", "terms"]

    def split_file(self, file):
        lines = []
        i = 0
        for line in file:
            if DOC_NR.search(line):
                text = "\n".join(lines)
                lines = []
                if i == 0:
                    self.file_header = header_parser.parseString(text)
                else:
                    yield text
                i += 1
            lines.append(line)

    def get_provenance(self, file, articles):
        provenance = super(LexisNexis, self).get_provenance(file, articles)

        query = None
        for key in self.query_keys:
            if key in self.file_header:
                query = self.file_header[key]
                continue

        if query is None:
            return provenance

        return "{provenance}; LexisNexis query: {query!r}".format(**locals())

    def parse_document(self, text):
        fields = parse_article(text)
        if fields is None:
            return

        yield dict(fields)
