import errno
import os
from queue import Empty, Queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from proteus import Model
import trytond.config as tryton_config
from trytond.pool import Pool
from trytond.pyson import Bool, Eval
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from trytond.modules.file_sync.watcher import create_observer


class TestBidirectionalSync(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def run_file_sync_tasks(self, config):
        count = 0
        while True:
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Queue = Pool(config.database_name).get('ir.queue')
                tasks = Queue.search([
                        ('name', '=', 'default'),
                        ('finished_at', '=', None),
                        ], order=[('id', 'ASC')])
                task = next((task for task in tasks
                        if task.data['model'] in {
                            'brainbow.document', 'file.sync.configuration',
                            'ir.attachment'}
                        and task.data['method'] in {
                            'delete_inactive', 'synchronize',
                            'synchronize_files'}), None)
                if not task:
                    transaction.commit()
                    return count
                task.run()
                transaction.commit()
                count += 1

    def test(self):
        had_section = tryton_config.has_section('file_sync')
        old_path = tryton_config.get('file_sync', 'path')
        old_worker = tryton_config.get('queue', 'worker')
        if not had_section:
            tryton_config.add_section('file_sync')

        with tempfile.TemporaryDirectory() as directory:
            tryton_config.set('file_sync', 'path', directory)
            tryton_config.set('queue', 'worker', 'True')
            initial_directory = Path(directory) / 'Shared' / 'Imported'
            initial_directory.mkdir(parents=True)
            (initial_directory / 'preexisting.md').write_text(
                'Created before the ERP tag', encoding='utf-8')
            (initial_directory / 'preexisting.bin').write_bytes(b'initial')
            root_document_path = Path(directory) / 'Shared' / 'root-file.md'
            root_document_path.write_text(
                'Created in the synchronized root', encoding='utf-8')

            config = activate_modules('file_sync')
            Tag = Model.get('brainbow.tag', config=config)
            Document = Model.get('brainbow.document', config=config)
            Attachment = Model.get('ir.attachment', config=config)
            Lang = Model.get('ir.lang', config=config)
            Notification = Model.get('res.notification', config=config)
            User = Model.get('res.user', config=config)

            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                ServerTag = Pool(config.database_name).get('brainbow.tag')
                self.assertEqual(
                    ServerTag.sync.states['invisible'].pyson(),
                    Bool(Eval('parent')).pyson())
                self.assertEqual(
                    ServerTag.view.states['invisible'].pyson(),
                    Bool(Eval('sync')).pyson())
                transaction.commit()

            language, = Lang.find([('code', '=', 'en')])
            root = Tag(name='Shared', sync=True, view=True)
            root.read_write_users.append(User(config.user))
            root.save()
            self.assertFalse(Document.find([('name', '=', 'root-file')]))
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            root.reload()
            self.assertTrue(root.sync)
            self.assertFalse(root.view)
            root_document, = Document.find([('name', '=', 'root-file')])
            self.assertEqual([tag.id for tag in root_document.tags], [root.id])
            self.assertEqual(
                root_document.text, 'Created in the synchronized root')
            root.view = True
            root.save()
            root.reload()
            self.assertTrue(root.sync)
            self.assertFalse(root.view)

            imported_tag, = Tag.find([('name', '=', 'Imported')])
            imported_document, = Document.find([
                    ('name', '=', 'preexisting'),
                    ])
            imported_attachment, = Attachment.find([
                    ('name', '=', 'preexisting.bin'),
                    ])
            self.assertEqual(imported_document.text,
                'Created before the ERP tag')
            self.assertEqual(bytes(imported_attachment.data), b'initial')
            self.assertEqual(imported_tag.parent.id, root.id)
            self.assertEqual(imported_attachment.resource.id, imported_tag.id)
            self.assertEqual(
                imported_attachment.resource.__class__.__name__,
                'brainbow.tag')

            probe_path = initial_directory / 'watchdog-probe.bin'
            events = Queue()
            SyncEntry = Pool(config.database_name).get('file.sync.entry')
            synchronizer = SyncEntry.get_synchronizer(required=True)
            self.assertFalse(synchronizer.is_ignored_path(
                    initial_directory / '.stfolder'))
            with patch.object(
                    Observer, 'start',
                    side_effect=OSError(errno.EMFILE, 'Too many open files')), \
                    patch(
                        'trytond.modules.file_sync.watcher.POLL_INTERVAL',
                        0.05):
                observer = create_observer(
                    directory, events, timeout=0.05,
                    ignore=synchronizer.is_ignored_path)
            try:
                self.assertIsInstance(observer, PollingObserver)
                ignored_directory = (
                    initial_directory / '.file-sync-test')
                ignored_directory.mkdir()
                ignored_file = ignored_directory / 'ignored.txt'
                ignored_file.write_text('Ignored marker', encoding='utf-8')
                probe_path.write_bytes(b'probe')
                received = []
                while (str(probe_path), 'creation') not in received:
                    try:
                        received.append(events.get(timeout=1))
                    except Empty:
                        break
                self.assertIn((str(probe_path), 'creation'), received)
                self.assertFalse(any(
                        '.file-sync-test' in Path(path).parts
                        for path, _ in received))
            finally:
                observer.stop()
                observer.join()
            probe_path.unlink()

            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                synchronizer.synchronize_path(str(ignored_directory))
                synchronizer.synchronize_path(str(ignored_file))
                transaction.commit()
            self.assertFalse(Tag.find([('name', '=', '.file-sync-test')]))
            self.assertFalse(Attachment.find([
                        ('name', '=', 'ignored.txt'),
                        ]))

            imported_tag.active = False
            imported_tag.save()
            self.assertTrue(initial_directory.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertFalse(initial_directory.exists())
            imported_tag.active = True
            imported_tag.save()
            self.assertFalse(initial_directory.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertEqual(
                (initial_directory / 'preexisting.md').read_text(
                    encoding='utf-8'),
                'Created before the ERP tag')
            self.assertEqual(
                (initial_directory / 'preexisting.bin').read_bytes(),
                b'initial')

            project = Tag(name='Projects', parent=root, sync=True)
            project.save()
            project_path = Path(directory) / 'Shared' / 'Projects'
            self.assertFalse(project_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertTrue(project_path.exists())
            project.reload()
            self.assertFalse(project.sync)
            document = Document(
                name='Guide', text='Initial document', language=language)
            document.tags.append(project)
            document.save()
            document_path = Path(directory) / 'Shared' / 'Projects' / 'Guide.md'
            self.assertFalse(document_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertEqual(document_path.read_text(encoding='utf-8'),
                'Initial document')
            self.assertFalse(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ]))
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                self.assertEqual(
                    Entry._fields['mtime_ns']._sql_type, 'BIGINT')
                self.assertEqual(Entry._fields['size']._sql_type, 'BIGINT')
                entry, = Entry.search([('document', '=', document.id)])
                self.assertGreater(entry.mtime_ns, 2 ** 31 - 1)
                transaction.commit()

            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context):
                    ServerDocument = Pool(config.database_name).get(
                        'brainbow.document')
                    ServerDocument.write(
                        [ServerDocument(document.id)],
                        {'text': 'Uncommitted update'})
                    self.assertEqual(
                        document_path.read_text(encoding='utf-8'),
                        'Initial document')
                    raise RuntimeError('rollback')
            self.assertEqual(self.run_file_sync_tasks(config), 0)
            self.assertEqual(document_path.read_text(encoding='utf-8'),
                'Initial document')
            document.reload()
            self.assertEqual(document.text, 'Initial document')

            interrupted_path = (
                Path(directory) / 'Shared' / 'Projects' / 'Interrupted.md')
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context):
                    ServerDocument = Pool(config.database_name).get(
                        'brainbow.document')
                    ServerDocument.create([{
                                'name': 'Interrupted',
                                'text': 'Uncommitted document',
                                'language': language.id,
                                'tags': [('add', [project.id])],
                                }])
                    self.assertFalse(interrupted_path.exists())
                    raise RuntimeError('rollback')
            self.assertEqual(self.run_file_sync_tasks(config), 0)
            self.assertFalse(interrupted_path.exists())

            committed_document = Document(
                name='Interrupted', text='Committed document',
                language=language)
            committed_document.tags.append(Tag(project.id))
            committed_document.save()
            self.assertFalse(interrupted_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertEqual(
                interrupted_path.read_text(encoding='utf-8'),
                'Committed document')
            self.assertFalse(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ]))

            attachment = Attachment(
                name='specification.bin', type='data', data=b'original',
                resource=project)
            attachment.tags.append(Tag(project.id))
            attachment.save()
            attachment_path = (
                Path(directory) / 'Shared' / 'Projects'
                / 'specification.bin')
            self.assertFalse(attachment_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertEqual(attachment_path.read_bytes(), b'original')

            text_base = b'Title\nCommon\nFooter\n'
            text_attachment = Attachment(
                name='notes.txt', type='data', data=text_base,
                resource=project)
            text_attachment.tags.append(Tag(project.id))
            text_attachment.save()
            text_path = (
                Path(directory) / 'Shared' / 'Projects' / 'notes.txt')
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertEqual(text_path.read_bytes(), text_base)
            text_attachment_id = text_attachment.id
            erp_text = b'ERP title\nCommon\nFooter\n'
            filesystem_text = b'Title\nCommon\nFilesystem footer\n'
            merged_text = b'ERP title\nCommon\nFilesystem footer\n'
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                ServerAttachment = Pool(config.database_name).get(
                    'ir.attachment')
                with Transaction().set_context(file_sync_skip=True):
                    ServerAttachment.write(
                        [ServerAttachment(text_attachment_id)],
                        {'data': erp_text})
                transaction.commit()
            text_path.write_bytes(filesystem_text)
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(text_path))
                transaction.commit()
            merged_attachment, = Attachment.find([
                    ('name', '=', 'notes.txt'),
                    ])
            self.assertNotEqual(merged_attachment.id, text_attachment_id)
            self.assertEqual(bytes(merged_attachment.data), merged_text)
            self.assertEqual(text_path.read_bytes(), merged_text)
            self.assertEqual(merged_attachment.resource.id, project.id)
            with config.set_context(active_test=False):
                text_versions = Attachment.find([
                        ('name', '=', 'notes.txt'),
                        ('active', '=', False),
                        ('replaced_by', '=', merged_attachment.id),
                        ])
                self.assertEqual(len(text_versions), 2)
                self.assertEqual(
                    {bytes(version.data) for version in text_versions},
                    {erp_text, filesystem_text})
            self.assertFalse(list(text_path.parent.glob(
                        'notes.conflict-resolve-*.txt')))
            self.assertFalse(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ]))

            attachment_id = attachment.id
            attachment_path.write_bytes(b'overwritten on the filesystem')
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(attachment_path))
                transaction.commit()
            attachment, = Attachment.find([
                    ('name', '=', 'specification.bin'),
                    ])
            self.assertNotEqual(attachment.id, attachment_id)
            self.assertEqual(
                bytes(attachment.data), b'overwritten on the filesystem')
            self.assertEqual(attachment.resource.id, project.id)
            with config.set_context(active_test=False):
                previous_attachment = Attachment(attachment_id)
                self.assertFalse(previous_attachment.active)
                self.assertEqual(bytes(previous_attachment.data), b'original')
                self.assertEqual(
                    previous_attachment.replaced_by.id, attachment.id)
                Attachment.delete([previous_attachment])
                self.assertFalse(Attachment.find([
                            ('id', '=', attachment_id),
                            ]))

            project_identity = project.id
            renamed_project = Tag(project.id)
            renamed_project.name = 'Work'
            renamed_project.save()
            old_project_path = Path(directory) / 'Shared' / 'Projects'
            work_path = Path(directory) / 'Shared' / 'Work'
            self.assertEqual(renamed_project.id, project_identity)
            self.assertTrue(old_project_path.exists())
            self.assertFalse(work_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertFalse(old_project_path.exists())
            document_path = work_path / 'Guide.md'
            attachment_path = work_path / 'specification.bin'
            self.assertEqual(document_path.read_text(encoding='utf-8'),
                'Initial document')
            self.assertEqual(
                attachment_path.read_bytes(), b'overwritten on the filesystem')
            project = renamed_project

            document_path.write_text(
                'Changed on the filesystem', encoding='utf-8')
            document_id = document.id
            with self.assertLogs(
                    'trytond.modules.file_sync.sync', level='INFO') as logs:
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context) as transaction:
                    Entry = Pool(config.database_name).get('file.sync.entry')
                    Entry.synchronize_path(str(document_path))
                    transaction.commit()
            log_output = '\n'.join(logs.output)
            self.assertIn('versioned ERP resource brainbow.document', log_output)
            self.assertIn(str(document_path), log_output)
            document, = Document.find([('name', '=', 'Guide')])
            self.assertNotEqual(document.id, document_id)
            self.assertEqual(document.text, 'Changed on the filesystem')
            with config.set_context(active_test=False):
                previous_document = Document(document_id)
                self.assertFalse(previous_document.active)
                self.assertEqual(previous_document.text, 'Initial document')
                self.assertEqual(previous_document.replaced_by.id, document.id)
            self.assertFalse(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ]))

            attachment.name = 'renamed.bin'
            attachment.data = b'changed in the ERP'
            attachment.save()
            renamed_path = (
                Path(directory) / 'Shared' / 'Work' / 'renamed.bin')
            self.assertTrue(attachment_path.exists())
            self.assertFalse(renamed_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertFalse(attachment_path.exists())
            self.assertEqual(renamed_path.read_bytes(), b'changed in the ERP')

            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                ServerAttachment = Pool(config.database_name).get(
                    'ir.attachment')
                with Transaction().set_context(file_sync_skip=True):
                    ServerAttachment.write(
                        [ServerAttachment(attachment.id)],
                        {'data': b'newer ERP attachment'})
                transaction.commit()
            renamed_path.write_bytes(b'older filesystem attachment')
            os.utime(renamed_path, ns=(1_000_000_000, 1_000_000_000))
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(renamed_path))
                transaction.commit()
            attachment.reload()
            self.assertEqual(bytes(attachment.data), b'newer ERP attachment')
            self.assertEqual(renamed_path.read_bytes(),
                b'newer ERP attachment')
            attachment_conflicts = list(renamed_path.parent.glob(
                    'renamed.conflict-resolve-*-filesystem.bin'))
            self.assertEqual(len(attachment_conflicts), 1)
            self.assertEqual(attachment_conflicts[0].read_bytes(),
                b'older filesystem attachment')

            archive_path = Path(directory) / 'Shared' / 'Archive'
            archive_path.mkdir()
            moved_path = archive_path / 'renamed.bin'
            attachment_identity = attachment.id
            os.replace(renamed_path, moved_path)
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(moved_path))
                Entry.synchronize_path(str(renamed_path))
                transaction.commit()
            attachment.reload()
            archive, = Tag.find([('name', '=', 'Archive')])
            self.assertEqual(attachment.id, attachment_identity)
            self.assertEqual([tag.id for tag in attachment.tags], [archive.id])
            self.assertEqual(attachment.resource.id, archive.id)
            self.assertEqual(moved_path.read_bytes(), b'newer ERP attachment')

            document.text = 'Title\nCommon\nFooter\n'
            document.save()
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            erp_document_text = 'ERP title\nCommon\nFooter\n'
            filesystem_document_text = (
                'Title\nCommon\nFilesystem footer\n')
            merged_document_text = (
                'ERP title\nCommon\nFilesystem footer\n')
            notifications_before_merge = len(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ]))
            document_id = document.id
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                ServerDocument = Pool(config.database_name).get(
                    'brainbow.document')
                with Transaction().set_context(file_sync_skip=True):
                    ServerDocument.write(
                        [ServerDocument(document_id)],
                        {'text': erp_document_text})
                transaction.commit()
            document_path.write_text(
                filesystem_document_text, encoding='utf-8')
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(document_path))
                transaction.commit()
            document, = Document.find([('name', '=', 'Guide')])
            self.assertNotEqual(document.id, document_id)
            self.assertEqual(document.text, merged_document_text)
            self.assertEqual(
                document_path.read_text(encoding='utf-8'),
                merged_document_text)
            with config.set_context(active_test=False):
                document_versions = Document.find([
                        ('name', '=', 'Guide'),
                        ('active', '=', False),
                        ('replaced_by', '=', document.id),
                        ])
                self.assertEqual(len(document_versions), 2)
                self.assertEqual(
                    {version.text for version in document_versions},
                    {erp_document_text, filesystem_document_text})
            self.assertFalse(list(document_path.parent.glob(
                        'Guide.conflict-resolve-*.md')))
            self.assertEqual(len(Notification.find([
                        ('label', '=', 'File synchronization conflict'),
                        ])), notifications_before_merge)

            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                ServerDocument = Pool(config.database_name).get(
                    'brainbow.document')
                with Transaction().set_context(file_sync_skip=True):
                    ServerDocument.write(
                        [ServerDocument(document.id)],
                        {'text': 'Concurrent ERP version'})
                transaction.commit()
            document_path.write_text(
                'Concurrent filesystem version', encoding='utf-8')
            future = time.time_ns() + 2_000_000_000
            os.utime(document_path, ns=(future, future))
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(document_path))
                transaction.commit()

            guide, = Document.find([('name', '=', 'Guide')])
            self.assertEqual(guide.text, 'Concurrent filesystem version')
            conflict_paths = list(document_path.parent.glob(
                    'Guide.conflict-resolve-*-erp.md'))
            self.assertEqual(len(conflict_paths), 1)
            self.assertEqual(
                conflict_paths[0].read_text(encoding='utf-8'),
                'Concurrent ERP version')
            self.assertTrue(Notification.find([
                        ('user', '=', config.user),
                        ('label', '=', 'File synchronization conflict'),
                        ]))

            attachment_id = attachment.id
            Attachment.delete([attachment])
            self.assertTrue(moved_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertFalse(moved_path.exists())
            moved_path.write_bytes(b'replacement attachment')
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(moved_path))
                transaction.commit()
            replacement_attachment, = Attachment.find([
                    ('name', '=', 'renamed.bin'),
                    ])
            self.assertEqual(bytes(replacement_attachment.data),
                b'replacement attachment')
            self.assertEqual(replacement_attachment.resource.id, archive.id)
            with config.set_context(active_test=False):
                old_attachment = Attachment(attachment_id)
                self.assertEqual(
                    old_attachment.replaced_by.id, replacement_attachment.id)

            guide_id = guide.id
            Document.delete([guide])
            self.assertTrue(document_path.exists())
            self.assertEqual(self.run_file_sync_tasks(config), 1)
            self.assertFalse(document_path.exists())
            document_path.write_text('Replacement document', encoding='utf-8')
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(document_path))
                transaction.commit()
            replacement_document, = Document.find([('name', '=', 'Guide')])
            self.assertEqual(replacement_document.text, 'Replacement document')
            with config.set_context(active_test=False):
                old_document = Document(guide_id)
                self.assertEqual(
                    old_document.replaced_by.id, replacement_document.id)

        if old_path is not None:
            tryton_config.set('file_sync', 'path', old_path)
        elif tryton_config.has_section('file_sync'):
            tryton_config.set('file_sync', 'path', '')
        tryton_config.set('queue', 'worker', old_worker)
