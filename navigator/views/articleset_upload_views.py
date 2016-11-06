import logging

from django.core.files.uploadedfile import UploadedFile
from django.core.urlresolvers import reverse
from django.shortcuts import redirect
from django.views.generic import ListView

from amcat.models import Plugin, Task
from amcat.scripts.article_upload.upload import UploadWizard, get_upload_wizard, UploadCreateArticlesScript
from amcat.scripts.article_upload.upload_formtools import BaseFieldMapFormSet
from amcat.tools.wizard import WizardFormStepView
from api.rest.resources import PluginResource
from navigator.views.articleset_views import ArticleSetListView, UPLOAD_PLUGIN_TYPE
from navigator.views.datatableview import DatatableMixin
from navigator.views.projectview import HierarchicalViewMixin, ProjectViewMixin, BreadCrumbMixin, BaseMixin
from navigator.views.scriptview import ScriptHandler, get_temporary_file_dict, ScriptView

log = logging.getLogger(__name__)



class ArticleSetUploadListView(HierarchicalViewMixin, ProjectViewMixin, BreadCrumbMixin, DatatableMixin, ListView):
    parent = ArticleSetListView
    model = Plugin
    resource = PluginResource
    view_name = "articleset-upload-list"
    url_fragment = "upload"

    def filter_table(self, table):
        table = table.rowlink_reverse('navigator:articleset-upload', args=[self.project.id, '{id}'])
        return table.filter(plugin_type=UPLOAD_PLUGIN_TYPE).hide('id', 'class_name')  # , 'plugin_type')


class ArticleSetUploadScriptHandler(ScriptHandler):
    def get_redirect(self):
        aset_ids = self.task._get_raw_result()

        return reverse("navigator:articleset-upload-fieldmap", args=[self.task.project.id])

        if len(aset_ids) == 1:
            return reverse("navigator:articleset-details", args=[self.task.project.id, aset_ids[0]]), "View set"

        # Multiple articlesets
        url = reverse("navigator:articleset-multiple", args=[self.task.project.id])
        return url + "?set=" + "&set=".join(map(str, aset_ids)), "View sets"

    @classmethod
    def serialize_files(cls, arguments):
        data = {}
        for k, v in arguments['data'].items():
            if isinstance(v, UploadedFile):
                data[k] = get_temporary_file_dict(v)
        arguments['data'].update(data)
        arguments['files'].update(data)
        return arguments

    def run_task(self):
        result = super().run_task()

        if not self.task.get_class().customizable_fields:
            handler = ScriptHandler.call(UploadCreateArticlesScript,
                               user=self.task.user,
                               project=self.task.project,
                               arguments={"calling_task": self.task.id,
                                          "field_map": '"nomap"'}
                               )

        return result

    def get_script(self):
        script_cls = self.task.get_class()
        kwargs = self.get_form_kwargs()
        form = script_cls.options_form(**kwargs)
        return script_cls(form)

    @classmethod
    def serialise_arguments(cls, arguments):
        arguments = super().serialise_arguments(arguments)
        arguments = cls.serialize_files(arguments)
        return arguments


class ArticleSetUploadView(BaseMixin, WizardFormStepView):
    parent = ArticleSetUploadListView
    wizard_class = UploadWizard
    model = Plugin
    view_name = "articleset-upload"
    template_name = "project/articleset_upload.html"

    def dispatch(self, request, *args, **kwargs):
        self.plugin = Plugin.objects.get(pk=self.kwargs['plugin'])
        self.script_class = self.plugin.get_class()
        self.wizard_class = get_upload_wizard(self.script_class)
        return super().dispatch(request, *args, **kwargs)

    def complete(self):
        arguments = self.wizard.get_full_data()
        arguments['data'].update(arguments['files'])
        task = ArticleSetUploadScriptHandler.call(self.script_class,
                                                  user=self.request.user,
                                                  project=self.project,
                                                  arguments=arguments)

        return redirect(reverse("navigator:task-details", args=[self.project.id, task.task.id]))

    def get_initial(self):
        initial = super().get_initial()
        if not issubclass(self.wizard.get_form(), BaseFieldMapFormSet):
            initial['project'] = self.project.id
        return initial

    def get_context_data(self, **kwargs):
        kwargs['script_name'] = self.plugin.label
        form = self.wizard.get_form()
        if issubclass(form, BaseFieldMapFormSet):
            kwargs['is_field_mapping_form'] = True
            kwargs['field_autocomplete_data'] = form.known_properties
        ctx = super().get_context_data(**kwargs)
        return ctx

    def get_wizard_kwargs(self):
        return {
           "project": self.project
        }

class ArticleSetUploadFieldMapView(BaseMixin, ScriptView):
    parent = ArticleSetUploadListView
    script = UploadCreateArticlesScript
    model = Task
    template_name = "project/articleset_upload.html"
    url_fragment = "upload-selectfields"
    view_name = "articleset-upload-fieldmap"

