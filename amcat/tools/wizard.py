import datetime
import uuid
import os
from collections import OrderedDict
from tempfile import NamedTemporaryFile
from typing import Iterable

from django.contrib.sessions.models import Session
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.forms import Form
from django.http import HttpResponseRedirect
from django.utils.http import urlencode
from django.views.generic import FormView

FILE_SUFFIX = ".wizupload"

class FormComplete(Exception):
    pass


class Wizard:
    form_list = None
    fields = None
    step = 1
    expiry = datetime.timedelta(1)
    def __init__(self, session, step, **kwargs):
        self.id = kwargs.pop("id", str(uuid.uuid4()))
        self.session = session
        self.step = step
        self.forms = OrderedDict(enumerate(self.get_form_list(), 0))
        self.state = self.get_or_create_state(session, self.id)


    def get_form_list(self):
        if self.form_list is None and self.fields:
            self.form_list = list(wizard_steps_factory(self.fields))

        return self.form_list

    def form_submitted(self, form):
        """
        Is called when a form is submitted.
        @param form: A complete and validated WizardStepForm.
        @return: The next step, if applicable. Otherwise None
        """
        assert form.is_valid()
        self.set_step_state(form.state)

        self.step = self.next_step()
        return self.step

    def get_form(self, step=None):
        if step  is None:
            step = self.step
        return self.forms[step]


    def next_step(self):
        next = self.step + 1
        if next not in self.forms:
            return None
        return next

    def get_cleaned_data(self):
        data = {}
        for step in self.steps:
            form_kwargs = self.get_step_state(step)
            form = self.get_form(step)(**form_kwargs)
            form.full_clean()
            data.update(form.cleaned_data)
            data.update(form_kwargs['files'])
        return {"data": data, "files": form_kwargs['files']}

    def get_full_data(self):
        data = {}
        files = {}
        for step in self.steps:
            data.update(self.get_step_state(step)['data'])
            files.update(self.get_step_state(step)['files'])
        return {"data": data, "files": files}


    def get_step_state(self, step: int = None):
        if step is None:
            step = self.step
        return self.state[step]

    def set_step_state(self, step_state, step: int = None):
        if step is None:
            step = self.step
        self.state[step] = step_state
        self.session.modified = True

    def get_form_kwargs(self, step: int = None):
        if step is None:
            step = self.step
        kwargs = {}
        try:
            stepstate = self.get_step_state(step)
        except KeyError:
            pass
        else:
            kwargs.update({
                "data": stepstate["data"],
                "files": stepstate["files"]
            })
        return kwargs

    @classmethod
    def get_or_create_state(cls, session: Session, id: str):
        if 'wizard_states' not in session:
            session['wizard_states'] = {}
        if id not in session['wizard_states']:
            session['wizard_states'][id] = {"_created": datetime.datetime.now().isoformat()}
            session.modified = True
            for k, v in list(session['wizard_states'].items()):
                if dict(v).get('_created', '0') < (datetime.datetime.now() - cls.expiry).isoformat():
                    session['wizard_states'].pop(k)

        return session['wizard_states'][id]

    @classmethod
    def delete_state(cls, session, id):
        session['wizard_states'].pop(id)
        session.modified = True



    def delete(self):
        self.delete_state(self.session, self.id)

    @classmethod
    def id_exists(cls, session: Session, id: str):
        return id in session['wizard_states']

    @property
    def steps(self):
        return list(self.forms.keys())


    def __iter__(self):
        return self.forms.items()



class WizardFile(UploadedFile):

    def store(self):
        with NamedTemporaryFile(suffix=FILE_SUFFIX, delete=False, dir=settings.FILE_UPLOAD_TEMP_DIR) as tf:
            for chunk in self.chunks():
                tf.write(chunk)
        return {
            "path": tf.name,
            "name": self.name,
            "filename": tf.name,
            "content_type": self.content_type,
            "size": self.file.size,
            "charset": self.charset,
        }

    @classmethod
    def from_dict(cls, storage_dict):
        storage_dict = storage_dict.copy()
        path = storage_dict.pop("filename")
        storage_dict.pop("path")
        storage_dict['file'] = open(path, mode="rb")
        return cls(**storage_dict)


class WizardStepFormMixin:

    def load_files(self):
        for k in self.files.keys():
            if isinstance(self.files[k], dict):
                self.files[k] = self.load_file(self.files[k])


    @property
    def state(self):
        return {"data": self.state_data, "files": self.state_files}

    @property
    def state_data(self):
        state_data = self.data.copy()
        return state_data

    @property
    def state_files(self):
        if not hasattr(self, "_files") or self._files is None:
            files = self.files.copy()
            self._files =  {k: WizardFile(f).store() for k, f in files.items()}
        return self._files

    def store_file(self, file):
        file = WizardFile(file.file, file.name, file.content_type, file.size, file.charset)
        return file.store()

    def load_file(self, file_dict):
        return WizardFile.from_dict(file_dict)

class WizardStepForm(WizardStepFormMixin, Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.load_files()
        self._files = None


class WizardFormStepView(FormView):
    template_name = "project/form_base.html"
    wizard_class = None
    wizard = None
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def get_form_kwargs(self):
        kwargs = {}
        kwargs.update(self.wizard.get_form_kwargs())
        kwargs.update(super().get_form_kwargs())
        return kwargs

    def get(self, request, *args, **kwargs):
        wizard = self.get_wizard(request)
        self.form_class = wizard.get_form()
        self.wizard = wizard
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        wizard = self.get_wizard(request)
        self.wizard = wizard
        self.form_class = wizard.get_form()
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        next_step = self.wizard.form_submitted(form)
        if next_step:
            self.form_class = self.wizard.get_form(next_step)
            return HttpResponseRedirect(self.get_step_url(next_step))
        try:
            return self.complete()
        finally:
            self.wizard.delete()

    def get_step_url(self, step):
        if step is None:
            return ""
        url = self.request.get_full_path().split("?", 1)[0]
        query = dict(wizard_id=self.wizard.id, wizard_step=int(step))
        return "{}?{}".format(url, urlencode(query))

    def complete(self):
        return None

    def get_wizard(self, request):
        id, step = self.get_wizard_data(request)
        if id and self.wizard_class.id_exists(request.session, id):
            wizard = self.wizard_class(request.session, step=step, id=id)
        else:
            wizard = self.wizard_class(request.session, step=0)
        return wizard

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs,
                                        wizard=self.wizard,
                                        next_step_url=self.get_step_url(self.wizard.step)
                                        )

    def get_wizard_data(self, request):
        wizard_id = request.GET.get("wizard_id")
        if not wizard_id:
            return None, None
        step = 0
        try:
            step = int(request.GET["wizard_step"])
        except (ValueError, KeyError):
            pass

        return wizard_id, step

def wizard_steps_factory(field_dicts: Iterable[dict], CBase:type=WizardStepForm) -> Iterable[type]:
    for field_dict in field_dicts:
        cls = type("_WizardStepForm", CBase)
        cls.base_fields.update(field_dict)
        yield cls
