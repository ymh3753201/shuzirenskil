"""OS credential store; no plaintext fallback and no secret command-line argument."""
import argparse
import getpass
import re
import sys

SERVICE = 'shuzirenskil'

def backend():
    try:
        import keyring
    except ImportError:
        raise ValueError('请在运行 Skill 的同一 Python 环境安装 keyring：python -m pip install keyring') from None
    store = keyring.get_keyring()
    allowed = {'win32': 'keyring.backends.Windows', 'darwin': 'keyring.backends.macOS', 'linux': 'keyring.backends.SecretService'}
    if type(store).__module__ != allowed.get(sys.platform):
        raise ValueError('没有可用的系统安全凭据库；请配置系统凭据库，禁止自动退回明文文件')
    return store

def read(name):
    try:
        return backend().get_password(SERVICE, name) or ''
    except (ImportError, ValueError):
        return ''
    except Exception:
        raise ValueError('系统凭据库读取失败；请解锁并检查访问权限') from None

def save(name, value, replace=False):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) or not value.strip() or any(c in value for c in '\r\n'):
        raise ValueError('密钥变量名或输入无效')
    store = backend()
    if store.get_password(SERVICE, name) and not replace:
        raise ValueError('已有此服务的密钥；核对后用 --replace 显式替换')
    store.set_password(SERVICE, name, value.strip())
    if store.get_password(SERVICE, name) != value.strip():
        raise ValueError('密钥回读验证失败')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['set', 'status'])
    p.add_argument('--name', required=True)
    p.add_argument('--replace', action='store_true')
    a = p.parse_args()
    if a.action == 'set':
        if not sys.stdin.isatty():
            raise ValueError('请在本机交互终端输入密钥，不要把密钥放命令参数或脚本')
        save(a.name, getpass.getpass('粘贴 API 密钥（输入不显示）：'), a.replace)
        print('密钥已保存到系统凭据库，回读验证通过；未调用模型')
    else:
        print('已配置' if backend().get_password(SERVICE, a.name) else '未配置')

if __name__ == '__main__':
    try: main()
    except Exception:
        print('配置未完成：请检查 keyring 依赖、系统凭据库和变量名；已有密钥请用 --replace。未输出密钥。', file=sys.stderr)
        sys.exit(1)
