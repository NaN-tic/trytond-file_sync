import unittest

from trytond import backend
from trytond.pool import Pool
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules
from trytond.transaction import Transaction


class TestCategoryMigration(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):
        config = activate_modules('file_sync')

        with Transaction().start(
                config.database_name, config.user,
                context=config.context) as transaction:
            pool = Pool(config.database_name)
            Category = pool.get('office.category')
            Attachment = pool.get('ir.attachment')
            AttachmentCategory = pool.get('office.attachment-category')
            Entry = pool.get('file.sync.entry')
            ReadOnlyGroup = pool.get('office.category-read-only-group')
            ReadWriteGroup = pool.get('office.category-read-write-group')
            ReadOnlyUser = pool.get('office.category-read-only-user')
            ReadWriteUser = pool.get('office.category-read-write-user')
            Group = pool.get('res.group')
            User = pool.get('res.user')

            with Transaction().set_context(
                    office_migration=True, file_sync_skip=True,
                    _check_access=False):
                group, = Group.create([{'name': 'Category readers'}])
                user = User(config.user)
                category, = Category.create([{
                            'name': 'Migrated category',
                            'read_only_groups': [('add', [group.id])],
                            'read_write_users': [('add', [user.id])],
                            }])
                with Transaction().set_context(language='es'):
                    Category.write([category], {
                            'name': 'Categoría migrada',
                            })
                attachment, = Attachment.create([{
                            'name': 'Migrated attachment',
                            'type': 'text',
                            'content': 'Migration contents',
                            'unlinked': True,
                            'categories': [('add', [category.id])],
                            }])
                entry, = Entry.create([{
                            'category': category.id,
                            'attachment': attachment.id,
                            'path': 'Migrated attachment.md',
                            'digest': 'digest',
                            'size': 18,
                            'mtime_ns': 1,
                            }])

            cursor = transaction.connection.cursor()
            cursor.execute(
                "UPDATE ir_translation SET name = 'brainbow.tag,name', "
                "module = 'brainbow' "
                "WHERE name = 'office.category,name'")
            cursor.execute(
                "UPDATE ir_model SET name = 'brainbow.tag', "
                "module = 'brainbow' WHERE name = 'office.category'")
            cursor.execute(
                "UPDATE ir_model_field SET model = 'brainbow.tag', "
                "module = 'brainbow' WHERE model = 'office.category'")
            cursor.execute(
                "UPDATE ir_model_field SET relation = 'brainbow.tag' "
                "WHERE relation = 'office.category'")

            table_renames = [
                ('office_category', 'brainbow_tag'),
                ('office_attachment-category',
                    'brainbow_attachment-tag'),
                ('office_category-read-only-group',
                    'file_sync_tag-read-only-group'),
                ('office_category-read-write-group',
                    'file_sync_tag-read-write-group'),
                ('office_category-read-only-user',
                    'file_sync_tag-read-only-user'),
                ('office_category-read-write-user',
                    'file_sync_tag-read-write-user'),
                ]
            for new_table, old_table in table_renames:
                backend.TableHandler.table_rename(new_table, old_table)

            column_renames = [
                ('brainbow_attachment-tag', 'attachment_category'),
                ('file_sync_entry', 'entry'),
                ('file_sync_tag-read-only-group', 'read_only_group'),
                ('file_sync_tag-read-write-group', 'read_write_group'),
                ('file_sync_tag-read-only-user', 'read_only_user'),
                ('file_sync_tag-read-write-user', 'read_write_user'),
                ]
            for table_name, model_name in column_renames:
                Legacy = type(
                    'Legacy', (), {'_table': table_name})
                Legacy.__name__ = 'legacy.category.' + model_name
                backend.TableHandler(Legacy).column_rename(
                    'category', 'tag')

            Category.__register__('office')
            AttachmentCategory.__register__('office')
            Entry.__register__('file_sync')
            ReadOnlyGroup.__register__('office')
            ReadWriteGroup.__register__('office')
            ReadOnlyUser.__register__('office')
            ReadWriteUser.__register__('office')

            migrated_category = Category(category.id)
            migrated_attachment = Attachment(attachment.id)
            migrated_entry = Entry(entry.id)
            self.assertEqual(
                migrated_category.name, 'Migrated category')
            with Transaction().set_context(language='es'):
                self.assertEqual(
                    Category(category.id).name, 'Categoría migrada')
            self.assertEqual(
                [item.id for item in migrated_category.read_only_groups],
                [group.id])
            self.assertEqual(
                [item.id for item in migrated_category.read_write_users],
                [user.id])
            self.assertEqual(
                [item.id for item in migrated_attachment.categories],
                [category.id])
            self.assertEqual(migrated_entry.category.id, category.id)
            transaction.commit()
