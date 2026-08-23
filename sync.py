import datetime
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

import trytond.config as config
from merge3 import Merge3
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.pool import Pool
from trytond.transaction import Transaction, inactive_records

TEXT_EXTENSIONS = {
    '.asm', '.bash', '.c', '.cc', '.cfg', '.cmake', '.conf', '.cpp',
    '.cs', '.css', '.csv', '.cxx', '.dart', '.editorconfig', '.env',
    '.erl', '.ex', '.exs', '.fish', '.fs', '.fsx', '.go', '.gql',
    '.gradle', '.graphql', '.groovy', '.h', '.hpp', '.hrl', '.htm',
    '.html', '.ini', '.java', '.js', '.json', '.jsonl', '.jsx', '.kt',
    '.kts', '.less', '.lua', '.md', '.markdown', '.php', '.pl', '.pm',
    '.properties', '.proto', '.ps1', '.py', '.pyi', '.r', '.rb', '.rs',
    '.rst', '.s', '.sass', '.scala', '.scss', '.sh', '.sql', '.svelte',
    '.swift', '.tex', '.toml', '.ts', '.tsx', '.tsv', '.txt', '.vb',
    '.vim', '.vue', '.xml', '.yaml', '.yml', '.zsh',
    }
TEXT_FILENAMES = {
    '.editorconfig', '.env', '.gitattributes', '.gitignore', 'dockerfile',
    'gemfile', 'jenkinsfile', 'makefile', 'procfile',
    }

logger = logging.getLogger(__name__)


class FileSystemDataManager:

    def __init__(self):
        self.backups = {}

    def __eq__(self, other):
        if not isinstance(other, FileSystemDataManager):
            return NotImplemented
        return True

    def snapshot(self, path):
        path = os.path.abspath(path)
        if path in self.backups:
            return
        if os.path.islink(path):
            self.backups[path] = ('symlink', os.readlink(path))
        elif os.path.isfile(path):
            descriptor, backup = tempfile.mkstemp(
                prefix='.file-sync-rollback-',
                dir=os.path.dirname(path))
            os.close(descriptor)
            os.unlink(backup)
            try:
                os.link(path, backup)
            except OSError:
                shutil.copy2(path, backup)
            self.backups[path] = ('file', backup)
        elif not os.path.exists(path):
            self.backups[path] = ('missing', None)
        else:
            raise IsADirectoryError(path)

    def abort(self, transaction):
        self.tpc_abort(transaction)

    def tpc_begin(self, transaction):
        pass

    def commit(self, transaction):
        pass

    def tpc_vote(self, transaction):
        pass

    def tpc_finish(self, transaction):
        self._discard()

    def tpc_abort(self, transaction):
        for path, (kind, backup) in reversed(self.backups.items()):
            try:
                if kind == 'file':
                    os.replace(backup, path)
                elif kind == 'symlink':
                    if os.path.lexists(path):
                        os.unlink(path)
                    os.symlink(backup, path)
                elif os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
            except OSError:
                logger.exception(
                    "Could not restore synchronized file %s", path)
        self._discard()

    def _discard(self):
        for kind, backup in self.backups.values():
            if kind == 'file':
                try:
                    os.unlink(backup)
                except FileNotFoundError:
                    pass
        self.backups.clear()


class Synchronizer:

    def __init__(self, required=False, ignore_patterns=None):
        configured_path = config.get('file_sync', 'path')
        if configured_path:
            configured_path = os.path.expanduser(configured_path)
            self.base_path = os.path.realpath(configured_path)
        else:
            self.base_path = None
        self.ignore_patterns = set(ignore_patterns or ())
        if required and not self.base_path:
            raise UserError(gettext('file_sync.msg_path_required'))

    def synchronize(self, roots=None):
        if not self.base_path:
            return
        Category = Pool().get('office.category')
        if roots is None:
            roots = Category.search([
                    ('parent', '=', None),
                    ('sync', '=', True),
                    ('active', '=', True),
                    ])
        roots = sorted(
            {root for root in roots if root and root.active and root.sync},
            key=lambda root: root.id)
        os.makedirs(self.base_path, exist_ok=True)
        for root in roots:
            self._synchronize_root(root)

    def synchronize_records(self, records, deletion=False):
        if not self.base_path:
            return
        for resource in records:
            entries = self._entries_for_resource(resource)
            desired_categories = set()
            if resource.active and not deletion:
                desired_categories = {
                    category for category in resource.categories
                    if category.active and category.synchronized_root()
                    }
            for entry in entries:
                if entry.category not in desired_categories or deletion:
                    self._remove_entry_from_erp(entry)
            for category in sorted(
                    desired_categories, key=lambda category: category.id):
                self._sync_db_resource(category, resource, missing_is_delete=False)

    def move_categories(self, old_paths):
        if not self.base_path:
            return
        for category, old_relative in old_paths.items():
            root = category.synchronized_root()
            if not old_relative or not root:
                continue
            source = self._validated_path(os.path.join(
                    self.base_path, old_relative))
            target = self._category_directory(root, category)
            if source == target or not os.path.lexists(source):
                continue
            if os.path.commonpath([self.base_path, source]) != self.base_path:
                raise UserError(gettext('file_sync.msg_path_outside_root',
                        path=source))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.exists(target):
                os.replace(source, target)
                logger.info(
                    "moved filesystem directory %s to %s", source, target)
            elif os.path.isdir(source) and os.path.isdir(target):
                self._merge_directories(source, target, category)
            else:
                conflict = self._conflict_path(target, 'erp-directory')
                os.replace(source, conflict)
                self._notify_conflict(category, self._relative(conflict))

    def remove_category_directories(self, paths):
        if not self.base_path:
            return
        relative_paths = sorted(
            {path for path in paths if path},
            key=lambda path: len(Path(path).parts))
        for relative in relative_paths:
            path = self._validated_path(
                os.path.join(self.base_path, relative),
                allow_leaf_symlink=True)
            if os.path.islink(path):
                os.unlink(path)
                logger.info("deleted filesystem symlink %s", path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
                logger.info("deleted filesystem directory %s", path)

    def synchronize_path(self, path):
        if not self.base_path:
            return
        if self.is_ignored_path(path):
            return
        path = os.path.realpath(os.path.abspath(path))
        if os.path.commonpath([self.base_path, path]) != self.base_path:
            raise UserError(gettext('file_sync.msg_path_outside_root',
                    path=path))
        if path == self.base_path:
            self.synchronize()
            return
        root = self._root_for_path(path)
        if not root:
            return
        if os.path.isdir(path) and not os.path.islink(path):
            category = self._category_for_directory(root, path, create=True)
            if category:
                seen_categories = set()
                self._walk_directory(root, category, path, seen_categories)
                self._reconcile_category(root, category, missing_is_delete=True)
        elif os.path.isfile(path) and not os.path.islink(path):
            category = self._category_for_directory(root, os.path.dirname(path),
                create=True)
            if category:
                self._sync_fs_file(category, path)
        elif not os.path.exists(path):
            relative = self._relative(path)
            Entry = Pool().get('file.sync.entry')
            entries = Entry.search([('path', '=', relative)])
            if entries:
                for entry in entries:
                    self._propagate_missing_entry(entry)
            else:
                Category = Pool().get('office.category')
                categories = Category.search([('file_sync_path', '=', relative)])
                for category in categories:
                    if category == root:
                        self._synchronize_root(root)
                    else:
                        self._deactivate_category_tree(category)

    def _synchronize_root(self, root):
        root_directory = self._category_directory(root, root)
        os.makedirs(root_directory, exist_ok=True)
        self._set_category_path(root, root_directory)

        seen_categories = set()
        self._walk_directory(root, root, root_directory, seen_categories)

        Category = Pool().get('office.category')
        descendants = Category.search([
                ('parent', 'child_of', [root.id]),
                ('active', '=', True),
                ])
        ignored_categories = sorted(
            [category for category in descendants if self._ignore_name(category.name)],
            key=lambda category: len(self._parents(category)))
        for category in ignored_categories:
            if category.active:
                self._deactivate_category_tree(category)
        descendants = [category for category in descendants if category.active]
        categories = [root] + [category for category in descendants if category != root]
        categories = sorted(
            categories,
            key=lambda category: len(
                self._category_components(root, category)))
        for category in categories:
            directory = self._category_directory(root, category)
            missing_is_delete = True
            if category.id not in seen_categories:
                if category.file_sync_path and category != root:
                    self._deactivate_category_tree(category)
                    continue
                os.makedirs(directory, exist_ok=True)
                self._set_category_path(category, directory)
                missing_is_delete = False
            self._reconcile_category(
                root, category, missing_is_delete=missing_is_delete)

    def _walk_directory(self, root, category, directory, seen_categories):
        seen_categories.add(category.id)
        self._set_category_path(category, directory)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except FileNotFoundError:
            return
        for item in children:
            if self._ignore_name(item.name):
                continue
            try:
                if item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    child_category = self._child_category(
                        category, self._decode_name(item.name), create=True)
                    self._walk_directory(
                        root, child_category, item.path, seen_categories)
                elif item.is_file(follow_symlinks=False):
                    self._sync_fs_file(category, item.path)
            except FileNotFoundError:
                continue

    def _reconcile_category(self, root, category, missing_is_delete):
        Attachment = Pool().get('ir.attachment')
        attachments = Attachment.search([
                ('categories', '=', category.id),
                ('active', '=', True),
                ('type', 'in', ['data', 'text']),
                ])
        for resource in attachments:
            if category.synchronized_root() == root:
                self._sync_db_resource(
                    category, resource, missing_is_delete=missing_is_delete)

    def _sync_fs_file(self, category, path):
        file_state = self._read_file(path)
        if not file_state:
            return
        data, digest, size, mtime_ns = file_state
        relative = self._relative(path)
        is_text = path.lower().endswith('.md')

        Entry = Pool().get('file.sync.entry')
        path_entries = Entry.search([
                ('category', '=', category.id),
                ('path', '=', relative),
                ])
        for entry in path_entries:
            resource = entry.resource
            if not resource:
                continue
            if not resource.active:
                logger.info(
                    "ignored filesystem file pending ERP deletion: %s",
                    path)
                return
            self._sync_existing(
                category, path, resource, entry, data, digest, size,
                mtime_ns)
            return

        resource = self._find_named_resource(category, path, is_text)
        if resource:
            entry = self._entry_for(category, resource)
            self._sync_existing(
                category, path, resource, entry, data, digest, size, mtime_ns)
            return

        entries = Entry.search([('digest', '=', digest)])
        for entry in entries:
            resource = entry.resource
            if (not resource or not resource.active
                    or (resource.type == 'text') != is_text):
                continue
            if entry.category.synchronized_root() != category.synchronized_root():
                continue
            if os.path.exists(self._entry_path(entry)):
                continue
            if entry.category != category:
                values = {
                    'categories': [
                        ('remove', [entry.category.id]),
                        ('add', [category.id]),
                        ],
                    }
                with Transaction().set_context(file_sync_skip=True):
                    resource.__class__.write([resource], values)
                entry.__class__.write([entry], {'category': category.id})
            self._rename_resource_from_path(resource, path)
            self._save_entry(
                entry, relative, digest, size, mtime_ns, data)
            logger.info(
                "moved ERP resource %s,%s to filesystem file %s",
                resource.__name__, resource.id, path)
            return

        resource = self._create_resource(category, path, data)
        entry = self._record_entry(
            category, resource, relative, digest, size, mtime_ns, data)
        self._link_inactive_versions(category, resource)
        return entry

    def _sync_existing(
            self, category, path, resource, entry, fs_data, fs_digest, fs_size,
            fs_mtime_ns):
        db_data = self._resource_data(resource)
        if db_data is None:
            return
        db_digest = self._digest(db_data)
        relative = self._relative(path)
        if not entry:
            if db_digest == fs_digest:
                self._record_entry(
                    category, resource, relative, fs_digest, fs_size, fs_mtime_ns,
                    fs_data)
            else:
                self._resolve_conflict(
                    category, path, resource, None, fs_data, fs_digest, fs_size,
                    fs_mtime_ns)
            return

        db_changed = db_digest != entry.digest
        fs_changed = fs_digest != entry.digest
        if db_digest == fs_digest:
            self._save_entry(
                entry, relative, fs_digest, fs_size, fs_mtime_ns, fs_data)
        elif db_changed and fs_changed:
            self._resolve_conflict(
                category, path, resource, entry, fs_data, fs_digest, fs_size,
                fs_mtime_ns)
        elif fs_changed:
            self._replace_resource_from_file(category, resource, path, fs_data)
            self._save_entry(
                entry, relative, fs_digest, fs_size, fs_mtime_ns, fs_data)
        else:
            mtime_ns = self._write_file(path, db_data)
            self._save_entry(
                entry, relative, db_digest, len(db_data), mtime_ns, db_data)

    def _sync_db_resource(self, category, resource, missing_is_delete):
        data = self._resource_data(resource)
        if data is None:
            return
        root = category.synchronized_root()
        if not root:
            return
        directory = self._category_directory(root, category)
        os.makedirs(directory, exist_ok=True)
        self._set_category_path(category, directory)
        path = os.path.join(directory, self._resource_filename(resource))
        relative = self._relative(path)
        digest = self._digest(data)
        entry = self._entry_for(category, resource)

        if entry and entry.path != relative:
            old_path = self._entry_path(entry)
            if os.path.isfile(old_path) and not os.path.islink(old_path):
                if not os.path.exists(path):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    os.replace(old_path, path)
                    logger.info(
                        "renamed filesystem file %s to %s", old_path, path)
                elif os.path.realpath(old_path) != os.path.realpath(path):
                    old_state = self._read_file(old_path)
                    if old_state and old_state[1] == entry.digest:
                        os.unlink(old_path)
                        logger.info(
                            "deleted duplicate filesystem file %s", old_path)
            entry.__class__.write([entry], {'path': relative})

        if os.path.isfile(path) and not os.path.islink(path):
            file_state = self._read_file(path)
            if file_state:
                self._sync_existing(
                    category, path, resource, entry, *file_state)
            return

        if entry and missing_is_delete:
            if digest == entry.digest:
                self._propagate_missing_entry(entry)
                return
            self._notify_conflict(category, relative)

        mtime_ns = self._write_file(path, data)
        if entry:
            self._save_entry(
                entry, relative, digest, len(data), mtime_ns, data)
        else:
            self._record_entry(
                category, resource, relative, digest, len(data), mtime_ns, data)

    def _propagate_missing_entry(self, entry):
        resource = entry.resource
        if resource and resource.active:
            self._remove_resource_category(resource, entry.category)
        self._delete_entry(entry)

    def _remove_entry_from_erp(self, entry):
        path = self._entry_path(entry)
        file_state = self._read_file(path)
        resource = entry.resource
        if file_state and file_state[1] != entry.digest:
            fs_data, fs_digest, fs_size, fs_mtime_ns = file_state
            conflict_path = self._conflict_path(path, 'filesystem')
            os.replace(path, conflict_path)
            replacement = self._create_resource(
                entry.category, conflict_path, fs_data)
            with Transaction().set_context(file_sync_skip=True):
                resource.__class__.write(
                    [resource], {'replaced_by': replacement.id})
            self._delete_entry(entry)
            self._record_entry(
                entry.category, replacement, self._relative(conflict_path),
                fs_digest, fs_size, fs_mtime_ns, fs_data)
            self._notify_conflict(entry.category, self._relative(conflict_path))
            return
        if file_state:
            os.unlink(path)
            logger.info("deleted filesystem file %s", path)
        self._delete_entry(entry)

    def _resolve_conflict(
            self, category, path, resource, entry, fs_data, fs_digest, fs_size,
            fs_mtime_ns):
        db_data = self._resource_data(resource)
        db_digest = self._digest(db_data)
        canonical_relative = self._relative(path)
        if (entry
                and self._merge_conflict(
                    category, path, resource, entry, db_data, fs_data)):
            return
        db_mtime_ns = self._resource_mtime_ns(resource)

        if fs_mtime_ns <= db_mtime_ns:
            conflict_path = self._conflict_path(path, 'filesystem')
            os.replace(path, conflict_path)
            older = self._create_resource(category, conflict_path, fs_data)
            with Transaction().set_context(file_sync_skip=True):
                older.__class__.write([older], {'replaced_by': resource.id})
            conflict_mtime = os.stat(conflict_path).st_mtime_ns
            self._record_entry(
                category, older, self._relative(conflict_path), fs_digest,
                fs_size, conflict_mtime, fs_data)
            canonical_mtime = self._write_file(path, db_data)
            if entry:
                self._save_entry(
                    entry, canonical_relative, db_digest, len(db_data),
                    canonical_mtime, db_data)
            else:
                self._record_entry(
                    category, resource, canonical_relative, db_digest,
                    len(db_data), canonical_mtime, db_data)
        else:
            conflict_path = self._conflict_path(path, 'erp')
            conflict_mtime = self._write_file(conflict_path, db_data)
            self._rename_resource_from_path(resource, conflict_path)
            replacement = self._create_resource(category, path, fs_data)
            with Transaction().set_context(file_sync_skip=True):
                resource.__class__.write(
                    [resource], {'replaced_by': replacement.id})
            if entry:
                self._save_entry(
                    entry, self._relative(conflict_path), db_digest,
                    len(db_data), conflict_mtime, db_data)
            else:
                self._record_entry(
                    category, resource, self._relative(conflict_path), db_digest,
                    len(db_data), conflict_mtime, db_data)
            self._record_entry(
                category, replacement, canonical_relative, fs_digest, fs_size,
                fs_mtime_ns, fs_data)
        self._notify_conflict(category, canonical_relative)

    def _merge_conflict(
            self, category, path, resource, entry, db_data, fs_data):
        base_data = entry.merge_base
        if base_data is None:
            return False
        base_data = bytes(base_data)
        if self._digest(base_data) != entry.digest:
            return False
        merged_data = self._merge_text_data(
            path, base_data, db_data, fs_data)
        if merged_data is None:
            return False

        replacement, filesystem_version = self._create_merged_versions(
            category, path, resource, merged_data, fs_data)
        merged_mtime_ns = self._write_file(path, merged_data)
        self._save_entry(
            entry, self._relative(path), self._digest(merged_data),
            len(merged_data), merged_mtime_ns, merged_data)
        logger.info(
            "merged ERP resource %s,%s and filesystem version %s,%s "
            "as %s,%s in %s",
            resource.__name__, resource.id,
            filesystem_version.__name__, filesystem_version.id,
            replacement.__name__, replacement.id, path)
        return True

    def _create_merged_versions(
            self, category, path, resource, merged_data, fs_data):
        entries = self._entries_for_resource(resource)
        replacement_defaults = self._resource_copy_defaults(
            category, resource, path, merged_data)
        with Transaction().set_context(file_sync_skip=True):
            replacement, = resource.__class__.copy(
                [resource], default=replacement_defaults)
            filesystem_defaults = self._resource_copy_defaults(
                category, resource, path, fs_data)
            filesystem_defaults.update({
                    'active': False,
                    'replaced_by': replacement.id,
                    })
            filesystem_version, = resource.__class__.copy(
                [resource], default=filesystem_defaults)
            if entries:
                entries[0].__class__.write(
                    entries, {'attachment': replacement.id})
            resource.__class__.write([resource], {
                    'active': False,
                    'replaced_by': replacement.id,
                    })
        return replacement, filesystem_version

    def _merge_text_data(self, path, base_data, db_data, fs_data):
        if any(self._merge_base_data(path, data) is None
                for data in (base_data, db_data, fs_data)):
            return
        base_lines = base_data.splitlines(keepends=True)
        db_lines = db_data.splitlines(keepends=True)
        fs_lines = fs_data.splitlines(keepends=True)
        merger = Merge3(base_lines, db_lines, fs_lines)
        regions = merger.reprocess_merge_regions(merger.merge_regions())
        merged = []
        for region in regions:
            kind = region[0]
            if kind == 'conflict':
                return
            start, end = region[1:3]
            if kind == 'unchanged':
                lines = base_lines
            elif kind in {'a', 'same'}:
                lines = db_lines
            else:
                lines = fs_lines
            merged.extend(lines[start:end])
        return b''.join(merged)

    def _create_resource(self, category, path, data):
        filename = os.path.basename(path)
        with Transaction().set_context(file_sync_skip=True):
            pool = Pool()
            Attachment = pool.get('ir.attachment')
            if filename.lower().endswith('.md'):
                Lang = pool.get('ir.lang')
                try:
                    content = data.decode('utf-8-sig')
                except UnicodeDecodeError as exception:
                    raise UserError(gettext(
                            'file_sync.msg_markdown_utf8', path=path)) from exception
                resource, = Attachment.create([{
                            'name': filename[:-3],
                            'type': 'text',
                            'content': content,
                            'language': Lang.get().id,
                            'unlinked': True,
                            'categories': [('add', [category.id])],
                            }])
            else:
                resource, = Attachment.create([{
                            'name': filename,
                            'type': 'data',
                            'data': data,
                            'unlinked': True,
                            'categories': [('add', [category.id])],
                            }])
        logger.info(
            "created ERP resource %s,%s from filesystem file %s",
            resource.__name__, resource.id, path)
        return resource

    def _replace_resource_from_file(self, category, resource, path, data):
        defaults = self._resource_copy_defaults(
            category, resource, path, data)
        entries = self._entries_for_resource(resource)
        with Transaction().set_context(file_sync_skip=True):
            replacement, = resource.__class__.copy(
                [resource], default=defaults)
            if entries:
                entries[0].__class__.write(
                    entries, {'attachment': replacement.id})
            resource.__class__.write([resource], {
                    'active': False,
                    'replaced_by': replacement.id,
                    })
        logger.info(
            "versioned ERP resource %s,%s as %s,%s from filesystem file %s",
            resource.__name__, resource.id,
            replacement.__name__, replacement.id, path)
        return replacement

    def _resource_copy_defaults(self, category, resource, path, data):
        defaults = {
            'active': True,
            'name': self._name_from_path(resource, path),
            'replaced_by': None,
            }
        if resource.type == 'text':
            try:
                defaults['content'] = data.decode('utf-8-sig')
            except UnicodeDecodeError as exception:
                raise UserError(gettext(
                        'file_sync.msg_markdown_utf8',
                        path=path)) from exception
        else:
            defaults.update({
                    'data': data,
                    'file_id': None,
                    'type': 'data',
                    })
        return defaults

    def _rename_resource_from_path(self, resource, path):
        name = self._name_from_path(resource, path)
        if resource.name != name:
            with Transaction().set_context(file_sync_skip=True):
                resource.__class__.write([resource], {'name': name})
            logger.info(
                "renamed ERP resource %s,%s from filesystem file %s",
                resource.__name__, resource.id, path)

    def _find_named_resource(self, category, path, is_text):
        name = os.path.basename(path)
        if is_text:
            name = name[:-3]
        Model = Pool().get('ir.attachment')
        name = self._decode_name(name)
        records = Model.search([
                ('categories', '=', category.id),
                ('name', '=', name),
                ('active', '=', True),
                ], order=[('id', 'DESC')], limit=1)
        return records[0] if records else None

    def _link_inactive_versions(self, category, replacement):
        Model = replacement.__class__
        with inactive_records():
            versions = Model.search([
                    ('categories', '=', category.id),
                    ('name', '=', replacement.name),
                    ('active', '=', False),
                    ('replaced_by', '=', None),
                    ('id', '!=', replacement.id),
                    ])
        if versions:
            with Transaction().set_context(file_sync_skip=True):
                Model.write(versions, {'replaced_by': replacement.id})

    def _remove_resource_category(self, resource, category):
        other_categories = [candidate for candidate in resource.categories
            if candidate != category]
        with Transaction().set_context(file_sync_skip=True):
            if other_categories:
                values = {'categories': [('remove', [category.id])]}
                resource.__class__.write([resource], values)
                logger.info(
                    "removed category %s from ERP resource %s,%s",
                    category.id, resource.__name__, resource.id)
            else:
                resource.__class__.write([resource], {'active': False})
                logger.info(
                    "deactivated ERP resource %s,%s",
                    resource.__name__, resource.id)

    def _deactivate_category_tree(self, category):
        Category = Pool().get('office.category')
        descendants = Category.search([('parent', 'child_of', [category.id])])
        categories = [category] + [child for child in descendants if child != category]
        categories = sorted(categories, key=lambda item: len(self._parents(item)),
            reverse=True)
        Entry = Pool().get('file.sync.entry')
        entries = Entry.search([('category', 'in', [item.id for item in categories])])
        for entry in entries:
            self._propagate_missing_entry(entry)
        with Transaction().set_context(file_sync_skip=True):
            Category.write(categories, {'active': False})

    def _child_category(self, parent, name, create):
        Category = Pool().get('office.category')
        categories = Category.search([
                ('parent', '=', parent.id),
                ('name', '=', name),
                ], limit=1)
        if categories:
            return categories[0]
        with inactive_records():
            categories = Category.search([
                    ('parent', '=', parent.id),
                    ('name', '=', name),
                    ], limit=1)
        if categories:
            with Transaction().set_context(file_sync_skip=True):
                Category.write(categories, {'active': True})
            logger.info(
                "reactivated ERP category %s from filesystem directory",
                categories[0].id)
            return categories[0]
        if not create:
            return
        with Transaction().set_context(file_sync_skip=True):
            category, = Category.create([{'name': name, 'parent': parent.id}])
        logger.info(
            "created ERP category %s from filesystem directory %s",
            category.id, name)
        return category

    def _category_for_directory(self, root, directory, create):
        root_directory = self._category_directory(root, root)
        try:
            relative = os.path.relpath(directory, root_directory)
        except ValueError:
            return
        if relative == '.':
            return root
        if relative == '..' or relative.startswith('..' + os.sep):
            return
        category = root
        for component in Path(relative).parts:
            category = self._child_category(
                category, self._decode_name(component), create=create)
            if not category:
                return
        return category

    def _root_for_path(self, path):
        Category = Pool().get('office.category')
        roots = Category.search([
                ('parent', '=', None),
                ('sync', '=', True),
                ('active', '=', True),
                ])
        matches = []
        for root in roots:
            directory = self._category_directory(root, root)
            try:
                if os.path.commonpath([directory, path]) == directory:
                    matches.append((len(directory), root))
            except ValueError:
                continue
        return max(matches, default=(0, None))[1]

    def _category_directory(self, root, category):
        return self._validated_path(os.path.join(
                self.base_path, *self._category_components(root, category)))

    def _category_components(self, root, category):
        categories = []
        current = category
        while current:
            categories.append(current)
            if current == root:
                break
            current = current.parent
        if not categories or categories[-1] != root:
            raise UserError(gettext('file_sync.msg_category_outside_root'))
        return [self._safe_component(item.name) for item in reversed(categories)]

    def _resource_filename(self, resource):
        name = self._safe_component(resource.name)
        if resource.type == 'text' and not name.lower().endswith('.md'):
            name += '.md'
        return name

    def _name_from_path(self, resource, path):
        name = os.path.basename(path)
        if resource.type == 'text' and name.lower().endswith('.md'):
            name = name[:-3]
        return self._decode_name(name)

    def _resource_data(self, resource):
        if resource.type == 'text':
            return (resource.content or '').encode('utf-8')
        if resource.type != 'data':
            return None
        data = resource.data
        if data is None:
            data = b''
        return bytes(data)

    def _resource_mtime_ns(self, resource):
        value = resource.write_date or resource.create_date
        if not value:
            return 0
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return int(value.timestamp() * 1_000_000_000)

    def _entries_for_resource(self, resource):
        Entry = Pool().get('file.sync.entry')
        return Entry.search([('attachment', '=', resource.id)])

    def _entry_for(self, category, resource):
        entries = [entry for entry in self._entries_for_resource(resource)
            if entry.category == category]
        return entries[0] if entries else None

    def _record_entry(
            self, category, resource, relative, digest, size, mtime_ns, data):
        Entry = Pool().get('file.sync.entry')
        values = {
            'category': category.id,
            'path': relative,
            'digest': digest,
            'merge_base': self._merge_base_data(relative, data),
            'size': size,
            'mtime_ns': mtime_ns,
            }
        values['attachment'] = resource.id
        entry, = Entry.create([values])
        return entry

    def _save_entry(
            self, entry, relative, digest, size, mtime_ns, data):
        values = {}
        for name, value in {
                'path': relative,
                'digest': digest,
                'merge_base': self._merge_base_data(relative, data),
                'size': size,
                'mtime_ns': mtime_ns,
                }.items():
            current = getattr(entry, name)
            if name == 'merge_base' and current is not None:
                current = bytes(current)
            if current != value:
                values[name] = value
        if values:
            entry.__class__.write([entry], values)

    @staticmethod
    def _merge_base_data(path, data):
        filename = os.path.basename(path).lower()
        extension = os.path.splitext(filename)[1]
        if (extension not in TEXT_EXTENSIONS
                and filename not in TEXT_FILENAMES):
            return
        if b'\0' in data:
            return
        try:
            data.decode('utf-8-sig')
        except UnicodeDecodeError:
            return
        return data

    def _delete_entry(self, entry):
        entry.__class__.delete([entry])

    def _set_category_path(self, category, directory):
        relative = self._relative(directory)
        if category.file_sync_path != relative:
            with Transaction().set_context(file_sync_skip=True):
                category.__class__.write([category], {'file_sync_path': relative})

    def _entry_path(self, entry):
        path = self._validated_path(os.path.join(
                self.base_path, entry.path))
        return path

    def _validated_path(self, path, allow_leaf_symlink=False):
        path = os.path.abspath(path)
        if os.path.commonpath([self.base_path, path]) != self.base_path:
            raise UserError(gettext('file_sync.msg_path_outside_root',
                    path=path))
        relative = os.path.relpath(path, self.base_path)
        current = self.base_path
        parts = Path(relative).parts if relative != '.' else ()
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            if (os.path.islink(current)
                    and not (allow_leaf_symlink
                        and index == len(parts) - 1)):
                raise UserError(gettext(
                        'file_sync.msg_symlink_path', path=current))
        return path

    def _relative(self, path):
        relative = os.path.relpath(path, self.base_path)
        if relative == '..' or relative.startswith('..' + os.sep):
            raise UserError(gettext('file_sync.msg_path_outside_root',
                    path=path))
        return Path(relative).as_posix()

    def _read_file(self, path):
        try:
            file_stat = os.lstat(path)
            if not stat.S_ISREG(file_stat.st_mode):
                return
            with open(path, 'rb') as handle:
                data = handle.read()
        except FileNotFoundError:
            return
        return data, self._digest(data), len(data), file_stat.st_mtime_ns

    def _write_file(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix='.file-sync-', dir=os.path.dirname(path))
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            manager = Transaction().join(FileSystemDataManager())
            manager.snapshot(path)
            os.replace(temporary, path)
            logger.info(
                "wrote filesystem file %s (%s bytes)", path, len(data))
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return os.stat(path).st_mtime_ns

    def _merge_directories(self, source, target, category):
        for name in sorted(os.listdir(source)):
            source_path = os.path.join(source, name)
            target_path = os.path.join(target, name)
            if not os.path.exists(target_path):
                os.replace(source_path, target_path)
            elif os.path.isdir(source_path) and os.path.isdir(target_path):
                self._merge_directories(source_path, target_path, category)
            elif os.path.isfile(source_path) and os.path.isfile(target_path):
                source_state = self._read_file(source_path)
                target_state = self._read_file(target_path)
                if (source_state and target_state
                        and source_state[1] == target_state[1]):
                    os.unlink(source_path)
                elif (source_state and target_state
                        and source_state[3] > target_state[3]):
                    conflict = self._conflict_path(
                        target_path, 'filesystem-directory')
                    os.replace(target_path, conflict)
                    os.replace(source_path, target_path)
                    self._notify_conflict(category, self._relative(target_path))
                else:
                    conflict = self._conflict_path(
                        target_path, 'erp-directory')
                    os.replace(source_path, conflict)
                    self._notify_conflict(category, self._relative(target_path))
            else:
                conflict = self._conflict_path(
                    target_path, 'filesystem-directory')
                os.replace(target_path, conflict)
                os.replace(source_path, target_path)
                self._notify_conflict(category, self._relative(target_path))
        try:
            os.rmdir(source)
        except OSError:
            pass

    def _conflict_path(self, path, source):
        stem, extension = os.path.splitext(path)
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            '%Y%m%dT%H%M%SZ')
        base = f'{stem}.conflict-resolve-{timestamp}-{source}'
        candidate = base + extension
        counter = 1
        while os.path.exists(candidate):
            candidate = f'{base}-{counter}{extension}'
            counter += 1
        return candidate

    def _notify_conflict(self, category, relative):
        logger.warning("filesystem synchronization conflict: %s", relative)
        Notification = Pool().get('res.notification')
        read_write, read_only = category.access_users()
        users = sorted(read_write | read_only, key=lambda user: user.id)
        values = []
        for user in users:
            if not user.active:
                continue
            values.append({
                    'user': user.id,
                    'icon': 'tryton-warning',
                    'label': gettext('file_sync.msg_conflict_label'),
                    'description': gettext(
                        'file_sync.msg_conflict_description', path=relative),
                    'model': 'office.category',
                    'records': json.dumps([category.id]),
                    })
        if values:
            Notification.create(values)

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    def _ignore_name(self, name):
        return any(fnmatch.fnmatchcase(name, pattern)
            for pattern in self.ignore_patterns)

    def is_ignored_path(self, path):
        if not self.base_path:
            return False
        try:
            relative = os.path.relpath(
                os.path.abspath(path), self.base_path)
        except ValueError:
            return False
        if relative == '..' or relative.startswith('..' + os.sep):
            return False
        return any(self._ignore_name(component)
            for component in Path(relative).parts)

    def _safe_component(self, name):
        if (not name or name in {'.', '..'} or '\x00' in name
                or self._ignore_name(name)):
            raise UserError(gettext(
                    'file_sync.msg_invalid_filename', name=name))
        return self._encode_name(name)

    @staticmethod
    def _encode_name(name):
        return (name
            .replace('%', '%25')
            .replace('/', '%2F')
            .replace('\\', '%5C'))

    @staticmethod
    def _decode_name(name):
        return (name
            .replace('%2F', '/')
            .replace('%5C', '\\')
            .replace('%25', '%'))

    @staticmethod
    def _parents(category):
        parents = []
        while category.parent:
            category = category.parent
            parents.append(category)
        return parents
