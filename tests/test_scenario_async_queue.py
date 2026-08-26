import json
import shutil
import tempfile
import unittest
from pathlib import Path

from proteus import Model
import trytond.config as tryton_config
from trytond.exceptions import UserError
from trytond.pool import Pool
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestAsyncQueue(unittest.TestCase):

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
            config = activate_modules('file_sync')
            Attachment = Model.get('ir.attachment', config=config)
            Lang = Model.get('ir.lang', config=config)
            Category = Model.get('office.category', config=config)

            def run_task(model, method):
                Queue = Pool(config.database_name).get('ir.queue')
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context) as transaction:
                    tasks = Queue.search([
                            ('name', '=', 'default'),
                            ('finished_at', '=', None),
                            ], order=[('id', 'ASC')])
                    task = next((task for task in tasks
                            if task.data['model'] == model
                            and task.data['method'] == method), None)
                    self.assertIsNotNone(task)
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

            language, = Lang.find([('code', '=', 'en')])
            root = Category(name='Shared', sync=True)
            root.save()
            run_task('file.sync.configuration', 'synchronize')
            root_path = Path(directory) / 'Shared'
            marker_path = Path(directory) / '.tryton-file-sync'
            self.assertTrue(root_path.is_dir())
            self.assertTrue(marker_path.is_file())

            marker_data = marker_path.read_text(encoding='utf-8')
            invalid_marker = json.loads(marker_data)
            invalid_marker['filesystem_id'] = 'another-database'
            marker_path.write_text(
                json.dumps(invalid_marker), encoding='utf-8')
            with self.assertRaises(UserError):
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context):
                    Entry = Pool(config.database_name).get('file.sync.entry')
                    Entry.synchronize()
            marker_path.write_text(marker_data, encoding='utf-8')

            project = Category(name='Projects', parent=root)
            project.save()
            run_task('file.sync.configuration', 'synchronize')
            project_path = root_path / 'Projects'

            text = Attachment(
                name='Guide', type='text', content='Initial text',
                language=language, unlinked=True)
            text.categories.append(project)
            text.save()
            text_path = root_path / 'Projects' / 'Guide.md'
            self.assertFalse(text_path.exists())
            run_task('ir.attachment', 'synchronize_files')
            self.assertEqual(
                text_path.read_text(encoding='utf-8'), 'Initial text')

            binary = Attachment(
                name='data.bin', type='data', data=b'initial',
                unlinked=True)
            binary.categories.append(Category(project.id))
            binary.save()
            binary_path = root_path / 'Projects' / 'data.bin'
            run_task('ir.attachment', 'synchronize_files')
            self.assertEqual(binary_path.read_bytes(), b'initial')

            directory_binary = Attachment(
                name='directory.bin', type='data', data=b'directory',
                unlinked=True)
            directory_binary.categories.append(Category(project.id))
            directory_binary.save()
            directory_binary_path = project_path / 'directory.bin'
            run_task('ir.attachment', 'synchronize_files')
            self.assertEqual(
                directory_binary_path.read_bytes(), b'directory')

            marker_path.unlink()
            shutil.rmtree(project_path)
            root.click('synchronize_files')
            self.assertFalse(project_path.exists())
            run_task('file.sync.configuration', 'synchronize')
            self.assertTrue(marker_path.is_file())
            self.assertTrue(project_path.is_dir())
            self.assertEqual(
                text_path.read_text(encoding='utf-8'), 'Initial text')
            self.assertEqual(binary_path.read_bytes(), b'initial')
            self.assertEqual(
                directory_binary_path.read_bytes(), b'directory')
            project.reload()
            text.reload()
            binary.reload()
            directory_binary.reload()
            self.assertTrue(project.active)
            self.assertTrue(text.active)
            self.assertTrue(binary.active)
            self.assertTrue(directory_binary.active)

            binary_path.unlink()
            root.click('synchronize_files')
            run_task('file.sync.configuration', 'synchronize')
            project.reload()
            directory_binary.reload()
            self.assertTrue(project.active)
            self.assertTrue(directory_binary.active)
            with config.set_context(active_test=False):
                binary = Attachment(binary.id)
                self.assertFalse(binary.active)

            text.content = 'Updated text'
            text.save()
            self.assertEqual(
                text_path.read_text(encoding='utf-8'), 'Initial text')
            run_task('ir.attachment', 'synchronize_files')
            self.assertEqual(
                text_path.read_text(encoding='utf-8'), 'Updated text')

            Attachment.delete([text])
            self.assertTrue(text_path.exists())
            run_task('ir.attachment', 'synchronize_files')
            self.assertFalse(text_path.exists())
            with config.set_context(active_test=False):
                inactive = Attachment(text.id)
                self.assertFalse(inactive.active)
                Attachment.delete([inactive])
                self.assertFalse(Attachment.find([('id', '=', text.id)]))

            shutil.rmtree(project_path)
            root.click('synchronize_files')
            run_task('file.sync.configuration', 'synchronize')
            with config.set_context(active_test=False):
                project = Category(project.id)
                directory_binary = Attachment(directory_binary.id)
                self.assertFalse(project.active)
                self.assertFalse(directory_binary.active)

        if old_path is not None:
            tryton_config.set('file_sync', 'path', old_path)
        elif tryton_config.has_section('file_sync'):
            tryton_config.set('file_sync', 'path', '')
        tryton_config.set('queue', 'worker', old_worker)
