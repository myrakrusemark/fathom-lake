#!/usr/bin/env python3
"""Install/update this same Lake plugin in Codex's personal marketplace.

Does not trust hooks or alter Claude configuration. Existing direct Lake MCP and
user hooks are left untouched; remove duplicates only after plugin verification.
"""
import json
from pathlib import Path
import shutil
import subprocess

source = Path(__file__).resolve().parents[1]
home = Path.home()
helpers = home / '.codex/skills/.system/plugin-creator/scripts'
target = home / 'plugins/lake'
marketplace = home / '.agents/plugins/marketplace.json'
if not target.exists():
    subprocess.run(['python3', str(helpers / 'create_basic_plugin.py'), 'lake', '--with-marketplace'], check=True)
name = subprocess.check_output(['python3', str(helpers / 'read_marketplace_name.py')], text=True).strip()
entries = json.loads(marketplace.read_text()).get('plugins', [])
if not any(e.get('name') == 'lake' and e.get('source', {}).get('path') == './plugins/lake' for e in entries):
    raise SystemExit('Existing personal Lake entry differs; inspect before replacing it.')
shutil.copytree(source, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
# Current installed Codex CLI uses legacy component discovery. Materialize its
# variant from the same shared plugin sources without changing Claude's files.
(target / 'plugin.json').unlink(missing_ok=True)
shutil.copyfile(source / 'codex/hooks.json', target / 'hooks/hooks.json')
shutil.copyfile(source / 'mcp.json', target / '.mcp.json')
subprocess.run(['python3', str(helpers / 'update_plugin_cachebuster.py'), str(target)], check=True)
subprocess.run(['python3', str(helpers / 'validate_plugin.py'), str(target)], check=True)
result = json.loads(subprocess.check_output(['codex', 'plugin', 'add', 'lake@' + name, '--json'], text=True))
print(json.dumps(result, indent=2))
# Codex 0.155 does not expand plugin-root variables inside MCP argv. Resolve
# the path after installation, leaving hook definitions and trust untouched.
installed = Path(result['installedPath'])
for root in (target, installed):
    for filename in ('.mcp.json', 'mcp.json'):
        config_path = root / filename
        config = json.loads(config_path.read_text())
        config['mcpServers']['lake-memory']['args'] = [str(installed / 'mcp/server.py')]
        config_path.write_text(json.dumps(config, indent=2) + '\n')
print('Installed. In a fresh Codex CLI, review the three Lake plugin commands with /hooks and trust each. No trust bypass was applied.')
