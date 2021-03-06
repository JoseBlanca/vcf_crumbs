# Copyright 2013 Jose Blanca, Peio Ziarsolo, COMAV-Univ. Politecnica Valencia
# This file is part of seq_crumbs.
# vcf_crumbs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# vcf_crumbs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR  PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with vcf_crumbs. If not, see <http://www.gnu.org/licenses/>.

import os.path
from subprocess import check_call, Popen, PIPE
from tempfile import NamedTemporaryFile
import gzip

from crumbs.utils.file_utils import wrap_in_buffered_reader, DEF_FILE_BUFFER

TEST_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                             '..', '..', 'test', 'test_data'))

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..',
                                        'data'))
BIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..',
                                       'bin'))

# Missing docstring
# pylint: disable=C0111


def compress_with_bgzip(in_fhand, compressed_fhand):
    '''It compresses the input fhand.

    The input fhand has to be a real file with a name.
    '''
    cmd = ['bgzip', '-c', in_fhand.name]
    check_call(cmd, stdout=compressed_fhand)


def uncompress_gzip(in_fhand, uncompressed_fhand):
    '''It compresses the input fhand.

    The input fhand has to be a real file with a name.
    '''
    cmd = ['gzip', '-dc', in_fhand.name]
    check_call(cmd, stdout=uncompressed_fhand)


def index_vcf_with_tabix(in_fpath):
    cmd = ['tabix', '-p', 'vcf', in_fpath]
    tabix = Popen(cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = tabix.communicate()
    if tabix.returncode:
        msg = 'Some problem indexing with tabix.\n'
        msg += 'stdout: ' + stdout
        msg += 'stderr: ' + stderr
        raise RuntimeError(msg)


def _vcf_is_gz(fhand):
    if hasattr(fhand, 'peek'):
        content = fhand.peek(2)[:2]
    else:
        content = fhand.read(2)
        fhand.seek(0)
    if not content:
        msg = 'The VCF file is empty'
        raise RuntimeError(msg)
    if content[0] == '#':
        return False
    if content == '\x1f\x8b':
        return True
    else:
        msg = 'Unable to determine if the VCF file is compressed or not'
        raise RuntimeError(msg)


def _fhand_is_tellable(fhand):
    try:
        fhand.tell()
        tellable = True
    except IOError:
        tellable = False
    return tellable


def get_input_fhand(in_fhand):
    in_fhand = wrap_in_buffered_reader(in_fhand, buffering=DEF_FILE_BUFFER)

    in_compressed = _vcf_is_gz(in_fhand)
    if in_compressed and not _fhand_is_tellable(in_fhand):
        msg = 'The given input has no tell member and it is compressed. '
        msg += 'You cannot use gzip file through stdin, try to pipe it '
        msg += 'uncompressed with zcat |'
        raise RuntimeError(msg)

    if in_compressed:
        mod_in_fhand = gzip.GzipFile(fileobj=in_fhand)
    else:
        mod_in_fhand = in_fhand

    return mod_in_fhand
