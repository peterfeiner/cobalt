# Copyright 2011 Gridcentric Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import subprocess
from distutils.core import setup

def git(*args):
    topdir = os.path.abspath(os.path.dirname(__file__))
    p = subprocess.Popen(('git',) + args, stdout=subprocess.PIPE, cwd=topdir)
    return p.communicate()[0]

def get_version():
    v = os.getenv('VERSION', None)
    if v is None:
        try:
            from pkginfo import UnpackedSDist
            d = UnpackedSDist(__file__)
            v = d.version
        except ValueError:
            try:
                v = git('describe', '--tags').strip().split('/', 1)[1].split('-', 1)[1]
            except Exception:
                v = '0.0'
    return v

def get_package():
    p = os.getenv('PACKAGE', None)
    if p is None:
        try:
            from pkginfo import UnpackedSDist
            d = UnpackedSDist(__file__)
            p = d.name
        except ValueError:
            p = 'all'
    return p

ROOT = os.path.dirname(os.path.realpath(__file__))
PIP_REQUIRES = os.path.join(ROOT, 'pip-requires')
TEST_REQUIRES = os.path.join(ROOT, 'test-requires')

def read_file_list(filename):
    with open(filename) as f:
        return [line.strip() for line in f.readlines()
                if len(line.strip()) > 0]

PACKAGE = get_package()
VERSION = get_version()
INSTALL_REQUIRES = read_file_list(PIP_REQUIRES)

COMMON = dict(
    author='GridCentric',
    author_email='support@gridcentric.com',
    namespace_packages=['gridcentric'],
    test_requires = read_file_list(TEST_REQUIRES),
    url='http://www.gridcentric.com/',
    version=VERSION,
    classifiers = [
        'Environment :: OpenStack',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 2.6']
)

if PACKAGE == 'all' or PACKAGE == 'cobalt':
    setup(name='cobalt',
          description='Cobalt extension for OpenStack Compute.',
          install_requires = INSTALL_REQUIRES,
          packages=['cobalt',
                    'cobalt.horizon',
                    'cobalt.nova',
                    'cobalt.nova.osapi',
                    'cobalt.nova.extension'],
          **COMMON)

if PACKAGE == 'all' or PACKAGE == 'cobalt-compute':
    setup(name='cobalt-compute',
          description='Cobalt extension for OpenStack Compute.',
          install_requires = INSTALL_REQUIRES + ['cobalt'],
          scripts=['bin/cobalt-compute'],
          **COMMON)

if PACKAGE == 'all' or PACKAGE == 'cobalt-api':
    setup(name='cobalt-api',
          description='Cobalt API extension.',
          install_requires = INSTALL_REQUIRES + ['cobalt'],
          **COMMON)

if PACKAGE == 'all' or PACKAGE == 'cobalt-horizon':
    setup(name='cobalt-horizon',
          description='Gridcentric plugin for OpenStack Dashboard',
          install_requires = INSTALL_REQUIRES + ['cobalt'],
          **COMMON)
