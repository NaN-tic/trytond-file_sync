from trytond.pool import Pool

from . import file_sync


def register():
    Pool.register(
        file_sync.Tag,
        file_sync.Document,
        file_sync.Attachment,
        file_sync.SyncEntry,
        file_sync.Configuration,
        file_sync.TagAttachment,
        file_sync.TagReadOnlyGroup,
        file_sync.TagReadWriteGroup,
        file_sync.TagReadOnlyUser,
        file_sync.TagReadWriteUser,
        module='file_sync', type_='model')
