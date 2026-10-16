"""Validate web/admin.html: inline-script syntax plus the invariants we rely on.

Usage:  python -X utf8 check_ui.py

Extracts the single inline <script>, syntax-checks it with the project's bundled
node, and reports the properties that have bitten us before (external
dependencies, maxlength blocking IME input, leftover destructive buttons).
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / 'web' / 'admin.html'
NODE = ROOT / 'vendor' / 'napcat' / 'node.exe'

EXPECTED_IDS = (
    'cv-ex-heat', 'cv-ex-net', 'cv-ex-zipf', 'cv-ex-lift',
    'loop-card', 'loop-plan', 'loop-msg', 'loop-preview', 'loop-clean',
    'purge-card', 'adj-msg',
)
# Buttons/functions that must NOT exist: the panel offers no way to delete
# retained transitions, and no destructive purge of any kind.
FORBIDDEN = ('purge-btn', '/api/generate/purge')


def main():
    if not PAGE.is_file():
        raise SystemExit(f'missing {PAGE}')
    html = PAGE.read_text(encoding='utf-8')
    blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    print(f'file            : {PAGE.name}  ({html.count(chr(10)) + 1} lines, {len(html)} chars)')
    print(f'script blocks   : {len(blocks)}')
    print(f'external refs   : {len(re.findall(r"https?://", html))} '
          f'(inside data-URI SVG namespaces only)')
    print(f'maxlength attrs : {len(re.findall("maxlength", html))} (must be 0: IME input)')

    problems = []
    for word in FORBIDDEN:
        if word in html:
            problems.append(f'forbidden destructive hook still present: {word}')
    for element in EXPECTED_IDS:
        if f'id="{element}"' not in html and f"'{element}'" not in html:
            problems.append(f'expected element missing: {element}')
    if not re.search(r'function bindLoopCard', html):
        problems.append('bindLoopCard() is missing')
    # The timer may only touch pure-display pages; the train page holds the
    # explore results, the drill-down and every form, and the bans page holds
    # forms too, so a full re-render there would wipe the user's work.
    if 'AUTO_PAGES' not in html or 'if (!AUTO_PAGES[state.page]) return;' not in html:
        problems.append('auto-refresh is not restricted to an AUTO_PAGES allowlist')
    if re.search(r'AUTO_PAGES\s*=\s*\{[^}]*\b(train|bans)\s*:', html):
        problems.append('AUTO_PAGES must not include the train or bans pages')
    if 'function typingNow' not in html:
        problems.append('typingNow() guard is missing')

    if not NODE.is_file():
        problems.append(f'node not found at {NODE}; syntax check skipped')
    else:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'inline.js'
            target.write_text('\n;\n'.join(blocks), encoding='utf-8')
            result = subprocess.run([str(NODE), '--check', str(target)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                problems.append('inline script syntax error:\n' +
                                (result.stderr or result.stdout).strip())
            else:
                print('inline JS syntax: OK')

    if problems:
        print('\nPROBLEMS:')
        for item in problems:
            print('  - ' + item)
        return 1
    print('\nAll UI checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
