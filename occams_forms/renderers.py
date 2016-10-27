"""
Renders HTML versions datastore forms.

Note that in order for these to work completely, clientside javascript
and associated plugins must be enabled.
"""

from __future__ import division
import collections
from datetime import date, datetime
import os
from itertools import groupby
import uuid
import cgi
from decimal import ROUND_UP

import magic
from pyramid.renderers import render
import six
import sqlalchemy as sa
import wtforms
import wtforms.fields.html5
import wtforms.widgets.html5
import wtforms.ext.dateutil.fields
from wtforms_components import DateRange

from occams_datastore import models as datastore

from . import _, log
from .fields import FileField


class states:
    PENDING_ENTRY = u'pending-entry'
    PENDING_REVIEW = u'pending-review'
    PENDING_CORRECTION = u'pending-correction'
    COMPLETE = u'complete'


TRANSITIONS = {
    states.PENDING_ENTRY: [states.PENDING_REVIEW],
    states.PENDING_REVIEW: [states.PENDING_CORRECTION, states.COMPLETE],
    states.PENDING_CORRECTION: [states.PENDING_REVIEW],
    states.COMPLETE: []
}


class modes:
    AUTO, AVAILABLE, ALL = range(3)


def version2json(schema):
    """
    Returns a single schema json record
    (this is how it's stored in the database)
    """

    return {
        'id': schema.id,
        'name': schema.name,
        'title': schema.title,
        'publish_date': schema.publish_date.isoformat()
    }


def form2json(schemata):
    """
    Returns a representation of schemata grouped by versions.

    This is useful for representing schemata grouped by their version.

    The final dict contains the following values:
        ``schema`` -- a dict containing:
            ``name`` -- the schema name
            ``title`` -- the schema's most recent human title
        ``versions`` -- a list containining each version (see ``version2json``)

    This method accepts a single value (in which it will be transformted into
    a schema/versions pair, or a list which will be regrouped
    into schema/versions pairs
    """

    def by_name(schema):
        return schema.name

    def by_version(schema):
        return schema.publish_date

    def make_json(groups):
        groups = sorted(groups, key=by_version)

        return {
            'schema': {
                'name': groups[0].name,
                'title': groups[-1].title
            },
            'versions': list(map(version2json, groups))
        }

    if isinstance(schemata, collections.Iterable):
        schemata = sorted(schemata, key=by_name)
        return [make_json(g) for k, g in groupby(schemata, by_name)]
    elif isinstance(schemata, datastore.Schema):
        return make_json([schemata])


def render_field(field, **kw):
    """
    Renders a wtform field with HTML5 attributes applied
    """
    if field.flags.required:
        kw['required'] = True
    # check validators Length, NumberRange or DateRange
    for validator in field.validators:
        if isinstance(validator, wtforms.validators.Length):
            # minlength is not supported by browsers
            if validator.min > -1:
                kw['minlength'] = validator.min
            # set maxlenght only, minlength is not supported by browsers
            if validator.max > -1:
                kw['maxlength'] = validator.max
        if isinstance(validator, wtforms.validators.NumberRange):
            if validator.min > -1:
                kw['min'] = validator.min
            if validator.max > -1:
                kw['max'] = validator.min
        if isinstance(validator, wtforms.validators.Regexp):
            kw['pattern'] = validator.regex.pattern
    return field(**kw)


def strip_whitespace(value):
    """
    Strips a string of whitespace.
    Will result to None if the string is empty.
    """
    if value is not None:
        return value.strip() or None


def make_field(attribute):
    """
    Converts an attribute to a WTForm field
    """

    kw = {
        'label': attribute.title,
        'description': attribute.description,
        'filters': [],
        'validators': []
    }

    if attribute.type == 'section':

        class Section(wtforms.Form):
            pass

        for subattribute in attribute.itertraverse():
            setattr(Section, subattribute.name, make_field(subattribute))

        return wtforms.FormField(
            Section, label=attribute.title, description=attribute.description)

    elif attribute.type == 'number':
        if attribute.decimal_places == 0:
            field_class = wtforms.fields.html5.IntegerField
        else:
            field_class = wtforms.fields.html5.DecimalField
            if attribute.decimal_places > 0:
                ndigits = abs(attribute.decimal_places)
                step = round(1 / pow(10, ndigits), ndigits)
                kw['widget'] = wtforms.widgets.html5.NumberInput(step)
                kw['places'] = attribute.decimal_places
                kw['rounding'] = ROUND_UP

    elif attribute.type == 'string':
        field_class = wtforms.StringField
        kw['filters'].append(strip_whitespace)

        if attribute.widget == 'phone':
            kw['widget'] = wtforms.widgets.html5.TelInput()
        elif attribute.widget == 'email':
            kw['widget'] = wtforms.widgets.html5.EmailInput()

    elif attribute.type == 'text':
        field_class = wtforms.TextAreaField
        kw['filters'].append(strip_whitespace)

    elif attribute.type == 'date':
        field_class = wtforms.ext.dateutil.fields.DateField
        kw['widget'] = wtforms.widgets.html5.DateInput()
        kw['validators'].append(DateRange(min=date(1899, 12, 31)))

    elif attribute.type == 'datetime':
        field_class = wtforms.ext.dateutil.fields.DateTimeField
        kw['widget'] = wtforms.widgets.html5.DateTimeInput()
        kw['validators'].append(DateRange(min=datetime(1899, 12, 31)))

    elif attribute.type == 'choice':
        choices = list(attribute.iterchoices())

        if len(choices) > 10:
            attribute_widget = 'select'
            label = u'{choice.title} - [ {choice.name} ]'
        else:
            attribute_widget = None
            label = u'{choice.title}'

        kw['choices'] = [(c.name, label.format(choice=c)) for c in choices]

        # Displays a blank option for long single select to force user to
        # select a value
        if attribute_widget == 'select' and not attribute.is_collection:
            kw['choices'].insert(0, (u'', u''))

        # If true, parse as string, else return none
        kw['coerce'] = lambda v: six.binary_type(v) if v else None

        if attribute.is_collection:
            field_class = wtforms.SelectMultipleField
            if attribute_widget == 'select':
                kw['widget'] = wtforms.widgets.Select(multiple=True)
            else:
                kw['widget'] = wtforms.widgets.ListWidget(prefix_label=False)
                kw['option_widget'] = wtforms.widgets.CheckboxInput()
        else:
            field_class = wtforms.SelectField
            if attribute_widget == 'select':
                kw['widget'] = wtforms.widgets.Select()
            else:
                kw['widget'] = wtforms.widgets.ListWidget(prefix_label=False)
                kw['option_widget'] = wtforms.widgets.RadioInput()

    elif attribute.type == 'blob':
        field_class = FileField

    else:
        raise Exception(u'Unknown type: %s' % attribute.type)

    if attribute.is_required:
        kw['validators'].append(wtforms.validators.InputRequired())
    else:
        kw['validators'].append(wtforms.validators.Optional())

    if attribute.value_min or attribute.value_max:
        # for string min and max are used to test length
        if attribute.type == 'string':
            if attribute.value_min == attribute.value_max:
                message = u'Field must be %(min)s characters long.'
            else:
                message = None
            kw['validators'].append(wtforms.validators.Length(
                min=attribute.value_min if attribute.value_min is not None else -1,
                max=attribute.value_max if attribute.value_max is not None else -1,
                message=message))
        if attribute.type == 'choice' and attribute.is_collection:
            if attribute.value_min == attribute.value_max:
                message = u'Field must be %(min)s characters long.'
            elif attribute.value_min is not None \
                    and attribute.value_max is None:
                message = u'Field must have at least %(min)s selected.'
            elif attribute.value_min is None \
                    and attribute.value_max is not None:
                message = u'Field must have at most %(max)s selected.'
            else:
                message = None
            kw['validators'].append(wtforms.validators.Length(
                min=attribute.value_min if attribute.value_min is not None else -1,
                max=attribute.value_max if attribute.value_max is not None else -1,
                message=message))

        # for number min and max are used to test the value
        elif attribute.type == 'number':
            if attribute.value_min == attribute.value_max:
                message = u'Number must be %(min)s.'
            else:
                message = None
            kw['validators'].append(wtforms.validators.NumberRange(
                min=attribute.value_min,
                max=attribute.value_max,
                message=message))

    if attribute.pattern:
        kw['validators'].append(wtforms.validators.Regexp(attribute.pattern))

    return field_class(**kw)


def make_form(session,
              schema,
              entity=None,
              formdata=None,
              show_metadata=True,
              transition=modes.AUTO,
              allowed_versions=None):
    """
    Converts a Datastore schema to a WTForm for data entry

    Parameters:
    session -- the database session to query for form metata
    schema -- the assumed form for data entry
    formdata -- (optional) incoming data for lookahead purposes:
                * if ``version`` changes, then the specified version will
                  override the ``schema`` parameter
    show_metadata -- (optional) includes entity metadata fields
    allowed_versions -- list of schemata versions that can override ``schema``

    Returns:
    A WTForm class. The reason why an instance is not returns is in case
    the user wants to sitch together multiple forms for Long Forms.
    """

    class DatastoreForm(wtforms.Form):

        class Meta:
            pass

        setattr(Meta, 'schema', schema)
        setattr(Meta, 'entity', entity)

        def validate(self, **kw):
            status = True

            if 'ofworkflow_' in self:
                status = status and self.ofworkflow_.validate(self)

                # No further validation needed if we're going to
                # erase the data anyway
                if self.ofworkflow_.state.data == states.PENDING_ENTRY:
                    return status

            # Skip all validation if coming from a read-only state
            if self.meta.entity \
                    and self.meta.entity.state.name == states.COMPLETE:
                return status

            if 'ofmetadata_' in self and self.ofmetadata_.not_done.data:
                return status and self.ofmetadata_.validate(self)

            else:
                return status and super(DatastoreForm, self).validate(**kw)

    if show_metadata:

        # If there was a version change so we render the correct form
        if formdata and 'ofmetadata_-version' in formdata:
            schema = (
                session.query(datastore.Schema)
                .filter_by(
                    name=schema.name,
                    publish_date=formdata['ofmetadata_-version'])
                .one())

        if not allowed_versions:
            allowed_versions = []

        allowed_versions.append(schema.publish_date)
        allowed_versions = sorted(set(allowed_versions))

        actual_versions = [(str(p), str(p)) for (p,) in (
            session.query(datastore.Schema.publish_date)
            .filter(datastore.Schema.name == schema.name)
            .filter(datastore.Schema.publish_date.in_(allowed_versions))
            .filter(datastore.Schema.retract_date == sa.null())
            .order_by(datastore.Schema.publish_date.asc())
            .all())]

        if len(allowed_versions) != len(actual_versions):
            log.warn(
                'Inconsitent versions: %s != %s' % (
                    allowed_versions, actual_versions))

        class Metadata(wtforms.Form):
            not_done = wtforms.BooleanField(_(u'Not Collected'))
            collect_date = wtforms.ext.dateutil.fields.DateField(
                _(u'Collect Date'),
                widget=wtforms.widgets.html5.DateInput(),
                validators=[
                    wtforms.validators.InputRequired(),
                    DateRange(min=date(1900, 1, 1)),
                ])
            version = wtforms.SelectField(
                _(u'Version'),
                choices=actual_versions,
                validators=[wtforms.validators.InputRequired()])

        setattr(DatastoreForm, 'ofmetadata_', wtforms.FormField(Metadata))

    if transition == modes.ALL:
        allowed_states = TRANSITIONS.keys()

    elif transition == modes.AVAILABLE:

        try:
            current_state = entity.state.name
        except AttributeError:
            current_state = states.PENDING_ENTRY

        allowed_states = TRANSITIONS[current_state]

    else:
        allowed_states = []

    if allowed_states:

        allowed_states = (
            session.query(datastore.State)
            .filter(datastore.State.name.in_(allowed_states))
            .order_by(datastore.State.title)
        )

        choices = [('', '')] \
            + [(state.name, state.title) for state in allowed_states]

        class Workflow(wtforms.Form):
            state = wtforms.SelectField(
                _(u'Set state to...'),
                choices=choices,
                validators=[
                    wtforms.validators.InputRequired(
                        _('Please select a state'))
                ])

        setattr(DatastoreForm, 'ofworkflow_', wtforms.FormField(Workflow))

    for attribute in schema.itertraverse():
        setattr(DatastoreForm, attribute.name, make_field(attribute))

    return DatastoreForm


def make_longform(session, schemata):
    """
    Converts multiple Datastore schemata to a sinlge WTForm.
    """

    class LongForm(wtforms.Form):
        pass

    for schema in schemata:
        form = make_form(session, schema, show_metadata=False)
        setattr(LongForm, schema.name, wtforms.FormField(form))

    return LongForm


def render_form(form,
                cancel_url=None,
                disabled=False,
                show_footer=True,
                attr=None):
    """
    Helper function to render a WTForm by OCCAMS standards
    """

    entity = form.meta.entity
    schema = form.meta.schema

    # We differentiate beween metadata and fields here because
    # sometimes we want the data fields to be disabled (when the form
    # is not collected) or the whole form disabled (when complete)
    # Also, we need to convert to bool and not true-ish or false-ish
    # because wtforms will still render the attribute if not explicitly boolean
    metadata_disabled = bool(disabled or (
        entity and entity.state and entity.state.name == states.COMPLETE))
    fields_disabled = bool(disabled or metadata_disabled or (
        entity and entity.not_done))

    return render('occams_forms:templates/form.pt', {
        'cancel_url': cancel_url,
        'schema': schema,
        'entity': entity,
        'form': form,
        'show_footer': show_footer,
        'metadata_disabled': metadata_disabled,
        'fields_disabled': fields_disabled,
        'disabled': disabled,
        'attr': attr or {},
    })


def entity_data(entity):
    """
    Serializes an entity into a dictionary for data entry
    """

    data = {
        'ofmetadata_': {
            'state': entity.state and entity.state.name,
            'not_done': entity.not_done,
            'collect_date': entity.collect_date,
            'version': str(entity.schema.publish_date),
        }
    }

    for attribute in entity.schema.iterleafs():

        if attribute.parent_attribute:
            parent = data.setdefault(attribute.parent_attribute.name, {})
        else:
            parent = data

        parent[attribute.name] = entity[attribute.name]

    return data


def apply_data(session, entity, data, upload_path):
    """
    Updates an entity with a dictionary of data
    """

    assert upload_path is not None, u'Destination path is required'

    previous_state = entity.state and entity.state.name

    # Assume the user can control transitions if we promped for it
    if 'ofworkflow_' in data:
        next_state = data['ofworkflow_']['state']

    # Anyone without the ability to transition needs to be approved
    else:
        next_state = states.PENDING_REVIEW

    # States are the only metadata that can change regardless of transition
    if previous_state != next_state:
        entity.state = (
            session.query(datastore.State).filter_by(name=next_state).one())

    # Do not update data if we're transitioning from a readonly-state
    if previous_state == states.COMPLETE:
        return entity

    if 'ofmetadata_' in data:
        metadata = data['ofmetadata_']
        if next_state == states.PENDING_ENTRY:
            entity.not_done = False
        else:
            entity.not_done = metadata['not_done']
        entity.collect_date = metadata['collect_date']
        entity.schema = (
            session.query(datastore.Schema)
            .filter_by(
                name=entity.schema.name,
                publish_date=metadata['version'])
            .one())

    clear_data = entity.not_done or next_state == states.PENDING_ENTRY

    for attribute in entity.schema.iterleafs():

        if clear_data:
            entity[attribute.name] = None
            continue

        # Find the appropriate attribute to update
        if attribute.parent_attribute:
            parent = data[attribute.parent_attribute.name]
        else:
            parent = data

        # Accomodate patch data (i.e. incomplete data, for updates)
        if attribute.name not in parent:
            continue

        if attribute.type == 'blob':
            # if data[attribute.name] is empty, it means field was empty
            # Python 2.7-3.3 has a bug where FieldStorage will yield False
            # unexpectetly, so ensure that the actual key value is an
            # instance of FieldStorage

            if isinstance(data[attribute.name], cgi.FieldStorage):
                original_name = os.path.basename(data[attribute.name].filename)
                input_file = data[attribute.name].file

                generated_path = os.path.join(*str(uuid.uuid4()).split('-'))
                dest_path = os.path.join(upload_path, generated_path)

                # create a directory excluding the filename
                os.makedirs(os.path.dirname(dest_path))

                # Write to a temporary file to prevent using incomplete files
                temp_dest_path = dest_path + '~'

                output_file = open(temp_dest_path, 'wb')

                input_file.seek(0)
                while True:
                    data = input_file.read(2 << 16)
                    if not data:
                        break
                    output_file.write(data)

                # Make sure the data is commited to the file system
                # before closing
                output_file.flush()
                os.fsync(output_file.fileno())

                output_file.close()

                # Rename successfully uploaded file
                os.rename(temp_dest_path, dest_path)

                # get mime type using filemagic
                # this depends on os program libmagic
                # look for alternate method in the future
                # to reduce dependencies
                with magic.Magic(flags=magic.MAGIC_MIME_TYPE) as m:
                    mime_type = m.id_filename(dest_path)

                value = datastore.BlobInfo(original_name, dest_path, mime_type)

            else:

                value = None

            if isinstance(entity[attribute.name], datastore.BlobInfo):
                os.unlink(entity[attribute.name].path)

        else:
            value = parent[attribute.name]

        entity[attribute.name] = value

    return entity
