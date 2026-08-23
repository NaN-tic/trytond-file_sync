from trytond.pool import Pool

from . import file_sync


def register():
    Pool.register(
        file_sync.Category,
        file_sync.Attachment,
        file_sync.SyncEntry,
        file_sync.Configuration,
        module='file_sync', type_='model')
