"""Prove the reasoning tests are load-bearing.

Copies this bridge into a throwaway tree, removes one behaviour at a time, and requires
the named test to FAIL. A green suite with the feature deleted would mean the tests only
assert their own mocks.

Run: python tools/verify_tests_load_bearing.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, relative file, exact text to find, replacement, test that must go red)
MUTATIONS = [
    ('reasoning-guard-off', 'muse_bridge/driver.py',
     'if not REASONING:\n            return ""',
     'if True:\n            return ""',
     'test_reasoning_text_reads_first_hit'),
    ('stale-thinking-leak', 'muse_bridge/driver.py',
     'if current and current == before:',
     'if current and current == before and not reasoning_now:',
     'test_stale_reasoning_not_leaked_before_new_answer'),
    ('no-reasoning-frame', 'muse_bridge/app.py',
     'if reasoning:',
     'if False:  # mutation',
     'test_app_streams_reasoning_content'),
    ('usage-drops-thinking', 'muse_bridge/app.py',
     '_usage_of(prompt, reasoning + text)',
     '_usage_of(prompt, text)',
     'test_nonstream_response_carries_reasoning_content'),
    ('retry-cooldown-gone', 'muse_bridge/state.py',
     'RETRYABLE_STATES = frozenset(\n'
     '    {AccountState.RATE_LIMITED, AccountState.BROWSER_UNAVAILABLE}\n'
     ')',
     'RETRYABLE_STATES = frozenset()',
     'test_transient_states_return_after_cooldown'),
    ('admin-auth-gone', 'muse_bridge/app.py',
     'async def attach(request: AttachRequest, _key: None = Depends(require_key)) -> dict:',
     'async def attach(request: AttachRequest) -> dict:',
     'test_mutating_admin_requires_key_when_configured'),
    ('admin-500-instead-of-503', 'muse_bridge/app.py',
     '        ok = await driver.touch_session()\n    except BrowserUnavailable as exc:',
     '        ok = await driver.touch_session()\n    except KeyError as exc:',
     'test_admin_refresh_and_wake_map_browser_unavailable_to_503'),
    ('wake-guard-gone', 'muse_bridge/pool.py',
     '                try:\n'
     '                    await driver.wake_vm()\n'
     '                    vm_ready = True\n'
     '                except Exception as exc:  # noqa: BLE001 - 唤醒失败不是派单失败',
     '                try:\n'
     '                    await driver.wake_vm()\n'
     '                    vm_ready = True\n'
     '                except ZeroDivisionError as exc:',
     'test_dispatch_survives_wake_failure_and_keeps_lock_for_caller'),
    ('handoff-guard-gone', 'muse_bridge/pool.py',
     '            if not handed_off:',
     '            if False:',
     'test_dispatch_releases_lock_when_unexpected_error'),
    ('smoke-auth-header-gone', 'muse_bridge/smoke.py',
     '    request = urllib.request.Request(f"{BASE}{path}", headers=_auth_headers())',
     '    request = urllib.request.Request(f"{BASE}{path}")',
     'test_get_sends_authorization_when_key_set'),
    ('env-validation-gone', 'muse_bridge/env.py',
     '        raise EnvError(f"{name} 必须是整数，当前值：{raw!r}") from None',
     '        return 0  # mutated: 坏值被吞掉',
     'test_bad_int_names_the_variable'),
    ('wake-reports-awake', 'muse_bridge/pool.py',
     '''            state = after_probe(logged_in=True, now=int(time.time()), vm_ready=vm_ready)
            store.mark_ready(account, vm_ready=vm_ready, state=state)
''',
     '''            state = after_probe(logged_in=True, now=int(time.time()), vm_ready=True)
            store.mark_ready(account, vm_ready=True, state=state)
''',
     'test_failed_wake_is_not_recorded_as_vm_ready'),
    ('doctor-error-masked', 'muse_bridge/doctor.py',
     '''    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
''',
     '''    finally:
        shutil.rmtree(tmp)
''',
     'test_store_check_reports_the_real_error_not_a_cleanup_error'),
    ('truncation-reports-stop', 'muse_bridge/app.py',
     '"finish_reason": "length" if truncated else "stop"',
     '"finish_reason": "stop"',
     'test_truncated_stream_reports_length_not_stop'),
    ('crlf-doubling-detected', 'muse_bridge/smoke.py',
     'BASE = f"http://127.0.0.1:{PORT}"',
     'BASE = f"http://127.0.0.1:{PORT}"\r\r\n',
     'test_no_doubled_carriage_returns'),
    ('cooldown-clock-row-write', 'muse_bridge/store.py',
     '                last_failure_at=record.last_failure_at,',
     '                last_failure_at=record.updated_at,',
     'test_touch_refresh_does_not_restart_cooldown'),
    ('deadline-truncation-silent', 'muse_bridge/driver.py',
     '        if received or started:',
     '        if not started and not received:',
     'test_deadline_mid_stream_raises_instead_of_returning_partial'),
    ('send-reaction-never-fires', 'muse_bridge/driver.py',
     '                and time.monotonic() >= reaction_deadline',
     '                and False  # mutation: 永远不早抛',
     'test_no_reaction_fails_fast_instead_of_waiting_the_full_timeout'),
    ('exposure-warning-silenced', 'muse_bridge/__main__.py',
     '    if host in _LOOPBACK:',
     '    if True:  # mutation: 永远静默',
     'test_exposed_bridge_without_keys_warns'),
]


def copy_tree(dest: Path) -> None:
    ignore = shutil.ignore_patterns('__pycache__', '.pytest_cache', '.ruff_cache',
                                    'data', '.git')
    shutil.copytree(ROOT, dest, ignore=ignore, dirs_exist_ok=True)


def run_reasoning_tests(tree: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests', '-q', '--tb=no',
         '-p', 'no:cacheprovider'],
        cwd=str(tree), capture_output=True, text=True,
        env={**__import__('os').environ, 'PYTHONPATH': str(tree),
              'PYTHONIOENCODING': 'utf-8'},
    )
    return proc.returncode, proc.stdout + proc.stderr


fails = []

def chk(ok, label):
    print(('  OK   ' if ok else '  FAIL ') + label)
    if not ok:
        fails.append(label)


with tempfile.TemporaryDirectory(prefix='loadbearing-') as tmp:
    tree = Path(tmp) / 'tree'
    copy_tree(tree)

    code, out = run_reasoning_tests(tree)
    chk(code == 0, 'control: unmutated tree is green')
    if code != 0:
        print(out[-1500:])

    for label, rel, old, new, expected in MUTATIONS:
        target = tree / rel
        text = target.read_text(encoding='utf-8').replace('\r\n', '\n')
        count = text.count(old)
        chk(count == 1, f'{label}: mutation anchor unique (found {count})')
        if count != 1:
            continue
        target.write_text(text.replace(old, new), encoding='utf-8', newline='\n')
        code, out = run_reasoning_tests(tree)
        target.write_text(text, encoding='utf-8', newline='\n')  # restore immediately
        chk(code != 0, f'{label}: suite goes red without the feature')
        went_red = re.search(r'^FAILED tests/[^:]+\.py::' + re.escape(expected),
                             out, re.M) is not None
        chk(went_red, f'{label}: the named test ({expected}) is the one that fails')
        if not went_red and code != 0:
            print(out[-1200:])

print('\n' + ('TESTS ARE LOAD-BEARING' if not fails else f'{len(fails)} FAILED'))
sys.exit(1 if fails else 0)
