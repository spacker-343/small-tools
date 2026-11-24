"""Version-gated, reversible local Electron patch installer. No binary patches."""
import argparse
import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

from capabilities import check_server

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / '.local'
MARKER = '// AGY_CHAT_EDITOR_V1'
BRIDGE = '''
// AGY_CHAT_EDITOR_V1
require('electron').contextBridge.exposeInMainWorld('agyChatEditor', Object.freeze({
  invoke: (data) => require('electron').ipcRenderer.invoke('agy-chat-editor:invoke', data)
}));
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unpack(data):
    if len(data) < 16:
        raise ValueError('ASAR 文件不完整')
    tag, header_size, pickle_size, json_size = struct.unpack('<4I', data[:16])
    if tag != 4 or header_size != pickle_size + 4 or json_size > pickle_size - 4:
        raise ValueError('不支持的 ASAR 格式')
    header = json.loads(data[16:16 + json_size])
    entries = {}
    def walk(node, prefix=''):
        for name, entry in node['files'].items():
            key = prefix + name
            if 'files' in entry:
                walk(entry, key + '/')
            elif 'offset' in entry:
                start = 8 + header_size + int(entry['offset'])
                value = data[start:start + entry['size']]
                if len(value) != entry['size']:
                    raise ValueError('ASAR 内容不完整')
                entries[key] = value
    walk(header)
    return header, entries


def repack(header, entries, changed):
    header = copy.deepcopy(header)
    output = bytearray()
    def walk(node, prefix=''):
        for name, entry in node['files'].items():
            key = prefix + name
            if 'files' in entry:
                walk(entry, key + '/')
            elif 'offset' in entry:
                content = entries[key]
                entry['offset'] = str(len(output))
                entry['size'] = len(content)
                if key in changed:
                    block = entry.get('integrity', {}).get('blockSize', 4194304)
                    entry['integrity'] = {'algorithm': 'SHA256', 'hash': sha(content), 'blockSize': block,
                                          'blocks': [sha(content[i:i+block]) for i in range(0, len(content), block)]}
                output.extend(content)
    walk(header)
    raw = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    payload = struct.pack('<I', len(raw)) + raw
    payload += b'\0' * (-len(payload) % 4)
    pickle_data = struct.pack('<I', len(payload)) + payload
    return struct.pack('<II', 4, len(pickle_data)) + pickle_data + output


def running(executable):
    # Pass the path through the environment, never through PowerShell source.
    command = "@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $env:AGY_CHAT_EDITOR_TARGET }).Count"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                             command], env={**os.environ, 'AGY_CHAT_EDITOR_TARGET': str(executable)},
                            capture_output=True, text=True, check=True)
    return int(result.stdout.strip()) > 0


def main():
    parser = argparse.ArgumentParser(description='安装、检查或卸载 Antigravity AI 回复编辑补丁')
    parser.add_argument('action', choices=['check', 'install', 'uninstall', 'launch'])
    parser.add_argument('--app', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/antigravity')
    args = parser.parse_args()
    application = args.app.resolve()
    executable = application / 'Antigravity.exe'
    archive = application / 'resources/app.asar'
    data = archive.read_bytes()
    header, entries = unpack(data)
    main_js = entries['dist/main.js'].decode('utf-8')
    preload_js = entries['dist/preload.js'].decode('utf-8')
    installed = MARKER in main_js
    # Inspect Electron's actual fuse state, without changing security settings.
    fuse = executable.read_bytes()
    at = fuse.find(b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX')
    if at < 0 or len(fuse) <= at + 38 or fuse[at+32] != 1 or fuse[at+33] < 5 or fuse[at+38] != ord('0'):
        raise ValueError('无法确认 ASAR 完整性策略；不修改程序，也不关闭校验')
    app_version = json.loads(entries['package.json']).get('version')
    print(f'应用版本: {app_version} | Electron: {(application / "version").read_text().strip()}')
    print('补丁状态:', '已安装' if installed else '未安装')
    print('会话目录:', Path.home() / '.gemini/antigravity/conversations')
    if args.action != 'uninstall':
        check_server((application / 'resources/bin/language_server.exe').read_bytes())
        print('协议和界面结构: 兼容（未限制版本号）')
    if args.action == 'check':
        return
    if args.action == 'launch' and running(executable):
        if not installed:
            raise ValueError('更新覆盖了补丁。请先退出 Antigravity，再通过启动脚本自动恢复')
        subprocess.Popen([str(executable)], close_fds=True)
        return
    if os.name != 'nt' or running(executable):
        raise ValueError('请先从菜单完全退出 Antigravity，再运行安装或卸载脚本')
    ledger_path = LOCAL / 'installation.json'
    if args.action == 'uninstall':
        if not installed:
            print('当前程序未安装此补丁，无需卸载。')
            return
        ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
        backup = Path(ledger['backup']).read_bytes()
        if sha(data) != ledger['patchedHash'] or sha(backup) != ledger['originalHash']:
            raise ValueError('程序或备份已变化，拒绝覆盖；请重新安装官方程序')
        replace_archive(archive, backup)
        print('已还原原始 app.asar。会话编辑历史和备份仍保留在 .local 中。')
        return
    if installed:
        print('补丁已经安装。源码改动在下次启动时加载。')
        if args.action == 'launch':
            subprocess.Popen([str(executable)], close_fds=True)
        return
    package = json.loads(entries['package.json'])
    if package.get('name') != 'antigravity':
        raise ValueError('目标不是 Antigravity')
    server_source = entries['dist/languageServer.js'].decode()
    for symbol in ('getLsBaseUrl', 'getLsProcess', 'getLsCsrfToken'):
        if f'exports.{symbol}' not in server_source:
            raise ValueError(f'缺少本地服务能力: {symbol}')
    if 'contextBridge' not in preload_js or MARKER in preload_js:
        raise ValueError('预加载脚本与预期不符')
    addition = '\n' + MARKER + '\ntry { require(' + json.dumps(str(ROOT / 'main.cjs')) + ").start(require('./languageServer')); } catch (error) { console.error('AI reply editor could not load', error); }\n"
    entries['dist/main.js'] += addition.encode('utf-8')
    entries['dist/preload.js'] += BRIDGE.encode('utf-8')
    changed = {'dist/main.js', 'dist/preload.js'}
    patched = repack(header, entries, changed)
    _, check = unpack(patched)
    if any(check[key] != value for key, value in entries.items()):
        raise ValueError('ASAR 回读校验失败')
    LOCAL.mkdir(exist_ok=True)
    backups = LOCAL / 'backups/application'
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / (sha(data) + '.asar')
    if backup.exists() and sha(backup.read_bytes()) != sha(data):
        raise ValueError('原程序备份校验失败')
    backup.write_bytes(data)
    (LOCAL / 'config.json').write_text(json.dumps({'python': sys.executable}), encoding='utf-8')
    ledger = {'backup': str(backup), 'originalHash': sha(data), 'patchedHash': sha(patched), 'app': str(application)}
    (LOCAL / 'installation.json').write_text(json.dumps(ledger, indent=2), encoding='utf-8')
    replace_archive(archive, patched)
    print('补丁安装完成。现在启动 Antigravity，AI 回复气泡旁将出现“编辑”。')
    if args.action == 'launch':
        subprocess.Popen([str(executable)], close_fds=True)


def replace_archive(path, data):
    temp = path.with_name('app.chat-editor.tmp')
    with temp.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('操作失败:', error)
        sys.exit(1)
