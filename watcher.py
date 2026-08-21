import errno
import logging

from watchdog.events import (
    DirCreatedEvent, DirDeletedEvent, DirMovedEvent, FileCreatedEvent,
    FileDeletedEvent, FileModifiedEvent, FileMovedEvent,
    FileSystemEventHandler)
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


logger = logging.getLogger(__name__)

POLL_INTERVAL = 30
WATCHDOG_RESOURCE_ERRORS = {errno.EMFILE, errno.ENFILE, errno.ENOSPC}
EVENT_FILTER = [
    FileCreatedEvent, FileModifiedEvent, FileDeletedEvent, FileMovedEvent,
    DirCreatedEvent, DirDeletedEvent, DirMovedEvent,
    ]


class FileSyncEventHandler(FileSystemEventHandler):

    def __init__(self, events, ignore=None):
        super().__init__()
        self.events = events
        self.ignore = ignore

    def _put(self, path, operation):
        if not self.ignore or not self.ignore(path):
            self.events.put((path, operation))

    def on_created(self, event):
        self._put(event.src_path, 'creation')

    def on_modified(self, event):
        self._put(event.src_path, 'modification')

    def on_deleted(self, event):
        self._put(event.src_path, 'deletion')

    def on_moved(self, event):
        self._put(event.src_path, 'deletion')
        self._put(event.dest_path, 'creation')


def create_observer(path, events, timeout, ignore=None):
    handler = FileSyncEventHandler(events, ignore=ignore)
    observer = Observer(timeout=timeout)
    observer.schedule(
        handler, path, recursive=True, event_filter=EVENT_FILTER)
    try:
        observer.start()
    except OSError as exception:
        observer.unschedule_all()
        if exception.errno not in WATCHDOG_RESOURCE_ERRORS:
            raise
        logger.error(
            'could not watch %s with the native watchdog observer (%s); '
            'switching to polling every %s seconds',
            path, exception, POLL_INTERVAL)
        observer = PollingObserver(timeout=POLL_INTERVAL)
        observer.schedule(
            handler, path, recursive=True, event_filter=EVENT_FILTER)
        observer.start()
    return observer
