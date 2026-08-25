import os
import tempfile
import unittest
from pathlib import Path

from proteus import Model
import trytond.config as tryton_config
from trytond.pool import Pool
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestBidirectionalSync(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        had_section = tryton_config.has_section('file_sync')
        old_path = tryton_config.get('file_sync', 'path')
        old_worker = tryton_config.get('queue', 'worker')
        if not had_section:
            tryton_config.add_section('file_sync')

        with tempfile.TemporaryDirectory() as directory:
            tryton_config.set('file_sync', 'path', directory)
            tryton_config.set('queue', 'worker', 'True')
            imported_path = Path(directory) / 'Shared' / 'Imported'
            imported_path.mkdir(parents=True)
            text_path = imported_path / 'guide.md'
            text_path.write_text('Initial text', encoding='utf-8')
            binary_path = imported_path / 'data.bin'
            binary_path.write_bytes(b'initial')

            config = activate_modules('file_sync')
            Attachment = Model.get('ir.attachment', config=config)
            Category = Model.get('office.category', config=config)

            def run_sync_tasks():
                count = 0
                Queue = Pool(config.database_name).get('ir.queue')
                while True:
                    with Transaction().start(
                            config.database_name, config.user,
                            context=config.context) as transaction:
                        tasks = Queue.search([
                                ('name', '=', 'default'),
                                ('finished_at', '=', None),
                                ], order=[('id', 'ASC')])
                        task = next((task for task in tasks
                                if task.data['model'] in {
                                    'file.sync.configuration',
                                    'ir.attachment'}
                                and task.data['method'] in {
                                    'delete_inactive', 'synchronize',
                                    'synchronize_files'}), None)
                        if not task:
                            return count
                        task_id = task.id
                        transaction.commit()
                    with Transaction().start(
                            config.database_name, config.user,
                            context=config.context,
                            _lock_records={Queue._table: [task_id]},
                            ) as transaction:
                        task = Queue(task_id)
                        task.run()
                        transaction.commit()
                        count += 1

            root = Category(name='Shared', sync=True)
            root.save()
            self.assertEqual(run_sync_tasks(), 1)

            imported, = Category.find([('name', '=', 'Imported')])
            text, = Attachment.find([
                    ('name', '=', 'guide'),
                    ('type', '=', 'text'),
                    ])
            binary, = Attachment.find([
                    ('name', '=', 'data.bin'),
                    ('type', '=', 'data'),
                    ])
            self.assertEqual(text.content, 'Initial text')
            self.assertEqual(bytes(binary.data), b'initial')
            self.assertTrue(text.unlinked)
            self.assertTrue(binary.unlinked)
            self.assertEqual(
                text.resource.__class__.__name__, 'office.unlinked')
            self.assertEqual(
                [category.id for category in text.categories], [imported.id])

            old_text_id = text.id
            text_path.write_text('Filesystem version', encoding='utf-8')
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(text_path))
                transaction.commit()
            text, = Attachment.find([
                    ('name', '=', 'guide'),
                    ('type', '=', 'text'),
                    ])
            self.assertNotEqual(text.id, old_text_id)
            self.assertEqual(text.content, 'Filesystem version')
            with config.set_context(active_test=False):
                old_text = Attachment(old_text_id)
                self.assertFalse(old_text.active)
                self.assertEqual(old_text.replaced_by.id, text.id)

            archive_path = Path(directory) / 'Shared' / 'Archive'
            archive_path.mkdir()
            moved_path = archive_path / 'data.bin'
            os.replace(binary_path, moved_path)
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(moved_path))
                Entry.synchronize_path(str(binary_path))
                transaction.commit()
            binary.reload()
            archive, = Category.find([('name', '=', 'Archive')])
            self.assertEqual(
                [category.id for category in binary.categories], [archive.id])
            self.assertTrue(binary.unlinked)
            self.assertEqual(
                binary.resource.__class__.__name__, 'office.unlinked')

        if old_path is not None:
            tryton_config.set('file_sync', 'path', old_path)
        elif tryton_config.has_section('file_sync'):
            tryton_config.set('file_sync', 'path', '')
        tryton_config.set('queue', 'worker', old_worker)
