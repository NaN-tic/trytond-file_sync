from trytond.model import (
    DeactivableMixin, ModelSingleton, ModelSQL, ModelView, Unique, fields)
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import (
    Transaction, inactive_records, without_check_access)


class BigInteger(fields.Integer):
    _sql_type = 'BIGINT'


class Category(metaclass=PoolMeta):
    __name__ = 'office.category'

    sync = fields.Boolean(
        "Synchronize",
        states={
            'invisible': Bool(Eval('parent')),
            },
        depends=['parent'])
    file_sync_path = fields.Char("Synchronized Path", readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.view.states.update({
                'invisible': Bool(Eval('sync')),
                })
        cls.view.depends.add('sync')
        cls._buttons.update({
                'synchronize_files': {
                    'invisible': ~Bool(Eval('sync')) | Bool(Eval('parent')),
                    },
                })

    @staticmethod
    def default_sync():
        return False

    @classmethod
    def on_modification(cls, mode, categories, field_names=None):
        super().on_modification(mode, categories, field_names=field_names)
        if mode not in {'create', 'write'}:
            return

        disable_sync = [category for category in categories
            if category.parent and category.sync]
        disable_view = [category for category in categories
            if not category.parent and category.sync and category.view]
        if disable_sync:
            super().write(disable_sync, {'sync': False})
        if disable_view:
            super().write(disable_view, {'view': False})

    def synchronized_root(self):
        category = self
        while category.parent:
            category = category.parent
        if category.active and category.sync:
            return category

    @classmethod
    def create(cls, vlist):
        categories = super().create(vlist)
        if (categories
                and not Transaction().context.get('file_sync_skip')):
            roots = {category.synchronized_root() for category in categories}
            Pool().get('file.sync.configuration').queue_synchronize(
                roots=[root for root in roots if root])
        return categories

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        pairs = list(zip(actions, actions))
        categories = {category for records, values in pairs
            for category in records}
        old_roots = {category.synchronized_root() for category in categories}
        old_paths = {category: category.file_sync_path
            for category in categories}
        super().write(*args)
        if Transaction().context.get('file_sync_skip'):
            return
        watched_fields = {'name', 'parent', 'sync', 'active'}
        if not any(watched_fields & set(values) for _, values in pairs):
            return
        moves = None
        if any({'name', 'parent'} & set(values) for _, values in pairs):
            moves = old_paths
        deactivated = {
            category: old_paths[category] for category in categories
            if not category.active}
        if deactivated:
            with Transaction().set_context(file_sync_skip=True):
                cls.write(list(deactivated), {'file_sync_path': None})
        roots = old_roots | {
            category.synchronized_root() for category in categories}
        Pool().get('file.sync.configuration').queue_synchronize(
            roots=[root for root in roots if root],
            moves=moves,
            removals=deactivated.values())

    @classmethod
    def delete(cls, categories):
        categories = list(categories)
        old_paths = {category: category.file_sync_path
            for category in categories}
        super().delete(categories)
        if (categories
                and not Transaction().context.get('file_sync_skip')):
            Pool().get('file.sync.configuration').queue_synchronize(
                removals=old_paths.values())

    @classmethod
    @ModelView.button
    def synchronize_files(cls, categories):
        roots = {category.synchronized_root() for category in categories}
        Pool().get('file.sync.configuration').queue_synchronize(
            roots=[root for root in roots if root], required=True)


class Attachment(metaclass=PoolMeta):
    __name__ = 'ir.attachment'

    @classmethod
    def create(cls, vlist):
        attachments = super().create(vlist)
        if (attachments
                and not Transaction().context.get('file_sync_skip')):
            cls.__queue__.synchronize_files(attachments)
        return attachments

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        pairs = list(zip(actions, actions))
        attachments = {attachment for records, _ in pairs
            for attachment in records}
        super().write(*args)
        if (attachments
                and not Transaction().context.get('file_sync_skip')):
            cls.__queue__.synchronize_files(attachments)

    @classmethod
    def delete(cls, attachments):
        attachments = list(attachments)
        active = [attachment for attachment in attachments
            if attachment.active]
        inactive = [attachment for attachment in attachments
            if not attachment.active]
        if active:
            super().delete(active)

        if inactive:
            Entry = Pool().get('file.sync.entry')
            pending_ids = {entry.attachment.id for entry in Entry.search([
                        ('attachment', 'in', [attachment.id
                                for attachment in inactive]),
                        ])}
            pending = [attachment for attachment in inactive
                if attachment.id in pending_ids]
            removable = [attachment for attachment in inactive
                if attachment.id not in pending_ids]
            if removable:
                with Transaction().set_context(file_sync_skip=True):
                    super().delete(removable)
            if pending:
                cls.__queue__.delete_inactive(pending)

    @classmethod
    def synchronize_files(cls, attachments):
        Pool().get('file.sync.entry').synchronize_records(attachments)

    @classmethod
    def delete_inactive(cls, attachments):
        attachments = [attachment for attachment in attachments
            if not attachment.active]
        if not attachments:
            return
        Pool().get('file.sync.entry').synchronize_records(
            attachments, deletion=True)
        with Transaction().set_context(file_sync_skip=True):
            super().delete(attachments)


class SyncEntry(DeactivableMixin, ModelSQL, ModelView):
    "Last synchronized state for one resource in one category."
    __name__ = 'file.sync.entry'

    _file_sync_ignore_patterns = {'.file-sync-*'}

    category = fields.Many2One(
        'office.category', "Category", required=True, ondelete='CASCADE')
    attachment = fields.Many2One(
        'ir.attachment', "Attachment", required=True, ondelete='CASCADE')
    path = fields.Char("Relative Path", required=True)
    digest = fields.Char("SHA-256", required=True)
    merge_base = fields.Binary("Merge Base")
    size = BigInteger("Size", required=True)
    mtime_ns = BigInteger("Modification Time", required=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('category_attachment_unique',
                Unique(table, table.category, table.attachment),
                'file_sync.msg_entry_attachment_unique'),
            ]

    @classmethod
    def __register__(cls, module_name):
        handler = cls.__table_handler__(module_name)
        had_document = handler.column_exist('document')
        if (handler.column_exist('tag')
                and not handler.column_exist('category')):
            handler.column_rename('tag', 'category')
        handler.drop_constraint('tag_attachment_unique')
        super().__register__(module_name)
        if had_document:
            handler.drop_column('document')

    @property
    def resource(self):
        return self.attachment

    @classmethod
    def get_synchronizer(cls, required=False):
        from .sync import Synchronizer
        return Synchronizer(
            required=required,
            ignore_patterns=cls._file_sync_ignore_patterns)

    @classmethod
    def synchronize(cls, roots=None, required=False):
        cls.get_synchronizer(required=required).synchronize(roots)

    @classmethod
    def synchronize_records(cls, records, deletion=False):
        cls.get_synchronizer().synchronize_records(
            records, deletion=deletion)

    @classmethod
    def synchronize_path(cls, path):
        cls.get_synchronizer(required=True).synchronize_path(path)

    @classmethod
    def move_categories(cls, old_paths):
        cls.get_synchronizer().move_categories(old_paths)

    @classmethod
    def remove_category_directories(cls, paths):
        cls.get_synchronizer().remove_category_directories(paths)


class Configuration(ModelSingleton, ModelSQL, ModelView):
    "File Sync Configuration"
    __name__ = 'file.sync.configuration'

    @classmethod
    def queue_synchronize(
            cls, roots=None, moves=None, removals=None, required=False):
        root_ids = sorted({root.id for root in roots or [] if root})
        moves = sorted(
            (category.id, path)
            for category, path in (moves or {}).items() if path)
        removals = sorted({path for path in removals or [] if path})
        if not root_ids and not moves and not removals:
            return
        with without_check_access():
            configurations = cls.search([], limit=1)
        if configurations:
            cls.__queue__.synchronize(
                configurations, root_ids, moves, removals, required)

    @classmethod
    def synchronize(
            cls, configurations, root_ids, moves, removals, required):
        pool = Pool()
        Entry = pool.get('file.sync.entry')
        Category = pool.get('office.category')
        category_ids = (
            set(root_ids) | {category_id for category_id, _ in moves})
        with inactive_records():
            categories = (Category.search([('id', 'in', category_ids)])
                if category_ids else [])
        categories = {category.id: category for category in categories}
        if moves:
            Entry.move_categories({
                    categories[category_id]: path for category_id, path in moves
                    if category_id in categories})
        if removals:
            Entry.remove_category_directories(removals)
        roots = [categories[root_id] for root_id in root_ids
            if root_id in categories]
        if roots:
            Entry.synchronize(roots, required=required)
