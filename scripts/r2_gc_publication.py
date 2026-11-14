#!/usr/bin/env python3
"""Publisher-only future GC integration. No standalone receipt/path CLI.

State advances only after a complete successful publisher receipt. A failure
before that point cannot confirm absence or create new deletion candidates.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import time

spec = importlib.util.spec_from_file_location('physical_gc', Path(__file__).with_name('r2-physical-gc.py'))
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)
STATE = 'physical-gc-state-v2.json'


def clock(prefix):
    value = os.environ.get('R2_GC_TEST_NOW')
    if value is None:
        return time.time()
    # Clock injection is impossible for production and old general test scopes.
    import r2_shared_lock
    if (not re.fullmatch(r'catalog-e2e/r2-gc-[A-Za-z0-9_-]+/', prefix) or
            prefix != r2_shared_lock.scope()):
        raise ValueError('GC clock override is restricted to r2-gc isolated test scope')
    return float(value)


def validate_state(value, owner, prefix, now):
    if (value.get('schema') != 2 or value.get('owner') != owner or
            value.get('prefix') != prefix):
        raise ValueError('old or foreign physical GC state; manual review required')
    state = value['engine']
    if state.get('schema') != 1 or state['observed_at'] > now:
        raise ValueError('invalid GC clock/schema')
    gc.objects(state)
    for field in ('last_receipt', 'baseline_receipt'):
        if not re.fullmatch('[0-9a-f]{64}', state[field]):
            raise ValueError('invalid receipt hash')
    for key, event in state['pending'].items():
        gc.objects({'objects': {key: event['object']}})
        if (event['object']['ownership'] != 'managed' or key in state['objects'] or
                not isinstance(event['removed_at'], (float, int)) or
                event['removed_at'] > state['observed_at'] or
                type(event['confirmations']) is not int or event['confirmations'] < 1):
            raise ValueError('invalid candidate event')
        for field in ('last_present_receipt', 'first_absent_receipt'):
            if not re.fullmatch('[0-9a-f]{64}', event[field]):
                raise ValueError('invalid candidate receipt')
    return state


def after_publication(pub, s3, bucket, base, current, receipt_key, proof, gate):
    """Only invoked in publisher's still-held shared critical section.

    pub is the publisher module: supplies authenticated reads/CAS puts and curl.
    Git head must still equal the published commit. Legitimate later inbox-only
    cleanup is handled by the NEXT schedule publication, not by weakening gate.
    """
    now = clock(pub.PREFIX)
    receipt_raw = pub.payload(proof)
    manifest_raw = pub.payload(current)
    receipt_sha = gc.digest(receipt_raw)
    if (not proof.get('success') or proof.get('commit') != current['commit'] or
            proof.get('manifest_sha256') != gc.digest(manifest_raw) or
            proof.get('catalog_sha256') != current.get('catalog_sha256') or
            proof.get('index_sha256') != current.get('index_sha256') or
            proof.get('release_sha256') != current['release_sha256'] or
            not re.fullmatch(r'receipts/[0-9a-f]{40}/[0-9]+-[0-9]+\.json', receipt_key)):
        raise ValueError('untrusted publisher receipt')

    def live_gate():
        gate()  # exact branch head AND mutex token/run/scope
        if gc.owned_bytes(s3, bucket, receipt_key, pub.OWNER) != receipt_raw:
            raise ValueError('immutable receipt mismatch')
        if gc.owned_bytes(s3, bucket, pub.MANIFEST, pub.OWNER) != manifest_raw:
            raise ValueError('live manifest changed')
        for key, field in [('Release', 'release_sha256'), ('Packages', 'index_sha256')]:
            raw = gc.owned_bytes(s3, bucket, key, pub.OWNER)
            if gc.digest(raw) != current[field] or pub.public(base, key) != raw:
                raise ValueError('live public/S3 index mismatch')
        gate()
        return receipt_sha

    live_gate()
    old_raw = pub.read(s3, bucket, STATE)
    old = None
    if old_raw is not None:
        if gc.owned_bytes(s3, bucket, STATE, pub.OWNER) != old_raw:
            raise ValueError('GC state changed')
        old = validate_state(json.loads(old_raw), pub.OWNER, pub.PREFIX, now)
    engine = gc.observe(old, current, receipt_sha, now)
    wrapper = {'schema': 2, 'owner': pub.OWNER, 'prefix': pub.PREFIX, 'engine': engine}
    validate_state(wrapper, pub.OWNER, pub.PREFIX, now)
    live_gate()
    # put_verify does IfNoneMatch for baseline, IfMatch on existing state.
    pub.put_verify(s3, bucket, base, STATE, pub.payload(wrapper))
    result = gc.delete_coordinated(s3, bucket, pub.OWNER, engine, now, live_gate)
    if result['deleted'] or result['absent']:
        for key in result['deleted'] + result['absent']:
            engine['pending'].pop(key, None)
        live_gate()
        pub.put_verify(s3, bucket, base, STATE, pub.payload(wrapper))
    result.update(baseline=old is None, pending=len(engine['pending']),
                  future_only=True, grace_seconds=gc.GRACE, receipt_sha256=receipt_sha)
    Path('r2-physical-gc-result.json').write_bytes(pub.payload(result))
    print('PHYSICAL GC ' + json.dumps(result, sort_keys=True), flush=True)
    return result
