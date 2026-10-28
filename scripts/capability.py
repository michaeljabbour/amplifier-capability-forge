#!/usr/bin/env python3
"""Build, check, install and inspect a self-contained capability release.

Python standard library only. Native tools keep their own install/runtime.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.0.0'
E = html.escape

def config():
    data = json.loads((ROOT / 'capability.json').read_text())
    for key in ('id', 'display_name', 'kind', 'promise'):
        if not data.get(key):
            raise ValueError('Missing capability field: ' + key)
    return data

def within(path, parent):
    value = path.resolve()
    if not value.is_relative_to(parent.resolve()):
        raise ValueError('Path escapes its package: ' + str(path))
    return value

def skill_root(data):
    return within(ROOT / data['skill_path'], ROOT) if data.get('skill_path') else None

def portable_files(data):
    folder = skill_root(data)
    if folder is None:
        return []
    if not (folder / 'SKILL.md').is_file():
        raise ValueError('Declared portable skill has no entrypoint')
    if folder == ROOT and not data.get('portable_include'):
        raise ValueError('A root-level skill needs an explicit portable_include list')
    candidates = []
    for name in data.get('portable_include', ['.']):
        selected = within(folder / name, folder)
        candidates.extend(selected.rglob('*') if selected.is_dir() else [selected])
    files = []
    for path in sorted(set(candidates)):
        if path.is_symlink():
            raise ValueError('Portable package contains a symlink: ' + str(path))
        if path.is_file() and not any(x in {'.git', '__pycache__', '.DS_Store'} for x in path.relative_to(folder).parts):
            if path.name.startswith('.env'):
                raise ValueError('Environment files do not belong in a portable release')
            files.append(path)
    entries = [p for p in files if p.name == 'SKILL.md']
    if len(entries) != 1:
        raise ValueError('The portable package must have exactly one canonical SKILL.md')
    for path in files:
        if path.suffix != '.md':
            continue
        for ref in re.findall(r'(?<!!)\[[^\]]+\]\(([^)\s]+)\)', path.read_text()):
            uri = urlsplit(ref)
            if uri.scheme or not uri.path:
                continue
            target = within(path.parent / unquote(uri.path), folder)
            if not target.exists():
                raise ValueError('Missing installed reference: ' + str(target))
    if not any('LICENSE' in p.name.upper() for p in files):
        raise ValueError('The portable package must carry its license')
    return files

def package_name(data):
    folder = skill_root(data)
    return data.get('skill_name') or (folder.name if folder != ROOT else data['id'])

def safe_url(value):
    value = str(value)
    uri = urlsplit(value)
    return value if not value.startswith('//') and uri.scheme in ('', 'http', 'https') else '#'

def inline(text):
    result = E(str(text))
    result = re.sub(r'`([^`]+)`', r'<code>\1</code>', result)
    result = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', result)
    result = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', lambda m: '<a href="' + E(safe_url(html.unescape(m[2])), quote=True) + '">' + m[1] + '</a>', result)
    return result

def markdown(text):
    out = []; code = []; in_code = False; listing = False
    for line in text.splitlines():
        if line.startswith('```'):
            if in_code:
                out.append('<pre><code>' + E('\n'.join(code)) + '</code></pre>'); code = []
            in_code = not in_code
            continue
        if in_code:
            code.append(line); continue
        match = re.match(r'^(#{1,6})\s+(.+)', line)
        bullet = re.match(r'^\s*(?:[-*]|\d+\.)\s+(.+)', line)
        if listing and not bullet:
            out.append('</ul>'); listing = False
        if match:
            level = min(len(match[1]) + 1, 6)
            out.append(f'<h{level}>' + inline(match[2]) + f'</h{level}>')
        elif bullet:
            if not listing:
                out.append('<ul>'); listing = True
            out.append('<li>' + inline(bullet[1]) + '</li>')
        elif line.strip():
            out.append('<p>' + inline(line) + '</p>')
    if listing: out.append('</ul>')
    if code: out.append('<pre>' + E('\n'.join(code)) + '</pre>')
    return '\n'.join(out)

def build(data):
    output = ROOT / '_site'
    if output.exists(): shutil.rmtree(output)
    output.mkdir()
    assets = within(ROOT / data.get('site_assets', 'site/capability'), ROOT)
    for name in ('style.css', 'site.js'):
        shutil.copyfile(assets / name, output / name)
    shutil.copytree(assets / 'theme', output / 'theme')
    docs = list(dict.fromkeys(['README.md', 'LICENSE', 'CHANGELOG.md', 'capability.json'] + data.get('docs', [])))
    exports = []
    for name in docs:
        source = within(ROOT / name, ROOT)
        if source.is_file():
            dest = output / name; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest); exports.append(name)
    files = portable_files(data); folder = skill_root(data)
    download = ''
    if files:
        dest = output / 'downloads'; dest.mkdir()
        archive = dest / (data['id'] + '-skill.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as handle:
            for path in files:
                info = zipfile.ZipInfo(package_name(data) + '/' + path.relative_to(folder).as_posix(), (2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                handle.writestr(info, path.read_bytes())
        download = '<a class="button" href="downloads/' + E(archive.name) + '">Download the portable skill</a>'
        for path in files:
            dest = output / data['skill_path'] / path.relative_to(folder); dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    modes = ''.join('<article><h3>' + E(str(m.get('name', ''))) + '</h3><p>' + E(str(m.get('when', ''))) + '</p><p class="muted">' + E(str(m.get('output', ''))) + '</p></article>' for m in data.get('modes', []))
    examples = data.get('examples', [])
    if any(x.get('fictional') is not True for x in examples):
        raise ValueError('Every illustrative example must be explicitly fictional')
    options = ''.join(f'<option value="{i}">' + E(str(x.get('title', 'Example'))) + '</option>' for i, x in enumerate(examples))
    panels = ''.join('<article class="example" id="example-' + str(i) + '"' + (' hidden' if i else '') + '><p class="eyebrow">Fictional example</p><h3>' + E(str(x.get('title', ''))) + '</h3><p><strong>The brief</strong> ' + E(str(x.get('brief', ''))) + '</p><div class="comparison"><div><h4>Before</h4><p>' + E(str(x.get('before', ''))) + '</p></div><div><h4>After applying the method</h4><p>' + E(str(x.get('after', ''))) + '</p></div></div><p>' + E(str(x.get('why', ''))) + '</p></article>' for i, x in enumerate(examples))
    example_ui = '<label for="example-select">Choose a fictional task</label><select id="example-select">' + options + '</select>' + panels if examples else '<p>Use the documented task checks below. No worked outcome is claimed.</p>'
    if folder:
        source = (folder / 'SKILL.md').read_text().split('---', 2)[-1]
        def resolve_method_link(match):
            ref = match[2]; uri = urlsplit(ref)
            if uri.scheme or uri.netloc: return match[0]
            base = (folder / uri.path).relative_to(ROOT).as_posix() if uri.path else (folder / 'SKILL.md').relative_to(ROOT).as_posix()
            return '[' + match[1] + '](' + base + ('#' + uri.fragment if uri.fragment else '') + ')'
        source = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', resolve_method_link, source)
        method = markdown(source)
    else:
        method = markdown(data.get('method_summary', 'Use the native tool and its documented command interface.'))
    commands = data.get('install_commands', [])
    if len(commands) > 3: raise ValueError('The primary install has more than three commands')
    install = '<pre><code>' + E('\n'.join(commands)) + '</code></pre>' if commands else '<p>Read the native installation guide linked below.</p>'
    native = data.get('runtime_install_commands', [])
    if native:
        install += '<details><summary>Native tools and optional execution</summary><pre><code>' + E('\n'.join(native)) + '</code></pre><p>These commands install the execution backend. The portable method is a separate surface.</p></details>'
    evidence = E(data.get('evidence_summary', 'Source and conformance checks are separate from demonstrated benefit. Comparative effectiveness and outside-user outcomes are not established by this site.'))
    checks = ''.join('<li><strong>' + E(str(v.get('name', 'Check'))) + '</strong> — ' + E(str(v.get('status', 'planned'))) + '<p>' + E(str(v.get('evidence', v.get('expected', '')))) + '</p></li>' for v in data.get('verification', []))
    doc_links = ' · '.join('<a href="' + E(name, quote=True) + '">' + E(Path(name).stem.replace('-', ' ')) + '</a>' for name in exports)
    boundaries = ''.join('<li>' + E(str(x)) + '</li>' for x in data.get('boundaries', []))
    status = E(data.get('publication_status', 'Local release candidate. Publication and real-user acceptance are separate.'))
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>''' + E(data['display_name']) + ''' | Amplifier</title><meta name="description" content="''' + E(data['promise'], quote=True) + '''"><link rel="stylesheet" href="style.css"><script src="site.js" defer></script></head><body><a class="skip" href="#main">Skip to content</a><header><a href="#main" class="brand">Amplifier<span>Skills and tools for thoughtful work</span></a><nav aria-label="Main navigation"><a href="#try">See it work</a><a href="#method">Method</a><a href="#install">Install</a><a href="#evidence">Evidence</a></nav></header><main id="main"><section class="hero"><div><p class="eyebrow">''' + E(data['kind'].replace('-', ' ')) + '''</p><h1>''' + E(data['display_name']) + '''</h1></div><div><p class="promise">''' + E(data['promise']) + '''</p><p>''' + E(str(data.get('audience', ''))) + '''</p><p>''' + E(str(data.get('trigger', ''))) + '''</p><a class="button" href="#install">Start here</a><p class="muted">''' + status + '''</p></div></section><section id="try"><p class="eyebrow">One brief, visible choices</p><h2>See the difference.</h2>''' + example_ui + '''<p class="muted">Authored illustrations show the intended method. They are not measured results.</p></section><section id="method"><p class="eyebrow">One coherent job</p><h2>Keep the useful judgment.</h2><div class="mode-grid">''' + modes + '''</div><details><summary>Read the canonical method</summary><div class="method">''' + method + '''</div></details><ul>''' + boundaries + '''</ul></section><section id="install"><p class="eyebrow">Choose the surface you need</p><h2>Make it yours.</h2>''' + download + install + '''<p>Keep the complete skill folder together, including its references and license. The host applies the method in your context; the skill does not start a background service.</p><p>''' + doc_links + '''</p></section><section id="evidence"><p class="eyebrow">Proof before a claim</p><h2>What has been checked?</h2><p>''' + evidence + '''</p><ul class="checks">''' + checks + '''</ul><p>Real-user outcomes remain unmeasured unless a dated record explicitly establishes them.</p></section></main><footer><strong>''' + E(data['display_name']) + '''</strong><p>''' + doc_links + '''</p><p>MIT theme attribution retained. No institutional affiliation or endorsement implied.</p></footer></body></html>'''
    (output / 'index.html').write_text(page)
    receipt = {'date': datetime.now(timezone.utc).isoformat(), 'tool_version': VERSION, 'kind': 'local packaging conformance', 'portable_files': len(files), 'capability_sha256': hashlib.sha256((ROOT / 'capability.json').read_bytes()).hexdigest(), 'benefit': 'not measured by build'}
    (output / 'build-record.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'built': str(output), 'portable_files': len(files), 'examples': len(examples)}))

def check(data):
    files = portable_files(data); output = ROOT / '_site'
    if not (output / 'index.html').exists(): raise ValueError('Build the site before checking')
    class Page(HTMLParser):
        def __init__(self): super().__init__(); self.links = []; self.ids = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if 'id' in attrs: self.ids.append(attrs['id'])
            for key in ('href', 'src'):
                if key in attrs: self.links.append(attrs[key])
    page = Page(); page.feed((output / 'index.html').read_text())
    if len(page.ids) != len(set(page.ids)): raise ValueError('Duplicate page IDs')
    for ref in page.links:
        uri = urlsplit(ref)
        if uri.scheme or uri.netloc: continue
        if uri.path and not within(output / unquote(uri.path), output).exists(): raise ValueError('Broken site link: ' + ref)
        if not uri.path and uri.fragment not in page.ids: raise ValueError('Missing anchor: ' + ref)
    if files:
        folder = skill_root(data)
        with zipfile.ZipFile(output / 'downloads' / (data['id'] + '-skill.zip')) as archive:
            expected = {package_name(data) + '/' + p.relative_to(folder).as_posix(): p.read_bytes() for p in files}
            if set(archive.namelist()) != set(expected): raise ValueError('ZIP membership drift')
            if any(archive.read(name) != value for name, value in expected.items()): raise ValueError('ZIP content drift')
    for name in ('style.css', 'theme/style.css'):
        if re.search(r'@import|url\(\s*[\"\x27]?(?:https?:|//)', (output / name).read_text()): raise ValueError('External runtime asset')
    if not (output / 'theme/LICENSE').is_file(): raise ValueError('Missing theme license')
    print(json.dumps({'check': 'passed', 'portable_files': len(files), 'local_links': len(page.links), 'claim': 'Conformance only; not efficacy'}))

def install(data, destination, replace=False):
    files = portable_files(data); source = skill_root(data)
    if source is None: raise ValueError('Use this capability\'s native installation commands')
    parent = destination.expanduser().resolve(); target = parent / package_name(data)
    if target.is_symlink(): raise ValueError('Refusing a symlink skill destination')
    identical = target.is_dir() and all((target / p.relative_to(source)).is_file() and (target / p.relative_to(source)).read_bytes() == p.read_bytes() for p in files)
    if target.exists() and not identical:
        if not replace: raise ValueError('Existing skill differs. Review it or use --replace to retain a dated backup.')
        backup = target.with_name(target.name + '.backup-' + datetime.now().strftime('%Y%m%d%H%M%S%f'))
        target.rename(backup); print('Retained backup: ' + str(backup))
    if not identical:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.capability-install-', dir=parent) as tmp:
            staging = Path(tmp) / package_name(data); staging.mkdir()
            for path in files:
                dest = staging / path.relative_to(source); dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, dest)
            staging.rename(target)
    print(json.dumps({'installed': str(target), 'files': len(files), 'unchanged': identical}))

def doctor(data):
    files = portable_files(data)
    print(json.dumps({'capability': data['id'], 'portable_method': 'valid' if files else 'native tool; no portable method', 'portable_files': len(files), 'native_install_commands': data.get('runtime_install_commands', []), 'runtime_verified_by_doctor': False, 'explanation': 'This checks packaging, not credentials, remote providers, model quality or production behavior.'}))

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('operation', choices=('build', 'check', 'install', 'doctor'))
    parser.add_argument('--dest', type=Path); parser.add_argument('--replace', action='store_true'); args = parser.parse_args()
    try:
        data = config()
        if args.operation == 'build': build(data)
        elif args.operation == 'check': check(data)
        elif args.operation == 'doctor': doctor(data)
        else:
            if args.dest is None: parser.error('install needs --dest pointing at the host skills parent directory')
            install(data, args.dest, args.replace)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr); return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
