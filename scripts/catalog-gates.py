#!/usr/bin/env python3
"""Pure transaction/cleanup gates; deliberately no deletion or publication executor."""
import re
import catalog as c


def transition(old_records, old_ledger, new_records, new_ledger):
    c.validate_ledger(old_ledger)
    c.validate_ledger(new_ledger)
    # Ledger entries are append-only even if the corresponding record is deleted.
    for token, row in old_ledger['processed'].items():
        if new_ledger['processed'].get(token) != row:
            raise ValueError('processed-source history was rewritten')
    if not new_records:
        raise ValueError('all-unlist approval protocol is not yet implemented')
    previous = {r['id']: r for r in old_records}
    for record in new_records:
        c.validate_record(record, record['id'] + '.json')
        token = c.source_token(record['source'])
        if new_ledger['processed'].get(token) != c.ledger_row(record):
            raise ValueError('new record missing ledger')
        if token in old_ledger['processed'] and previous.get(record['id']) != record:
            raise ValueError('stale inbox resurrection or mutation requires explicit restore')
    return True


def cleanup_candidates(plan, records, ledger, receipt, current_commit, snapshot,
                       enabled=False, approved_plan_sha256=None):
    # Absent/false gate has no side effects. Strings such as "false" do not enable.
    if enabled is not True:
        return []
    if not re.fullmatch(c.BLOB, current_commit):
        raise ValueError('invalid current commit')
    if approved_plan_sha256 != c.sha(c.encoded(plan)):
        raise ValueError('no exact independently approved plan')
    if plan.get('base_commit') != current_commit:
        raise ValueError('branch advanced after plan')
    # A caller must independently obtain the trusted receipt from the publish job,
    # never accept arbitrary user-supplied JSON as authenticated evidence.
    if not isinstance(receipt, dict) or receipt.get('commit') != current_commit or receipt.get('success') is not True:
        raise ValueError('publication did not succeed at current commit')
    if receipt.get('catalog_sha256') != c.sha(c.encoded(sorted(records, key=lambda r: r['id']))):
        raise ValueError('published catalog changed')
    if receipt.get('index_sha256') != c.sha(c.render(sorted(records, key=lambda r: r['id']))):
        raise ValueError('published index changed')
    expected = c.cleanup_plan(records, ledger, snapshot, current_commit, plan['debs_tree'])
    if expected != plan:
        raise ValueError('catalog, ledger or exact inbox blob changed')
    verified = receipt.get('verified_objects', {})
    for record in records:
        evidence = verified.get(record['object_key'])
        if evidence != {'sha256': record['sha256'], 'size': record['size'],
                        'head_metadata_verified': True, 'full_get_hash_verified': True}:
            raise ValueError('missing complete object verification')
    return list(plan['files'])
