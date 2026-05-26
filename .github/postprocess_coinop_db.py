#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_JSON_NAME = os.getenv('DB_JSON_NAME', 'db.json')
DB_ZIP_NAME = os.getenv('DB_ZIP_NAME', 'db.json.zip')
DB_BRANCH = os.getenv('DB_BRANCH', 'db')
DB_ID = os.getenv('DB_ID') or os.getenv('GITHUB_REPOSITORY', 'theypsilon/test')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY', 'theypsilon/test')
DB_URL = os.getenv('DB_URL', f'https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/{DB_BRANCH}/{DB_ZIP_NAME}')
TRACK_RELEASE = os.getenv('TRACK_RELEASE', 'true').lower() != 'false'

COINOP_ALPHA_TAG = 'coinopcollectionalpha'
COINOP_BETA_TAG = 'coinopcollectionbeta'
COINOP_DEFAULT_FILTER = '[MiSTer] !coinop-collection-beta !coinop-collection-alpha'
STATUS_ALPHA = 'alpha'
STATUS_BETA = 'beta'
STATUS_STABLE = 'stable'


def main() -> int:
    validate_file_name(DB_JSON_NAME, 'DB_JSON_NAME')
    validate_file_name(DB_ZIP_NAME, 'DB_ZIP_NAME')

    if not Path(DB_JSON_NAME).exists():
        log(f'{DB_JSON_NAME} was not generated. Nothing to publish.')
        return 0

    process_database(DB_JSON_NAME)

    passes_db_tests(DB_ID, DB_JSON_NAME)
    zip_database(DB_ZIP_NAME, DB_JSON_NAME)
    configure_git()
    publish_db()
    return 0


def validate_file_name(name: str, variable_name: str) -> None:
    if name == '' or '/' in name:
        raise ValueError(f'{variable_name} must be a file name, not a path')


def configure_git() -> None:
    run(['git', 'config', '--global', 'user.email', 'theypsilon@gmail.com'])
    run(['git', 'config', '--global', 'user.name', 'The CI/CD Bot'])


def zip_database(zip_name: str, json_name: str) -> None:
    if Path(zip_name).exists():
        Path(zip_name).unlink()
    run(['zip', zip_name, json_name])


def process_database(db_json_name: str) -> None:
    with open(db_json_name, encoding='utf-8') as f:
        db = json.load(f)

    alpha_tag = ensure_tag_dictionary_entry(db, COINOP_ALPHA_TAG)
    beta_tag = ensure_tag_dictionary_entry(db, COINOP_BETA_TAG)
    rbf_references: dict[str, set[str]] = {}
    mra_alpha_count = 0
    mra_beta_count = 0
    rbf_alpha_count = 0
    rbf_beta_count = 0

    for db_path, description in db.get('files', {}).items():
        if not db_path.lower().endswith('.mra'):
            continue

        mra_path = db_path[1:] if db_path.startswith('|') else db_path
        if not Path(mra_path).exists():
            log(f'Warning: Cannot inspect missing MRA file: {mra_path}')
            continue

        statuses = read_mra_statuses(mra_path)
        referenced_rbf = read_mra_rbf(mra_path)
        if referenced_rbf is not None:
            rbf_references.setdefault(referenced_rbf, set()).add(status_for_rbf_reference(statuses))

        if STATUS_ALPHA in statuses and append_unique_tag(description, alpha_tag):
            mra_alpha_count += 1
        if STATUS_BETA in statuses and append_unique_tag(description, beta_tag):
            mra_beta_count += 1

    for db_path, description in db.get('files', {}).items():
        if not db_path.lower().endswith('.rbf'):
            continue

        status = status_for_rbf(db_path, rbf_references)
        if status == STATUS_ALPHA and append_unique_tag(description, alpha_tag):
            rbf_alpha_count += 1
        if status == STATUS_BETA and append_unique_tag(description, beta_tag):
            rbf_beta_count += 1

    db.setdefault('default_options', {})['filter'] = COINOP_DEFAULT_FILTER

    with open(db_json_name, 'w', encoding='utf-8') as f:
        json.dump(db, f, sort_keys=True)

    log(
        f'Applied Coin-Op status tags: '
        f'{mra_alpha_count} alpha MRAs, {mra_beta_count} beta MRAs, '
        f'{rbf_alpha_count} alpha RBFs, {rbf_beta_count} beta RBFs'
    )


def ensure_tag_dictionary_entry(db: dict[str, Any], tag: str) -> int:
    tag_dictionary = db.setdefault('tag_dictionary', {})
    if tag in tag_dictionary:
        return tag_dictionary[tag]

    next_index = 0
    if tag_dictionary:
        next_index = max(tag_dictionary.values()) + 1

    tag_dictionary[tag] = next_index
    return next_index


def read_mra_statuses(mra_path: str) -> set[str]:
    with open(mra_path, encoding='utf-8', errors='ignore') as f:
        normalized = re.sub(r'\s+', '', f.read()).lower()

    statuses = set()
    if '<t_status>alpha</t_status>' in normalized:
        statuses.add(STATUS_ALPHA)
    if '<t_status>beta</t_status>' in normalized:
        statuses.add(STATUS_BETA)
    return statuses


def read_mra_rbf(mra_path: str) -> Optional[str]:
    with open(mra_path, encoding='utf-8', errors='ignore') as f:
        normalized = re.sub(r'\s+', '', f.read()).lower()

    match = re.search(r'<rbf>([^<]+)</rbf>', normalized)
    if match is None:
        return None
    return normalize_rbf_reference(match.group(1))


def status_for_rbf_reference(statuses: set[str]) -> str:
    if STATUS_BETA in statuses:
        return STATUS_BETA
    if STATUS_ALPHA in statuses:
        return STATUS_ALPHA
    return STATUS_STABLE


def status_for_rbf(db_path: str, rbf_references: dict[str, set[str]]) -> Optional[str]:
    statuses = set()
    for key in rbf_reference_keys(db_path):
        statuses.update(rbf_references.get(key, set()))

    if STATUS_STABLE in statuses or not statuses:
        return None
    if STATUS_BETA in statuses:
        return STATUS_BETA
    if STATUS_ALPHA in statuses:
        return STATUS_ALPHA
    return None


def rbf_reference_keys(db_path: str) -> set[str]:
    path = Path(db_path)
    stem = path.stem.lower()
    return {normalize_rbf_reference(stem), normalize_rbf_reference(strip_date_suffix(stem))}


def normalize_rbf_reference(rbf: str) -> str:
    return Path(rbf).stem.lower()


def strip_date_suffix(stem: str) -> str:
    date_suffix = stem[-9:]
    if len(date_suffix) == 9 and date_suffix[0] == '_' and date_suffix[1:].isdigit():
        return stem[:-9]
    return stem


def append_unique_tag(description: dict[str, Any], tag: int) -> bool:
    tags = description.setdefault('tags', [])
    if tag in tags:
        return False
    tags.append(tag)
    tags.sort()
    return True


def passes_db_tests(db_id: str, db_json_name: str) -> None:
    log('\nTesting database...\n')
    with tempfile.TemporaryDirectory() as temp_folder:
        downloader_test = f'{temp_folder}/downloader_test.py'
        download_file(
            'https://github.com/MiSTer-devel/Downloader_MiSTer/releases/download/latest/downloader_test.py',
            downloader_test,
        )
        run(['chmod', '+x', downloader_test])
        run([downloader_test, db_id, f'{Path.cwd()}/{db_json_name}'])
    log('\nThe test went well.\n')


def publish_db() -> None:
    log('Publishing processed database...')
    run(['git', 'checkout', '--orphan', DB_BRANCH])
    run(['git', 'reset'])
    run(['git', 'add', DB_ZIP_NAME, *create_drop_in_database_files(DB_ID, DB_URL)])
    run(['git', 'commit', '-m', 'Creating database'])
    run(['git', 'push', '--force', 'origin', DB_BRANCH])

    if TRACK_RELEASE:
        track_release()


def track_release() -> None:
    try:
        log('Tracking release...')
        db_commit_hash = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        releases_check = subprocess.run(
            ['git', 'ls-remote', '--heads', 'origin', 'db-releases'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if 'refs/heads/db-releases' in releases_check.stdout:
            run(['git', 'fetch', 'origin', 'db-releases'])
            run(['git', 'checkout', 'db-releases'])
        else:
            run(['git', 'checkout', '--orphan', 'db-releases'])

        run(['git', 'reset', '--hard'])

        with open('commits.txt', 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}: {db_commit_hash}\n")

        run(['git', 'add', 'commits.txt'])
        run(['git', 'commit', '-m', f'Track release {db_commit_hash}'])
        run(['git', 'push', 'origin', 'db-releases'])
        run(['git', 'checkout', DB_BRANCH])
    except Exception as e:
        log(f'Warning: Failed to track release: {e}')
        log(traceback.format_exc())


def create_drop_in_database_files(db_id: str, db_url: str) -> list[str]:
    try:
        sanitized_db_id = sanitize_db_id_for_filename(db_id)
        return [
            file
            for suffix, filter_value in [
                ('', None),
                ('_beta', '[MiSTer] !coinop-collection-alpha'),
                ('_alpha', '[MiSTer]'),
            ]
            for file in create_drop_in_database_file_pair(sanitized_db_id, suffix, db_id, db_url, filter_value)
        ]
    except Exception as e:
        log(f'Warning: Failed to create drop-in database files: {e}')
        log(traceback.format_exc())
        return []


def create_drop_in_database_file_pair(
    sanitized_db_id: str,
    suffix: str,
    db_id: str,
    db_url: str,
    filter_value: Optional[str],
) -> list[str]:
    drop_in_ini = f'downloader_{sanitized_db_id}{suffix}.ini'
    drop_in_zip = f'downloader_{sanitized_db_id}{suffix}.zip'
    drop_in_contents = f'[{db_id}]\ndb_url = {db_url}\n'
    if filter_value is not None:
        drop_in_contents += f'filter = {filter_value}\n'

    with open(drop_in_ini, 'w', encoding='utf-8', newline='\n') as f:
        f.write(drop_in_contents)

    with zipfile.ZipFile(drop_in_zip, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(drop_in_ini, drop_in_contents)

    return [drop_in_ini, drop_in_zip]


def sanitize_db_id_for_filename(db_id: str) -> str:
    sanitized_db_id = re.sub(r'[^A-Za-z0-9._-]+', '_', db_id).strip('._-')
    if sanitized_db_id == '':
        raise ValueError(f'Unable to derive a drop-in filename from DB_ID "{db_id}"')
    return sanitized_db_id


def download_file(url: str, output_path: str) -> None:
    log(f'Downloading {url} to {output_path}')
    run(['curl', '--fail', '--location', '--output', output_path, url])


def run(commands: list[str]) -> None:
    log(' '.join(commands))
    subprocess.run(commands, check=True, stderr=subprocess.STDOUT)


def log(*text: Any) -> None:
    print(*text, flush=True)


if __name__ == '__main__':
    sys.exit(main())
