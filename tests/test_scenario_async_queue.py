import tempfile
import unittest
from pathlib import Path

from proteus import Model
import trytond.config as tryton_config
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
            Tag = Model.get('brainbow.tag', config=config)
            Document = Model.get('brainbow.document', config=config)
            Attachment = Model.get('ir.attachment', config=config)
            Lang = Model.get('ir.lang', config=config)

            def run_task(expected_model, expected_method='synchronize_files'):
                with Transaction().start(
                        config.database_name, config.user,
                        context=config.context) as transaction:
                    Queue = Pool(config.database_name).get('ir.queue')
                    tasks = Queue.search([
                            ('name', '=', 'default'),
                            ('finished_at', '=', None),
                            ], order=[('id', 'ASC')])
                    task = next((task for task in tasks
                            if task.data['model'] == expected_model
                            and task.data['method'] == expected_method), None)
                    self.assertIsNotNone(task)
                    if expected_method == 'synchronize_files':
                        with self.assertLogs(
                                'trytond.modules.file_sync.sync',
                                level='INFO') as logs:
                            task.run()
                        output = '\n'.join(logs.output)
                    else:
                        task.run()
                        output = ''
                    transaction.commit()
                return output

            language, = Lang.find([('code', '=', 'en')])
            root = Tag(name='Shared', sync=True)
            root.save()
            project = Tag(name='Projects', parent=root)
            project.save()

            document = Document(
                name='Guide', text='Initial document', language=language)
            document.tags.append(project)
            document.save()
            document_path = (
                Path(directory) / 'Shared' / 'Projects' / 'Guide.md')
            self.assertFalse(document_path.exists())
            logs = run_task('brainbow.document')
            self.assertIn('wrote filesystem file', logs)
            self.assertIn(str(document_path), logs)
            self.assertEqual(
                document_path.read_text(encoding='utf-8'),
                'Initial document')

            document.text = 'Updated document'
            document.save()
            self.assertEqual(
                document_path.read_text(encoding='utf-8'),
                'Initial document')
            run_task('brainbow.document')
            self.assertEqual(
                document_path.read_text(encoding='utf-8'),
                'Updated document')

            document.name = 'Manual'
            document.save()
            renamed_document_path = (
                Path(directory) / 'Shared' / 'Projects' / 'Manual.md')
            self.assertTrue(document_path.exists())
            self.assertFalse(renamed_document_path.exists())
            logs = run_task('brainbow.document')
            self.assertIn('renamed filesystem file', logs)
            self.assertFalse(document_path.exists())
            self.assertTrue(renamed_document_path.exists())
            document_path = renamed_document_path

            attachment = Attachment(
                name='data.bin', type='data', data=b'initial',
                resource=project)
            attachment.tags.append(Tag(project.id))
            attachment.save()
            self.assertEqual(attachment.resource.id, project.id)
            attachment_path = (
                Path(directory) / 'Shared' / 'Projects' / 'data.bin')
            self.assertFalse(attachment_path.exists())
            run_task('ir.attachment')
            self.assertEqual(attachment_path.read_bytes(), b'initial')

            attachment.data = b'updated'
            attachment.save()
            self.assertEqual(attachment_path.read_bytes(), b'initial')
            run_task('ir.attachment')
            self.assertEqual(attachment_path.read_bytes(), b'updated')

            attachment.name = 'renamed.bin'
            attachment.save()
            renamed_attachment_path = (
                Path(directory) / 'Shared' / 'Projects' / 'renamed.bin')
            self.assertTrue(attachment_path.exists())
            self.assertFalse(renamed_attachment_path.exists())
            logs = run_task('ir.attachment')
            self.assertIn('renamed filesystem file', logs)
            self.assertFalse(attachment_path.exists())
            self.assertTrue(renamed_attachment_path.exists())
            attachment_path = renamed_attachment_path

            Document.delete([document])
            self.assertTrue(document_path.exists())
            run_task('brainbow.document')
            self.assertFalse(document_path.exists())
            with config.set_context(active_test=False):
                inactive_document = Document(document.id)
                Document.delete([inactive_document])
                self.assertFalse(Document.find([('id', '=', document.id)]))

            Attachment.delete([attachment])
            self.assertTrue(attachment_path.exists())
            with Transaction().start(
                    config.database_name, config.user,
                    context=config.context) as transaction:
                Entry = Pool(config.database_name).get('file.sync.entry')
                Entry.synchronize_path(str(attachment_path))
                transaction.commit()
            self.assertFalse(Attachment.find([('name', '=', 'renamed.bin')]))
            with config.set_context(active_test=False):
                inactive_attachment = Attachment(attachment.id)
                Attachment.delete([inactive_attachment])
                self.assertTrue(Attachment.find([
                            ('id', '=', attachment.id),
                            ]))
            run_task('ir.attachment')
            run_task('ir.attachment', 'delete_inactive')
            self.assertFalse(attachment_path.exists())
            with config.set_context(active_test=False):
                self.assertFalse(Attachment.find([
                            ('id', '=', attachment.id),
                            ]))

        if old_path is not None:
            tryton_config.set('file_sync', 'path', old_path)
        elif tryton_config.has_section('file_sync'):
            tryton_config.set('file_sync', 'path', '')
        tryton_config.set('queue', 'worker', old_worker)
