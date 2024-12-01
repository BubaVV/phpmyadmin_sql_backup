#!/usr/bin/env python3
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
####################################################################################
#
# A Python script to automate the download of SQL dump backups
# via a phpMyAdmin web interface.
#
# tested on Python 3.4+
# requires: grab (http://grablib.org/)
#
# Christoph Haunschmidt, started 2016-03

import argparse
import datetime
import os
import re
import sys
from itertools import product
from urllib.parse import urljoin

import requests
from lxml import html

__version__ = '2024-12-01'

CONTENT_DISPOSITION_FILENAME_RE = re.compile(r'^.*filename="(?P<filename>[^"]+)".*$')
DEFAULT_PREFIX_FORMAT = r'%Y-%m-%d--%H-%M-%S-UTC_'


def is_login_successful(tree):
    hrefs = tree.xpath("//a/@href")
    target_substrings = ["frame_content", "server_export.php", "index.php?route=/server/export"]
    combinations = product(target_substrings, hrefs)

    return any(substring in href for substring, href in combinations)


def download_sql_backup(url, user, password, dry_run=False, overwrite_existing=False, prepend_date=True, basename=None,
                        output_directory=os.getcwd(), exclude_dbs=None, compression='none', prefix_format=None,
                        timeout=60, http_auth=None, server_name=None, **kwargs):
    prefix_format = prefix_format or DEFAULT_PREFIX_FORMAT
    exclude_dbs = exclude_dbs.split(',') if exclude_dbs else []
    session = requests.Session()

    # Login
    response = session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise ValueError("Failed to load the login page.")

    tree = html.fromstring(response.content)
    form_action = tree.xpath("//form[@id='login_form']/@action")
    form_action = form_action[0] if form_action else url

    form_data = {
        "pma_username": user,
        "pma_password": password,
    }

    hidden_inputs = tree.xpath("//form[@id='login_form']//input[@type='hidden']")
    for hidden_input in hidden_inputs:
        name = hidden_input.get("name")
        value = hidden_input.get("value", "")
        if name:
            form_data[name] = value

    login_response = session.post(urljoin(url,form_action), data=form_data, timeout=timeout)

    if login_response.status_code != 200:
        raise ValueError("Could not log in. Please check your credentials.")

    tree = html.fromstring(login_response.content)
    if not is_login_successful(tree):
        raise ValueError("Could not log in. Please check your credentials.")

    # Extract export URL
    export_url = tree.xpath("id('topmenu')//a[contains(@href,'server_export.php')]/@href")
    if not export_url:
        export_url = tree.xpath("id('topmenu')//a[contains(@href,'index.php?route=/server/export')]/@href")
    if not export_url:
        raise ValueError("Could not find export URL.")
    export_url = export_url[0]

    # Access export page
    export_response = session.get(urljoin(url,export_url), timeout=timeout)
    export_tree = html.fromstring(export_response.content)


    # Determine databases to dump
    dbs_available = export_tree.xpath("//select[@name='db_select[]']/option/@value")
    dbs_to_dump = [db_name for db_name in dbs_available if db_name not in exclude_dbs]
    if not dbs_to_dump:
        print(f'Warning: no databases to dump (databases available: "{", ".join(dbs_available)}")',
              file=sys.stderr)

    # Prepare form data
    dump_form_action = export_tree.xpath("//form[@name='dump']/@action")[0]
    form_data = {'db_select[]': dbs_to_dump}
    form_data['compression'] = compression
    form_data['what'] = 'sql'
    form_data['filename_template'] = '@SERVER@'
    form_data['sql_structure_or_data'] = 'structure_and_data'
    dump_hidden_inputs = export_tree.xpath("//form[@name='dump']//input[@type='hidden']")
    for hidden_input in dump_hidden_inputs:
        name = hidden_input.get("name")
        value = hidden_input.get("value", "")
        if name:
            form_data[name] = value

    # Submit form and download file
    file_response = session.post(urljoin(url, dump_form_action), data=form_data, timeout=timeout, stream=True)
    content_disposition = file_response.headers.get('Content-Disposition', '')
    re_match = CONTENT_DISPOSITION_FILENAME_RE.match(content_disposition)
    if not re_match:
        raise ValueError(f"Could not determine SQL backup filename from {content_disposition}")

    content_filename = re_match.group('filename')
    filename = content_filename if basename is None else basename + os.path.splitext(content_filename)[1]
    if prepend_date:
        prefix = datetime.datetime.utcnow().strftime(prefix_format)
        filename = prefix + filename
    out_filename = os.path.join(output_directory, filename)

    if os.path.isfile(out_filename) and not overwrite_existing:
        basename, ext = os.path.splitext(out_filename)
        n = 1
        print(f'File {out_filename} already exists, to overwrite it use --overwrite-existing', file=sys.stderr)
        while True:
            alternate_out_filename = f'{basename}_({n}){ext}'
            if not os.path.isfile(alternate_out_filename):
                out_filename = alternate_out_filename
                break
            n += 1

    # Save file if not dry run
    if not dry_run:
        with open(out_filename, 'wb') as f:
            for chunk in file_response.iter_content(chunk_size=8192):
                f.write(chunk)

    return out_filename


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Automates the download of SQL dump backups via a phpMyAdmin web interface.',
        epilog='Written by Christoph Haunschmidt et al., version: {}'.format(__version__))

    parser.add_argument('url', metavar='URL', help='phpMyAdmin login page url')
    parser.add_argument('user', metavar='USERNAME', help='phpMyAdmin login username')
    parser.add_argument('password', metavar='PASSWORD', help='phpMyAdmin login password')
    parser.add_argument('-o', '--output-directory', default=os.getcwd(),
                        help='output directory for the SQL dump file (default: the current working directory)')
    parser.add_argument('-p', '--prepend-date', action='store_true', default=False,
                        help='prepend current UTC date & time to the filename; '
                             'see the --prefix-format option for custom formatting')
    parser.add_argument('-e', '--exclude-dbs', default='',
                        help='comma-separated list of database names to exclude from the dump')
    parser.add_argument('-s', '--server-name', default=None,
                        help='mysql server hostname to supply if enabled as field on login page')
    parser.add_argument('--compression', default='none', choices=['none', 'zip', 'gzip'],
                        help='compression method for the output file - must be supported by the server (default: %(default)s)')
    parser.add_argument('--basename', default=None,
                        help='the desired basename (without extension) of the SQL dump file (default: the name given '
                             'by phpMyAdmin); you can also set an empty basename "" in combination with '
                             '--prepend-date and --prefix-format')
    parser.add_argument('--timeout', type=int, default=60,
                        help='timeout in seconds for the requests (default: %(default)s)')
    parser.add_argument('--overwrite-existing', action='store_true', default=False,
                        help='overwrite existing SQL dump files (instead of appending a number to the name)')
    parser.add_argument('--prefix-format', default='',
                        help=str('the prefix format for --prepend-date (default: "{}"); in Python\'s strftime format. '
                                 'Must be used with --prepend-date to be in effect'.format(
                            DEFAULT_PREFIX_FORMAT.replace('%', '%%'))))
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='dry run, do not actually download any file')
    parser.add_argument('--http-auth', default=None,
                        help='Basic HTTP authentication, using format "username:password"')

    args = parser.parse_args()

    if args.prefix_format and not args.prepend_date:
        print('Error: --prefix-format given without --prepend-date', file=sys.stderr)
        sys.exit(2)

    try:
        dump_fn = download_sql_backup(**vars(args))
    except Exception as e:
        print('Error: {}'.format(e), file=sys.stderr)
        sys.exit(1)

    print('{} saved SQL dump to: {}'.format(('Would have' if args.dry_run else 'Successfully'), dump_fn),
          file=sys.stdout)
