""" Translatable models, the main hvad API.
"""
import django
from django.core import checks
from django.core.exceptions import ImproperlyConfigured, FieldDoesNotExist
from django.db import models, router, transaction
from django.db.models.base import ModelBase
from django.db.models.manager import Manager
from django.db.models.signals import class_prepared
from django.utils.translation import get_language
from hvad.descriptors import LanguageCodeAttribute, TranslatedAttribute
from hvad.fields import SingleTranslationObject, MasterKey
from hvad.manager import TranslationManager
from hvad.settings import hvad_settings
from hvad.utils import get_cached_translation, set_cached_translation, SmartGetField
from types import MethodType
from itertools import chain
import sys

__all__ = ('TranslatableModel', 'TranslatedFields', 'NoTranslation')

forbidden_translated_fields = ('Meta', 'objects', 'master', 'master_id')

#===============================================================================

NoTranslation = object()

#===============================================================================

class TranslatedFields(object):
    """ Wrapper class to define translated fields on a model. """

    def __init__(self, meta=None, base_class=None, **fields):
        forbidden = set(forbidden_translated_fields).intersection(fields)
        if forbidden:
            raise ImproperlyConfigured(
                'Invalid translated field: %s' % ', '.join(sorted(forbidden)))
        self.meta = meta or {}
        self.base_class = base_class
        self.fields = fields

    @staticmethod
    def _split_together(constraints, fields, name):
        sconst, tconst = [], []
        for constraint in constraints:
            if all(item in fields for item in constraint):
                tconst.append(constraint)
            elif not any(item in fields for item in constraint):
                sconst.append(constraint)
            else:
                raise ImproperlyConfigured(
                    'Constraints in Meta.%s cannot mix translated and '
                    'untranslated fields, such as %r.' % (name, constraint))
        return sconst, tconst

    def contribute_to_class(self, model, name):
        if model._meta.order_with_respect_to in self.fields:
            raise ImproperlyConfigured(
                'Using a translated fields in %s.Meta.order_with_respect_to is ambiguous '
                'and hvad does not support it.' %
                model._meta.model_name
            )
        if hasattr(model._meta, 'translations_model'):
            raise ImproperlyConfigured(
                "A TranslatableModel can only define one set of "
                "TranslatedFields, %r defines more than one." % model
            )
        translations_model = self.create_translations_model(model, name)
        model._meta.translations_model = translations_model
        if not model._meta.abstract:
            model._meta.translations_accessor = name

    def create_translations_model(self, model, related_name):
        """ Create the translations model for a shared model.
            model -- the model class to create translations for
            related_name -- the related name for the reverse FK from the translations model.
        """
        model_name = '%sTranslation' % model.__name__
        translation_bases, translation_base_fields = self._scan_model_bases(model)

        attrs = self.fields.copy()
        attrs.update({
            'Meta': self._build_meta_class(
                model, translation_base_fields.union(self.fields).union(('language_code',))
            ),
            '__module__': model.__module__,
        })

        if not model._meta.abstract:
            # If this class is abstract, we must not contribute management fields
            attrs['master'] = MasterKey(model, related_name=related_name,
                                        editable=False, on_delete=models.CASCADE, null=True)
            if 'language_code' not in attrs:    # allow overriding
                attrs['language_code'] = models.CharField(max_length=15, db_index=True)

        # Create the new model
        if self.base_class:
            translation_bases.insert(0, self.base_class)
        translations_model = ModelBase(model_name, tuple(translation_bases), attrs)
        translations_model._meta.shared_model = model
        if not model._meta.abstract:
            # Abstract models do not have a DNE class
            bases = (model.DoesNotExist, translations_model.DoesNotExist,)
            translations_model.DoesNotExist = type('DoesNotExist', bases, {})

        # Register it as a global in the shared model's module.
        # This is needed so that Translation model instances, and objects which
        # refer to them, can be properly pickled and unpickled. The Django session
        # and caching frameworks, in particular, depend on this behaviour.
        setattr(sys.modules[model.__module__], model_name, translations_model)
        return translations_model

    def _scan_model_bases(self, model):
        """ Scan the model class' bases, collecting translated fields """
        bases, fields = list(), set()
        scan_bases = list(reversed(model.__bases__))
        while scan_bases:
            base = scan_bases.pop()
            if base is TranslatableModel or not issubclass(base, TranslatableModel):
                continue
            if not base._meta.abstract:
                raise TypeError(
                    'Multi-table inheritance of translatable models is not supported. '
                    'Concrete model %s is not a valid base model for %s.' %
                    (base._meta.model_name, model._meta.model_name)
                )
            # The base may have translations model, then just inherit that
            if hasattr(base._meta, 'translations_model'):
                bases.append(base._meta.translations_model)
                fields.update(field.name for field in base._meta.translations_model._meta.fields)
            else:
                # But it may not, and simply inherit other abstract bases, scan them
                scan_bases.extend(reversed(base.__bases__))
        bases.append(BaseTranslationModel)
        return bases, fields

    def _build_meta_class(self, model, tfields):
        """ Create the Meta class for the translation model
            model -- the shared model
            tfields -- the list of names of all fields, direct and inherited
        """
        abstract = model._meta.abstract
        meta = self.meta.copy()
        meta.update({
            'abstract': abstract,
            'db_tablespace': model._meta.db_tablespace,
            'managed': model._meta.managed,
            'app_label': model._meta.app_label,
            'db_table': meta.get('db_table',
                                 hvad_settings.TABLE_NAME_FORMAT % (model._meta.db_table,)),
            'default_permissions': (),
        })

        # Split fields in Meta.unique_together
        sconst, tconst = self._split_together(
            model._meta.unique_together, tfields, 'unique_together'
        )
        model._meta.unique_together = tuple(sconst)
        model._meta.original_attrs['unique_together'] = tuple(sconst)
        meta['unique_together'] = tuple(tconst)
        if not abstract:
            meta['unique_together'] += (('language_code', 'master'),)

        # Split fields in Meta.index_together
        sconst, tconst = self._split_together(
            model._meta.index_together, tfields, 'index_together'
        )
        model._meta.index_together = tuple(sconst)
        model._meta.original_attrs['index_together'] = tuple(sconst)
        meta['index_together'] = tuple(tconst)

        return type('Meta', (object,), meta)

#===============================================================================

class BaseTranslationModel(models.Model):
    """ Base model for all translation models """

    def _get_unique_checks(self, exclude=None):
        # Due to the way translations are handled, checking for unicity of
        # the ('language_code', 'master') constraint is useless. We filter it out
        # here so as to avoid a useless query
        unique_checks, date_checks = super(BaseTranslationModel, self)._get_unique_checks(exclude=exclude)
        unique_checks = [check for check in unique_checks
                         if check != (self.__class__, ('language_code', 'master'))]
        return unique_checks, date_checks

    class Meta:
        abstract = True

#===============================================================================

class TranslatableModel(models.Model):
    """
    Base model for all models supporting translated fields (via TranslatedFields).
    """
    objects = TranslationManager()
    _plain_manager = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = '_plain_manager'

    def __init__(self, *args, **kwargs):
        # Split arguments into shared/translatd
        veto_names = ('pk', 'master', 'master_id', self._meta.translations_model._meta.pk.name)
        skwargs, tkwargs = {}, {}
        translations_opts = self._meta.translations_model._meta
        for key, value in kwargs.items():
            if key in veto_names:
                skwargs[key] = value
            else:
                try:
                    translations_opts.get_field(key)
                except FieldDoesNotExist:
                    skwargs[key] = value
                else:
                    tkwargs[key] = value
        super(TranslatableModel, self).__init__(*args, **skwargs)
        language_code = tkwargs.get('language_code') or get_language()
        if language_code is not NoTranslation:
            tkwargs['language_code'] = language_code
            set_cached_translation(self, self._meta.translations_model(**tkwargs))

    @classmethod
    def from_db(cls, db, field_names, values):
        if len(values) != len(cls._meta.concrete_fields):
            # Missing values are deferred and must be marked as such
            values = list(values)
            values.reverse()
            values = [values.pop() if f.attname in field_names else models.DEFERRED
                    for f in cls._meta.concrete_fields]
        new = cls(*values, language_code=NoTranslation)
        new._state.adding = False
        new._state.db = db
        return new

    def save(self, *args, **skwargs):
        veto_names = ('pk', 'master', 'master_id', self._meta.translations_model._meta.pk.name)
        translations_opts = self._meta.translations_model._meta
        translation = get_cached_translation(self)
        tkwargs = skwargs.copy()

        # split update_fields in shared/translated fields
        update_fields = skwargs.get('update_fields')
        if update_fields is not None:
            supdate, tupdate = [], []
            for name in update_fields:
                if name in veto_names:
                    supdate.append(name)
                else:
                    try:
                        translations_opts.get_field(name)
                    except FieldDoesNotExist:
                        supdate.append(name)
                    else:
                        tupdate.append(name)
            skwargs['update_fields'], tkwargs['update_fields'] = supdate, tupdate

        # save share and translated model in a single transaction
        db = router.db_for_write(self.__class__, instance=self)
        with transaction.atomic(using=db, savepoint=False):
            if update_fields is None or skwargs['update_fields']:
                super(TranslatableModel, self).save(*args, **skwargs)
            if (update_fields is None or tkwargs['update_fields']) and translation is not None:
                if translation.pk is None and update_fields:
                    del tkwargs['update_fields'] # allow new translations
                translation.master = self
                translation.save(*args, **tkwargs)
    save.alters_data = True

    def translate(self, language_code):
        """ Create a new translation for current instance.
            Does NOT check if the translation already exists.
        """
        set_cached_translation(
            self,
            self._meta.translations_model(language_code=language_code)
        )
    translate.alters_data = True

    #===========================================================================
    # Validation
    #===========================================================================

    def clean_fields(self, exclude=None):
        super(TranslatableModel, self).clean_fields(exclude=exclude)
        translation = get_cached_translation(self)
        if translation is not None:
            translation.clean_fields(exclude=exclude + ['id', 'master', 'master_id', 'language_code'])

    def validate_unique(self, exclude=None):
        super(TranslatableModel, self).validate_unique(exclude=exclude)
        translation = get_cached_translation(self)
        if translation is not None:
            translation.validate_unique(exclude=exclude)

    #===========================================================================
    # Checks
    #===========================================================================

    @classmethod
    def check(cls, **kwargs):
        errors = super(TranslatableModel, cls).check(**kwargs)
        errors.extend(cls._check_shared_translated_clash())
        errors.extend(cls._check_default_manager_translation_aware())
        return errors

    @classmethod
    def _check_shared_translated_clash(cls):
        fields = set(chain.from_iterable(
            (f.name, f.attname)
            for f in cls._meta.fields
        ))
        tfields = set(chain.from_iterable(
            (f.name, f.attname)
            for f in cls._meta.translations_model._meta.fields
            if f.name not in ('id', 'master')
        ))
        return [checks.Error("translated field '%s' clashes with untranslated field." % field,
                             hint=None, obj=cls, id='hvad.models.E01')
                for field in tfields.intersection(fields)]

    @classmethod
    def _check_default_manager_translation_aware(cls):
        errors = []
        if not isinstance(cls._default_manager, TranslationManager):
            errors.append(checks.Error(
                "The default manager on a TranslatableModel must be a "
                "TranslationManager instance or an instance of a subclass of "
                "TranslationManager, the default manager of %r is not." % cls,
                hint=None, obj=cls, id='hvad.models.E02'
            ))
        return errors

    @classmethod
    def _check_local_fields(cls, fields, option):
        """ Remove fields we recognize as translated fields from tests """
        to_check = []
        for field in fields:
            try:
                cls._meta.translations_model._meta.get_field(field)
            except FieldDoesNotExist:
                to_check.append(field)
        return super(TranslatableModel, cls)._check_local_fields(to_check, option)

    @classmethod
    def _check_ordering(cls):
        if not cls._meta.ordering:
            return []

        if not isinstance(cls._meta.ordering, (list, tuple)):
            return [checks.Error("'ordering' must be a tuple or list.",
                                 hint=None, obj=cls, id='models.E014')]

        fields = [f for f in cls._meta.ordering if f != '?']
        fields = [f[1:] if f.startswith('-') else f for f in fields]
        fields = set(f for f in fields if f not in ('_order', 'pk') and '__' not in f)

        valid_fields = set(chain.from_iterable(
            (f.name, f.attname)
            for f in cls._meta.fields
        ))
        valid_tfields = set(chain.from_iterable(
            (f.name, f.attname)
            for f in cls._meta.translations_model._meta.fields
            if f.name not in ('master', 'language_code')
        ))

        return [checks.Error("'ordering' refers to the non-existent field '%s' --hvad." % field,
                             hint=None, obj=cls, id='models.E015')
                for field in fields - valid_fields - valid_tfields]

#=============================================================================

def prepare_translatable_model(sender, **kwargs):
    """ Make a model translatable if it inherits TranslatableModel.
        Invoked by Django after it has finished setting up any model.
    """
    model = sender
    if not issubclass(model, TranslatableModel) or model._meta.abstract:
        return

    if model._meta.proxy:
        model._meta.translations_accessor = model._meta.concrete_model._meta.translations_accessor
        model._meta.translations_model = model._meta.concrete_model._meta.translations_model

    if not hasattr(model._meta, 'translations_model'):
        raise ImproperlyConfigured("No TranslatedFields found on %r, subclasses of "
                                   "TranslatableModel must define TranslatedFields." % model)

    #### Now we have to work ####

    # Create query foreign object
    if model._meta.proxy:
        hvad_query = model._meta.concrete_model._meta.get_field('_hvad_query')
    else:
        hvad_query = SingleTranslationObject(model)
        model.add_to_class('_hvad_query', hvad_query)

    # Set descriptors
    ignore_fields = ('pk', 'master', 'master_id', 'language_code',
                     model._meta.translations_model._meta.pk.name)
    setattr(model, 'language_code', LanguageCodeAttribute(model, hvad_query))
    for field in model._meta.translations_model._meta.fields:
        if field.name in ignore_fields:
            continue
        setattr(model, field.name, TranslatedAttribute(model, field.name, hvad_query))
        attname = field.get_attname()
        if attname and attname != field.name:
            setattr(model, attname, TranslatedAttribute(model, attname, hvad_query))

    # Replace get_field_by_name with one that warns for common mistakes
    if not isinstance(model._meta.get_field, SmartGetField):
        model._meta.get_field = MethodType(
            SmartGetField(model._meta.get_field),
            model._meta
        )


class_prepared.connect(prepare_translatable_model)
