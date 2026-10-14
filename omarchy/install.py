#!/usr/bin/python3
"""Install the Omarchy integration without root or changes to packaged files."""
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
home = Path.home()
config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
bin_dir = home / '.local/bin'
bin_dir.mkdir(parents=True, exist_ok=True)
# Honor the shell config that is actually running (including user compatibility copies).
shell_root = Path(os.environ.get('OMARCHY_PATH', '/usr/share/omarchy'))
instances = subprocess.run(['qs', 'list', '--all'], capture_output=True, text=True, check=True).stdout
for line in instances.splitlines():
    if 'Config path:' in line:
        entry = Path(line.split('Config path:', 1)[1].strip())
        if entry.name == 'shell.qml' and entry.parent.name == 'shell':
            shell_root = entry.parent.parent
            break
shell_env = dict(os.environ, OMARCHY_PATH=str(shell_root))


def backup(path):
    if path.exists():
        shutil.copy2(path, str(path) + '.bak.' + datetime.now().strftime('%Y%m%d%H%M%S'))


if not (repo / 'target/release/recall').exists():
    raise SystemExit('Build the engine first: scripts/build-llama.sh && cargo build --release')
# The checkout is the installation source, including its pinned shared libraries.
for name, body in {
    'omarchy-recall-shell': f'export OMARCHY_PATH="{shell_root}"\nexec omarchy-shell "$@"',
    'omarchy-recall-client': f'exec /usr/bin/python3 "{repo}/omarchy/client.py" "$@"',
    'recall': f'exec "{repo}/target/release/recall" "$@"',
    'omarchy-recall': f'exec /usr/bin/python3 "{repo}/omarchy/cli.py" "$@"',
}.items():
    path = bin_dir / name
    backup(path)
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


# Install the embedded skill through the same entry point agents use.
subprocess.run(['/usr/bin/python3', str(repo / 'omarchy/cli.py'), 'skill', 'install'], check=True)

settings_path = config / 'recall/settings.json'
settings_path.parent.mkdir(parents=True, exist_ok=True)
if not settings_path.exists():
    settings_path.write_text(json.dumps({
        'roots': [str(home / name) for name in ('Documents', 'Pictures') if (home / name).is_dir()],
        'code_roots': [], 'code_editor': 'nvim', 'hotkey': '',
        'onboarded': True, 'encoder_idle_seconds': 120,
        'index_directory': str(data / 'recall/index'), 'models_directory': str(data / 'recall/Models'),
    }, indent=2) + '\n')
    settings_path.chmod(0o600)

desktop = data / 'applications/io.github.pdstn12.Recall.desktop'
desktop.parent.mkdir(parents=True, exist_ok=True)
desktop.write_text((repo / 'app/data/io.github.pdstn12.Recall.desktop').read_text().replace('@EXEC@', str(repo / 'app/recall-app')))
icon = data / 'icons/hicolor/scalable/apps/io.github.pdstn12.Recall.svg'
icon.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(repo / 'app/data/io.github.pdstn12.Recall.svg', icon)

plugin = config / 'omarchy/plugins/ferdous.recall'
plugin.mkdir(parents=True, exist_ok=True)
for source in (repo / 'omarchy/plugin').iterdir():
    backup(plugin / source.name)
    if source.suffix == '.qml' and 'singleton ShellColor' in (shell_root / 'shell/Commons/qmldir').read_text():
        (plugin / source.name).write_text(source.read_text().replace('Color.menu', 'ShellColor.menu'))
    else:
        shutil.copy2(source, plugin / source.name)

service = config / 'systemd/user/omarchy-recall.service'
service.parent.mkdir(parents=True, exist_ok=True)
backup(service)
service.write_text(f'''[Unit]
Description=Recall local semantic search for Omarchy
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
Environment=RECALL_NO_SCOPE=1
Environment=RECALL_OMARCHY=1
ExecStart="{repo}/app/recall-app" --background
Restart=on-failure
RestartSec=5
Nice=10
UMask=0077
TimeoutStopSec=15

[Install]
WantedBy=graphical-session.target
''')

bindings = config / 'hypr/bindings.lua'
marker = '-- omarchy-recall: start'
if bindings.exists() and marker not in bindings.read_text():
    backup(bindings)
    with bindings.open('a') as file:
        file.write('\n' + marker + '\no.bind("SUPER + CTRL + SHIFT + F", "Recall semantic search", "omarchy-recall toggle")\n-- omarchy-recall: end\n')
    subprocess.run(['hyprctl', 'reload'], check=True)
    subprocess.run(['hyprctl', 'configerrors'], check=True)

subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', 'omarchy-recall.service'], check=True)
subprocess.run(['systemctl', '--user', 'restart', 'omarchy-recall.service'], check=True)
subprocess.run(['omarchy-shell', 'shell', 'rescanPlugins'], check=True, env=shell_env)
subprocess.run(['omarchy', 'plugin', 'enable', 'ferdous.recall'], check=True, env=shell_env)
print('Installed. Super+Ctrl+Shift+F opens Recall. omarchy-recall settings chooses folders.')
