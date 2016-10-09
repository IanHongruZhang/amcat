import logging
import os

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.core.urlresolvers import reverse
from django.shortcuts import redirect
from django.views.generic import ListView

from amcat.models import Plugin
from amcat.tools.wizard import Wizard, WizardStepForm, WizardFormStepView
from api.rest.resources import PluginResource
from navigator.views.articleset_views import ArticleSetListView, UPLOAD_PLUGIN_TYPE
from navigator.views.datatableview import DatatableMixin
from navigator.views.projectview import HierarchicalViewMixin, ProjectViewMixin, BreadCrumbMixin, BaseMixin
from navigator.views.scriptview import ScriptHandler, get_temporary_file_dict

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

    @classmethod
    def serialise_arguments(cls, arguments):
        arguments = super().serialise_arguments(arguments)
        arguments = cls.serialize_files(arguments)
        return arguments


class UploadWizard(Wizard):
    inner_form = None
    def __init__(self, session, step, **kwargs):
        super().__init__(session, step, **kwargs)



def get_upload_wizard(script_class):

    form = script_class.options_form
    try:
        return form.as_wizard_form()
    except AttributeError:
        pass

    step_form = type("StepForm", (WizardStepForm, form), {})

    class PluginWizard(UploadWizard):
        inner_form = form
        form_list = (step_form,)
    return PluginWizard

class ArticleSetUploadView(BaseMixin, WizardFormStepView):
    parent = ArticleSetUploadListView
    wizard_class = UploadWizard
    model = Plugin
    view_name = "articleset-upload"
    template_name = "project/articleset_upload.html"

    def dispatch(self, request, *args, **kwargs):
        plugin = Plugin.objects.get(pk=self.kwargs['plugin'])
        self.script_class = plugin.get_class()
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
