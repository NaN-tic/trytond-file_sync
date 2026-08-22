from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import (
    DeactivableMixin, ModelSingleton, ModelSQL, ModelView, Unique, fields)
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import (
    Transaction, inactive_records, without_check_access)


class BigInteger(fields.Integer):
    _sql_type = 'BIGINT'


class Tag(metaclass=PoolMeta):
    __name__ = 'brainbow.tag'

    sync = fields.Boolean(
        "Synchronize",
        states={
            'invisible': Bool(Eval('parent')),
            },
        depends=['parent'])
    attachments = fields.Many2Many(
        'file.sync.tag-ir.attachment', 'tag', 'attachment', "Attachments")
    read_only_groups = fields.Many2Many(
        'file.sync.tag-read-only-group', 'tag', 'group', "Read-only Groups")
    read_write_groups = fields.Many2Many(
        'file.sync.tag-read-write-group', 'tag', 'group',
        "Read-write Groups")
    read_only_users = fields.Many2Many(
        'file.sync.tag-read-only-user', 'tag', 'user', "Read-only Users")
    read_write_users = fields.Many2Many(
        'file.sync.tag-read-write-user', 'tag', 'user', "Read-write Users")
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
    def on_modification(cls, mode, tags, field_names=None):
        super().on_modification(mode, tags, field_names=field_names)
        if mode not in {'create', 'write'}:
            return

        disable_sync = [tag for tag in tags if tag.parent and tag.sync]
        disable_view = [tag for tag in tags
            if not tag.parent and tag.sync and tag.view]
        if disable_sync:
            super().write(disable_sync, {'sync': False})
        if disable_view:
            super().write(disable_view, {'view': False})

    def synchronized_root(self):
        tag = self
        while tag.parent:
            tag = tag.parent
        if tag.active and tag.sync:
            return tag

    def access_users(self):
        "Return the inherited read-write and read-only user sets."
        read_write = set()
        read_only = set()
        tag = self
        while tag:
            read_write.update(tag.read_write_users)
            read_only.update(tag.read_only_users)
            for group in tag.read_write_groups:
                read_write.update(group.users)
            for group in tag.read_only_groups:
                read_only.update(group.users)
            tag = tag.parent
        read_only.difference_update(read_write)
        return read_write, read_only

    @classmethod
    def create(cls, vlist):
        tags = super().create(vlist)
        if (tags
                and not Transaction().context.get('file_sync_skip')):
            roots = {tag.synchronized_root() for tag in tags}
            Pool().get('file.sync.configuration').queue_synchronize(
                roots=[root for root in roots if root])
        return tags

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        pairs = list(zip(actions, actions))
        tags = {tag for records, values in pairs for tag in records}
        old_roots = {tag.synchronized_root() for tag in tags}
        old_paths = {tag: tag.file_sync_path for tag in tags}
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
            tag: old_paths[tag] for tag in tags if not tag.active}
        if deactivated:
            with Transaction().set_context(file_sync_skip=True):
                cls.write(list(deactivated), {'file_sync_path': None})
        roots = old_roots | {tag.synchronized_root() for tag in tags}
        Pool().get('file.sync.configuration').queue_synchronize(
            roots=[root for root in roots if root],
            moves=moves,
            removals=deactivated.values())

    @classmethod
    def delete(cls, tags):
        tags = list(tags)
        old_paths = {tag: tag.file_sync_path for tag in tags}
        super().delete(tags)
        if (tags
                and not Transaction().context.get('file_sync_skip')):
            Pool().get('file.sync.configuration').queue_synchronize(
                removals=old_paths.values())

    @classmethod
    @ModelView.button
    def synchronize_files(cls, tags):
        roots = {tag.synchronized_root() for tag in tags}
        Pool().get('file.sync.configuration').queue_synchronize(
            roots=[root for root in roots if root], required=True)


class Document(metaclass=PoolMeta):
    __name__ = 'brainbow.document'

    replaced_by = fields.Many2One(
        'brainbow.document', "Replaced By", readonly=True,
        ondelete='SET NULL', domain=[('id', '!=', Eval('id', -1))],
        states={
            'invisible': ~Bool(Eval('replaced_by')),
            })

    @classmethod
    def create(cls, vlist):
        documents = super().create(vlist)
        if (documents
                and not Transaction().context.get('file_sync_skip')):
            cls.__queue__.synchronize_files(documents)
        return documents

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        pairs = list(zip(actions, actions))
        documents = {document for records, _ in pairs for document in records}
        super().write(*args)
        if (documents
                and not Transaction().context.get('file_sync_skip')):
            cls.__queue__.synchronize_files(documents)

    @classmethod
    def delete(cls, documents):
        documents = list(documents)
        active = [document for document in documents if document.active]
        inactive = [document for document in documents if not document.active]
        skip = Transaction().context.get('file_sync_skip')

        if active:
            with Transaction().set_context(file_sync_skip=True):
                super().write(active, {'active': False})
        if active and not skip:
            cls.__queue__.synchronize_files(active)

        if inactive:
            Entry = Pool().get('file.sync.entry')
            pending_ids = {entry.document.id for entry in Entry.search([
                        ('document', 'in', [document.id
                                for document in inactive]),
                        ])}
            pending = [document for document in inactive
                if document.id in pending_ids]
            removable = [document for document in inactive
                if document.id not in pending_ids]
            if removable:
                with Transaction().set_context(file_sync_skip=True):
                    super().delete(removable)
            if pending:
                cls.__queue__.delete_inactive(pending)

    @classmethod
    def synchronize_files(cls, documents):
        Pool().get('file.sync.entry').synchronize_records(documents)

    @classmethod
    def delete_inactive(cls, documents):
        documents = [document for document in documents
            if not document.active]
        if not documents:
            return
        Pool().get('file.sync.entry').synchronize_records(
            documents, deletion=True)
        with Transaction().set_context(file_sync_skip=True):
            super().delete(documents)


class Attachment(DeactivableMixin, metaclass=PoolMeta):
    __name__ = 'ir.attachment'

    tags = fields.Many2Many(
        'file.sync.tag-ir.attachment', 'attachment', 'tag', "Tags")
    replaced_by = fields.Many2One(
        'ir.attachment', "Replaced By", readonly=True,
        ondelete='SET NULL', domain=[('id', '!=', Eval('id', -1))],
        states={
            'invisible': ~Bool(Eval('replaced_by')),
            })

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
        skip = Transaction().context.get('file_sync_skip')

        if active:
            with Transaction().set_context(file_sync_skip=True):
                super().write(active, {'active': False})
        if active and not skip:
            cls.__queue__.synchronize_files(active)

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
    "Last synchronized state for one resource in one tag."
    __name__ = 'file.sync.entry'

    _file_sync_ignore_patterns = {'.file-sync-*'}

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    document = fields.Many2One(
        'brainbow.document', "Document", ondelete='CASCADE')
    attachment = fields.Many2One(
        'ir.attachment', "Attachment", ondelete='CASCADE')
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
            ('tag_document_unique', Unique(table, table.tag, table.document),
                'file_sync.msg_entry_document_unique'),
            ('tag_attachment_unique',
                Unique(table, table.tag, table.attachment),
                'file_sync.msg_entry_attachment_unique'),
            ]

    @classmethod
    def validate_fields(cls, entries, field_names):
        super().validate_fields(entries, field_names)
        if not field_names or field_names & {'document', 'attachment'}:
            for entry in entries:
                if bool(entry.document) == bool(entry.attachment):
                    raise UserError(gettext(
                            'file_sync.msg_entry_single_resource'))

    @property
    def resource(self):
        return self.document or self.attachment

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
    def move_tags(cls, old_paths):
        cls.get_synchronizer().move_tags(old_paths)

    @classmethod
    def remove_tag_directories(cls, paths):
        cls.get_synchronizer().remove_tag_directories(paths)


class Configuration(ModelSingleton, ModelSQL, ModelView):
    "File Sync Configuration"
    __name__ = 'file.sync.configuration'

    @classmethod
    def queue_synchronize(
            cls, roots=None, moves=None, removals=None, required=False):
        root_ids = sorted({root.id for root in roots or [] if root})
        moves = sorted(
            (tag.id, path) for tag, path in (moves or {}).items() if path)
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
        Tag = pool.get('brainbow.tag')
        tag_ids = set(root_ids) | {tag_id for tag_id, _ in moves}
        with inactive_records():
            tags = Tag.search([('id', 'in', tag_ids)]) if tag_ids else []
        tags = {tag.id: tag for tag in tags}
        if moves:
            Entry.move_tags({
                    tags[tag_id]: path for tag_id, path in moves
                    if tag_id in tags})
        if removals:
            Entry.remove_tag_directories(removals)
        roots = [tags[root_id] for root_id in root_ids if root_id in tags]
        if roots:
            Entry.synchronize(roots, required=required)


class TagAttachment(ModelSQL):
    "Tag - Attachment"
    __name__ = 'file.sync.tag-ir.attachment'

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    attachment = fields.Many2One(
        'ir.attachment', "Attachment", required=True, ondelete='CASCADE')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.update({'tag', 'attachment'})
        table = cls.__table__()
        cls._sql_constraints += [
            ('tag_attachment_unique', Unique(table, table.tag,
                    table.attachment), 'file_sync.msg_tag_attachment_unique'),
            ]


class TagReadOnlyGroup(ModelSQL):
    "Tag - Read-only Group"
    __name__ = 'file.sync.tag-read-only-group'

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    group = fields.Many2One(
        'res.group', "Group", required=True, ondelete='CASCADE')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('tag')
        table = cls.__table__()
        cls._sql_constraints += [
            ('tag_group_unique', Unique(table, table.tag, table.group),
                'file_sync.msg_tag_group_unique'),
            ]


class TagReadWriteGroup(ModelSQL):
    "Tag - Read-write Group"
    __name__ = 'file.sync.tag-read-write-group'

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    group = fields.Many2One(
        'res.group', "Group", required=True, ondelete='CASCADE')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('tag')
        table = cls.__table__()
        cls._sql_constraints += [
            ('tag_group_unique', Unique(table, table.tag, table.group),
                'file_sync.msg_tag_group_unique'),
            ]


class TagReadOnlyUser(ModelSQL):
    "Tag - Read-only User"
    __name__ = 'file.sync.tag-read-only-user'

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    user = fields.Many2One(
        'res.user', "User", required=True, ondelete='CASCADE')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('tag')
        table = cls.__table__()
        cls._sql_constraints += [
            ('tag_user_unique', Unique(table, table.tag, table.user),
                'file_sync.msg_tag_user_unique'),
            ]


class TagReadWriteUser(ModelSQL):
    "Tag - Read-write User"
    __name__ = 'file.sync.tag-read-write-user'

    tag = fields.Many2One(
        'brainbow.tag', "Tag", required=True, ondelete='CASCADE')
    user = fields.Many2One(
        'res.user', "User", required=True, ondelete='CASCADE')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__access__.add('tag')
        table = cls.__table__()
        cls._sql_constraints += [
            ('tag_user_unique', Unique(table, table.tag, table.user),
                'file_sync.msg_tag_user_unique'),
            ]
