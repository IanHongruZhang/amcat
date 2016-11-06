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
Base module for article upload scripts
"""
import os
import json
import pickle
import logging
import tempfile
from typing import Iterable, List
import zipfile

import datetime
from django import forms
from django.contrib.postgres.forms import JSONField
from django.core.files import File
from django.core.files.uploadedfile import UploadedFile
from django.forms import ModelChoiceField, ModelMultipleChoiceField

from amcat.forms.widgets import BootstrapMultipleSelect
from amcat.models import Article, Project, ArticleSet, Task
from amcat.scripts import script
from amcat.scripts.article_upload.upload_formtools import BaseFieldMapFormSet
from amcat.tools.amcates import ES
from amcat.tools.wizard import Wizard, WizardStepForm

log = logging.getLogger(__name__)

ARTICLE_FIELDS = ("text", "title", "url", "date", "parent_hash")


class ParseError(Exception):
    pass


class ArticleError(ParseError):
    pass


class MissingValueError(ArticleError):
    def __init__(self, field, *args):
        super().__init__("Missing value for field: '{}'".format(field), *args)


class ParsedArticle():
    def __init__(self, fields: dict = (), field_errors: list = ()) -> None:
        self.fields = dict(fields)
        self.errors = (field_errors)


class ParserForm(forms.Form):
    @classmethod
    def as_wizard_steps(self) -> Iterable[forms.Form]:
        return []


class UploadWizardStepForm(WizardStepForm):
    def __init__(self, *args, **kwargs):
        self._project = kwargs.pop('project', kwargs.get('data', {}).get('project'))
        super().__init__(*args, **kwargs)
        if not isinstance(self._project, Project):
            self._project = Project.objects.get(id=self._project)
        if self._project is None:
            raise TypeError("Expected 'project' in either kwargs or form data")


class ZipFileUploadForm(UploadWizardStepForm):
    file = forms.FileField(
        help_text="You can also upload a zip file containing the desired files. Uploading very large files can take a long time. If you encounter timeout problems, consider uploading smaller files")
    articlesets = forms.ModelMultipleChoiceField(queryset=ArticleSet.objects.all())
    project = forms.ModelChoiceField(queryset=Project.objects.all(), widget=forms.HiddenInput)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].initial = self._project
        self.fields["articlesets"] = forms.ModelMultipleChoiceField(
            queryset=self._project.all_articlesets(True),
            widget=BootstrapMultipleSelect
        )

    def is_zip(self, file: File):
        """
        Tests if a file is a zipfile.
        @param file: any open binary file object
        @return: True if file is a zip file, otherwise false
        """
        p = file.tell()
        file.seek(0)
        iszip = file.read(4) == b"PK\3\4"
        file.seek(p)
        return iszip

    def get_uploaded_texts(self):
        """
        Returns a list of DecodedFile objects representing the zipped files,
        or just a [DecodedFile] if the uploaded file was not a .zip file.
        """
        f = self.files['file']
        if self.is_zip(f):
            return [f for f in self.iter_zip_file_contents(f)]
        else:
            return [f]

    def get_entries(self) -> Iterable[File]:
        return self.get_uploaded_texts()

    def iter_zip_file_contents(self, zip_file):
        """
        Generator that unpacks and yields the zip entries as File objects. Skips folders.
        @param zip_file: The zip file to iterate over.
        """
        with zipfile.ZipFile(zip_file) as zf:
            for zinfo in zf.infolist():
                if zinfo.filename.endswith("/"):
                    continue
                with zf.open(zinfo, 'rb') as f:
                    yield File(f, zinfo.filename)



class UploadParser:
    options_form_class = ParserForm
    def __init__(self, options_form: ParserForm):
        self.options_form = options_form
        self.options = options_form.cleaned_data

    def parse_file(self, file: File) -> Iterable[ParsedArticle]:
        pass

    def get_options_form_class(self):
        return self.options_form_class

    def get_provenance(self) -> str:
        return ""


class UploadWizard(Wizard):
    upload_form = ZipFileUploadForm

    def __init__(self, session, step, project, **kwargs):
        super().__init__(session, step, **kwargs)
        self.project = project

    @classmethod
    def get_file_info(cls, upload_form):
        return None

    def get_form_list(self):
        return [self.upload_form]

    def get_form_kwargs(self, step=None):
        es = ES()
        if step is None:
            step = self.step
        kwargs = super().get_form_kwargs(step)
        kwargs['project'] = self.project
        if step != 0:
            try:
                kwargs0 = self.get_form_kwargs(0)
            except KeyError:
                pass
            else:
                form0 = self.get_form(0)(**kwargs0)
                form0.load_files()
                form0.full_clean()

                file_info = self.get_file_info(form0)

                if file_info is not None:
                    kwargs['file_info'] = file_info

                if 'articlesets' in form0:
                    kwargs['set_property_names'] = es.get_used_properties(form0['articlesets'])

        return kwargs


    def get_full_data(self):
        data = {}
        files = {}
        for step in self.steps:
            form = self.get_form(step)
            if issubclass(form, BaseFieldMapFormSet):
                f = form(data=self.get_step_state(step)['data'])
                f.full_clean()
                data.update(f.cleaned_data)
                data['field_map'] = json.dumps(data['field_map'])
                continue
            data.update(self.get_step_state(step)['data'])
            files.update(self.get_step_state(step)['files'])
        return {"data": data, "files": files}


class UploadScriptMeta(type):
    def __new__(cls, name, bases, attrs):
        newcls = super(UploadScriptMeta, cls).__new__(cls, name, bases, attrs)
        try:
            newcls.options_form = type("AutoOptionsForm", (ZipFileUploadForm, newcls.parser_class.options_form_class), {})
        except AttributeError:
            pass
        return newcls

class UploadScript(script.Script, metaclass=UploadScriptMeta):
    parser_class = None
    customizable_fields = True

    def get_files(self) -> List[File]:
        form = ZipFileUploadForm(data=self.bound_form.data, files=self.bound_form.files)
        files = list(form.get_entries())
        return files

    def get_parser(self) -> UploadParser:
        print(self.parser_class.options_form_class)
        opts = self.parser_class.options_form_class(data=self.bound_form.data, files=self.bound_form.files)
        opts.full_clean()
        if not opts.is_valid():
            raise Exception(opts.errors)
        return self.parser_class(opts)

    def run(self):
        parsed_articles = []
        parser = self.get_parser()
        files = self.get_files()
        for i, file in enumerate(files):
            self.progress_monitor.update(10, "Parsing file {} / {} ({})".format(i, len(files), file.name))
            p = parser.parse_file(file)
            parsed_articles.extend(p)

        self.progress_monitor.update(10, "Collecting article fields")
        valid = True
        fields = {}
        for i, a in enumerate(parsed_articles):
            if a.errors:
                valid = False
            print(a.fields)
            for field in a.fields.keys():
                complete = True
                if field not in fields:
                    complete = (i == 0)
                fields[field] = {"field_name": field, "complete": complete}
            for fieldname, field in fields.items():
                if fieldname not in a.fields:
                    field['complete'] = False

        if not valid:
            raise Exception([e for a in parsed_articles for e in a.errors])

        self.progress_monitor.update(10, "Serializing results")
        with tempfile.NamedTemporaryFile(suffix=".parseresult", delete=False) as f:
            pickle.dump([{"fields": a.fields, "errors": a.errors} for a in parsed_articles], f)
        return f.name, list(fields.values())


class UploadCreateArticlesScript(script.Script):
    class options_form:
        field_map = JSONField(max_length=2048,
                              help_text='Dictionary consisting of "<field>":{"column":"<column name>"} and/or "<field>":{"value":"<value>"} mappings.')
        calling_task = ModelChoiceField(queryset=Task.objects.all())

    def run(self):
        calling_task = self.options["calling_task"]
        article_dicts, fields = self.get_task_result()

        project = calling_task.arguments['project']
        articlesets = calling_task.arguments['articlesets']
        file = calling_task.arguments['file']

        mapped_articles = []
        if self.options["fieldmap"] != "nomap":
            for a in article_dicts:
                mapped_art = {}
                for k, v in self.options["fieldmap"].items():
                    if 'column' in v and v['column'] in a:
                        mapped_art[k] = a[v['column']]
                    elif ('column' in v and v['column'] not in a and 'value' in v
                          or 'column' not in v and 'value' in v):
                        mapped_art[k] = v['value']
                    else:
                        raise MissingValueError("{} missing from {}".format(v, a))
                mapped_articles.append(mapped_art)
        else:
            mapped_articles = article_dicts

        articles = list(self.get_articles(mapped_articles))

        Article.create_articles(articles, articlesets=articlesets,
                                monitor=self.progress_monitor.submonitor(40))

        for art in articles:
            _set_project(art, project)

        self.progress_monitor.update(10, "Uploaded {n} articles, post-processing".format(n=len(articles)))

        for aset in articlesets:
            new_provenance = self.get_provenance(file, articles)
            aset.provenance = ("%s\n%s" % (aset.provenance or "", new_provenance)).strip()
            aset.save()

        if getattr(self, 'task', None):
            self.task.log_usage("articles", "upload", n=len(articles))

        self.progress_monitor.update(10, "Done! Uploaded articles".format(n=len(articles)))
        return [a.id for a in articlesets]

    def get_task_result(self):
        calling_task = self.options["calling_task"]
        tf, fields = calling_task.get_async_result()
        if not tf.endswith(".parseresult"):
            raise Exception("Unexpected calling task result: {}".format(tf))
        with open(tf, "rb") as f:
            calling_task_articles = pickle.load(f)
        os.remove(tf)
        return calling_task_articles, fields

    def get_articles(self, article_dicts) -> Iterable[Article]:
        for adict in article_dicts:
            fields = {k: v for k, v in adict.items() if k in ARTICLE_FIELDS}
            properties = {k: v for k, v in adict.items() if k not in ARTICLE_FIELDS}
            return Article(properties=properties, **fields)

    def get_provenance(self, file, articles):
        n = len(articles)
        filename = file and file.name
        timestamp = str(datetime.datetime.now())[:16]
        return ("[{timestamp}] Uploaded {n} articles from file {filename!r} "
                "using {self.__class__.__name__}".format(**locals()))


def _set_project(art, project):
    try:
        if getattr(art, "project", None) is not None: return
    except Project.DoesNotExist:
        pass  # django throws DNE on x.y if y is not set and not nullable
    art.project = project


def _create_wizard(base_wizard, forms):
    class Wizard(base_wizard):
        def get_form_list(self):
            return super().get_form_list() + forms
    return Wizard


def get_upload_wizard(script_class):
    form = script_class.parser_class.options_form_class
    if hasattr(form, 'as_wizard'):
        return form.as_wizard()
    else:
        return _create_wizard(UploadWizard, [script_class.parser_class.options_form_class])



REQUIRED = tuple(
    field.name for field in Article._meta.get_fields() if field.name in ARTICLE_FIELDS and not field.blank)
