#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import logging
import os
import sys
import time


TRYTOND = os.path.abspath(os.path.normpath(os.path.join(
            __file__, '..', '..', '..')))
if os.path.isfile(os.path.join(TRYTOND, '__init__.py')):
    sys.path.insert(0, os.path.dirname(TRYTOND))

import trytond.commandline as commandline  # noqa: E402
import trytond.config as config  # noqa: E402

logger = logging.getLogger(__name__)


def main():
    parser = commandline.get_parser_daemon()
    parser.description = (
        'Watch [file_sync] path with inotify and synchronize Tryton files.')
    parser.add_argument(
        '--debounce', type=float, default=0.25,
        help='seconds used to coalesce filesystem events (default: 0.25)')
    commandline.set_autocomplete(parser)
    options = parser.parse_args()
    config.update_etc(options.configfile)
    commandline.config_log(options)
    if len(options.database_names) != 1:
        parser.error('exactly one database must be selected')
    configured_path = config.get('file_sync', 'path')
    if not configured_path:
        parser.error('[file_sync] path must be configured')

    from trytond.pool import Pool
    from trytond.transaction import Transaction

    from trytond.modules.file_sync.inotify import (
        IN_CREATE, IN_MOVED_TO, InotifyWatcher)
    from trytond.modules.file_sync.sync import Synchronizer

    database_name = options.database_names[0]
    path = os.path.realpath(os.path.expanduser(configured_path))
    with commandline.pidfile(options):
        Pool.start()
        pool = Pool(database_name)
        with Transaction().start(database_name, 0, readonly=True):
            pool.init()
        os.makedirs(path, exist_ok=True)
        logger.info('watching %s for database %s', path, database_name)
        with InotifyWatcher(path) as watcher:
            with Transaction().start(database_name, 0) as transaction:
                Synchronizer(required=True).synchronize()
                transaction.commit()
            pending = {}
            last_event = None
            try:
                while True:
                    events = watcher.read(
                        timeout=max(options.debounce, 0.05))
                    if events:
                        last_event = time.monotonic()
                        for event in events:
                            pending[event.path] = (
                                pending.get(event.path, 0) | event.mask)
                    if (not pending or last_event is None
                            or time.monotonic() - last_event
                            < options.debounce):
                        continue
                    paths = sorted(
                        pending.items(),
                        key=lambda item: not os.path.exists(item[0]))
                    pending.clear()
                    try:
                        with Transaction().start(
                                database_name, 0) as transaction:
                            synchronizer = Synchronizer(required=True)
                            for changed_path, mask in paths:
                                if not os.path.exists(changed_path):
                                    operation = 'deletion'
                                elif mask & (IN_CREATE | IN_MOVED_TO):
                                    operation = 'creation'
                                else:
                                    operation = 'modification'
                                logger.info(
                                    'synchronizing filesystem %s: %s',
                                    operation, changed_path)
                                synchronizer.synchronize_path(changed_path)
                            transaction.commit()
                    except Exception:
                        logger.exception(
                            'failed to synchronize filesystem events')
            except (KeyboardInterrupt, SystemExit):
                pass


if __name__ == '__main__':
    main()
