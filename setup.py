import io
import os
import re
from configparser import ConfigParser

from setuptools import setup


MODULE = 'file_sync'
MODULE_PREFIX = {
    'brainbow': 'nantic',
    }


def read(filename):
    return io.open(
        os.path.join(os.path.dirname(__file__), filename),
        'r', encoding='utf-8').read()


configuration = ConfigParser()
configuration.read('tryton.cfg')
info = dict(configuration.items('tryton'))
for key in ('depends', 'extras_depend', 'xml'):
    if key in info:
        info[key] = info[key].strip().splitlines()
version = info.get('version', '0.0.1')
major_version, minor_version, _ = version.split('.', 2)


def get_require_version(name):
    return '%s >= %s.%s, < %s.%s' % (
        name, major_version, minor_version,
        major_version, int(minor_version) + 1)


requires = [get_require_version('trytond')]
requires.append('merge3 >= 0.0.16')
requires.append('watchdog >= 6.0')
for dependency in info.get('depends', []):
    if not re.match(r'(ir|res)(\W|$)', dependency):
        prefix = MODULE_PREFIX.get(dependency, 'trytond')
        requires.append(get_require_version(
                '%s_%s' % (prefix, dependency)))


setup(
    name='nantic_file_sync',
    version=version,
    description='Bidirectional Brainbow filesystem synchronization',
    long_description=read('README'),
    author='NaN-tic',
    author_email='info@nan-tic.com',
    package_dir={'trytond.modules.file_sync': '.'},
    packages=[
        'trytond.modules.file_sync',
        'trytond.modules.file_sync.tests',
        ],
    package_data={
        'trytond.modules.file_sync': info.get('xml', []) + [
            'tryton.cfg', 'view/*.xml', 'locale/*.po'],
        },
    install_requires=requires,
    scripts=['trytond-file-sync.py'],
    zip_safe=False,
    entry_points={
        'trytond.modules': [
            'file_sync = trytond.modules.file_sync',
            ],
        },
    )
