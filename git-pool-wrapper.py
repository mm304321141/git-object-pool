#!/usr/bin/env python3
"""
Git Wrapper - 全局对象池共享方案（自驱 submodule 版）
拦截 clone / submodule update --init / fetch / gc / prune，实现跨仓库对象共享
"""

import os
import sys
import subprocess
import fcntl
import re
import shutil
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from concurrent.futures import ThreadPoolExecutor

# ---------- 配置 ----------
GIT_POOL = os.environ.get('GIT_POOL', os.path.expanduser('~/.git-pool'))
POOL_LOCK = Path(GIT_POOL) / '.lock'
REGISTRY_FILE = Path(GIT_POOL) / '.registered_shells'
SYSTEM_GIT = '/usr/bin/git'

_registry_lock = threading.Lock()


# ---------- 工具函数 ----------
def init_pool():
    Path(GIT_POOL).mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.touch(exist_ok=True)
    POOL_LOCK.touch(exist_ok=True)


def _sanitize_url(url: str) -> str:
    """去除 URL 中可能混入的零宽字符及不可见控制字符（Unicode category Cf/Cc/Cs）。"""
    return ''.join(c for c in url if unicodedata.category(c) not in ('Cf', 'Cc', 'Cs'))


def normalize_url(url: str) -> str:
    """归一化 URL 为真实路径分级风格的 key（不做 URL 编码）。
    例：
      https://host/storage/foo.git    -> host/storage/foo
      git@host:storage/foo.git        -> host/storage/foo
      ssh://git@host/storage/foo.git  -> host/storage/foo
    """
    # 1) 去掉协议前缀
    url = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', '', url)
    # 2) 去掉 user@ 前缀
    url = re.sub(r'^[^@/]+@', '', url)
    # 3) host:path -> host/path（仅替换首个 ':'，且仅当 ':' 出现在第一个 '/' 之前）
    slash_idx = url.find('/')
    colon_idx = url.find(':')
    if colon_idx != -1 and (slash_idx == -1 or colon_idx < slash_idx):
        url = url[:colon_idx] + '/' + url[colon_idx + 1:]
    # 4) 去掉末尾 / 再去掉末尾 .git
    url = url.rstrip('/')
    url = re.sub(r'\.git$', '', url)
    # 5) 折叠多余连续斜杠
    url = re.sub(r'/+', '/', url)
    return url.lower().strip('/')


def pool_repo_path(url: str) -> Path:
    return Path(GIT_POOL) / normalize_url(url)


def acquire_lock():
    lock_fd = open(POOL_LOCK, 'w')
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def release_lock(lock_fd):
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()


def run_git(args: List[str], capture=False, check=True, env=None):
    if env is None:
        env = os.environ.copy()
    cmd = [SYSTEM_GIT] + args
    if capture:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    else:
        result = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr, end='' if result.stderr.endswith('\n') else '\n')
        sys.exit(result.returncode)
    return result


def exec_git(args: List[str]):
    os.execve(SYSTEM_GIT, [SYSTEM_GIT] + args, os.environ.copy())


def register_shell(gitdir: str):
    gitdir = os.path.abspath(gitdir)
    if not os.path.isdir(gitdir):
        return
    with _registry_lock:
        if REGISTRY_FILE.exists():
            with open(REGISTRY_FILE, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
        else:
            lines = []
        if gitdir not in lines:
            with open(REGISTRY_FILE, 'a') as f:
                f.write(gitdir + '\n')


def clean_registry():
    if not REGISTRY_FILE.exists():
        return
    with open(REGISTRY_FILE, 'r') as f:
        lines = [line.strip() for line in f if line.strip()]
    valid = [l for l in lines if os.path.isdir(l)]
    with open(REGISTRY_FILE, 'w') as f:
        f.write('\n'.join(valid) + ('\n' if valid else ''))


def find_gitdir(path: str) -> Optional[str]:
    path = Path(path).resolve()
    while path != path.parent:
        gitpath = path / '.git'
        if gitpath.exists():
            if gitpath.is_dir():
                return str(gitpath.resolve())
            if gitpath.is_file():
                with open(gitpath, 'r') as f:
                    content = f.read().strip()
                if content.startswith('gitdir:'):
                    gitdir = content[7:].strip()
                    if not os.path.isabs(gitdir):
                        gitdir = str((gitpath.parent / gitdir).resolve())
                    return gitdir
        path = path.parent
    return None


def parse_clone_args(args: List[str]) -> Tuple[List[str], List[str]]:
    passthrough_args = []
    positionals = []
    options_with_value = {
        '-b', '--branch', '--depth', '--origin', '-o', '--template', '--reference',
        '--reference-if-able', '--separate-git-dir', '-c', '--config', '--server-option',
        '--jobs', '-j', '--filter', '--shallow-since', '--shallow-exclude',
        '--upload-pack', '-u'
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            positionals.extend(args[i + 1:])
            break
        if arg.startswith('-'):
            passthrough_args.append(arg)
            opt = arg.split('=', 1)[0]
            if '=' not in arg and opt in options_with_value and i + 1 < len(args):
                i += 1
                passthrough_args.append(args[i])
        else:
            positionals.append(arg)
        i += 1
    return passthrough_args, positionals


def has_option(args: List[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + '=') for arg in args)


def parse_global_options(argv: List[str]) -> Tuple[List[str], str, List[str]]:
    global_opts = []
    args = argv[1:]
    options_with_value = {
        '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--exec-path', '--list-cmds'
    }
    flag_options = {
        '-v', '--version', '--help', '--html-path', '--man-path', '--info-path', '-p', '--paginate',
        '-P', '--no-pager', '--no-replace-objects', '--bare', '--no-optional-locks', '--no-advice'
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--':
            global_opts.append(arg)
            i += 1
            break
        if not arg.startswith('-'):
            return global_opts, arg, args[i + 1:]
        opt = arg.split('=', 1)[0]
        if arg in flag_options or any(arg.startswith(name + '=') for name in options_with_value):
            global_opts.append(arg)
        elif opt in options_with_value:
            global_opts.append(arg)
            if '=' not in arg and i + 1 < len(args):
                i += 1
                global_opts.append(args[i])
        else:
            return global_opts, arg, args[i + 1:]
        i += 1
    if i < len(args):
        return global_opts, args[i], args[i + 1:]
    return global_opts, '', []


def effective_cwd(global_opts: List[str]) -> str:
    cwd = os.getcwd()
    i = 0
    while i < len(global_opts):
        arg = global_opts[i]
        if arg == '-C' and i + 1 < len(global_opts):
            path = global_opts[i + 1]
            cwd = path if os.path.isabs(path) else os.path.abspath(os.path.join(cwd, path))
            i += 1
        i += 1
    return cwd


def ensure_pool_repo(url: str, pool_path: Path):
    """池裸仓 ensure：不存在则 bare clone，存在则 fetch；并发安全（文件锁）。"""
    lock_fd = acquire_lock()
    try:
        if not pool_path.exists():
            print(f'[Wrapper] 首次克隆裸仓到池: {pool_path}', file=sys.stderr)
            run_git(['clone', '--bare', url, str(pool_path)])
        else:
            result = run_git(['--git-dir', str(pool_path), 'rev-parse', '--git-dir'], capture=True, check=False)
            if result.returncode != 0:
                print(f'[Wrapper] 池中裸仓损坏，重新克隆: {pool_path}', file=sys.stderr)
                shutil.rmtree(pool_path)
                run_git(['clone', '--bare', url, str(pool_path)])
            else:
                run_git(['--git-dir', str(pool_path), 'fetch', '--all', '--prune'], capture=True, check=False)
    finally:
        release_lock(lock_fd)


def invalid_pool_url(url: str) -> bool:
    """返回 True 表示该 URL 不应进池（本地路径 / file:// / 相对路径）。
    注意：相对 URL 在调用前应先用 resolve_relative_url 解析为绝对 URL。"""
    return (not url or url.startswith('./') or url.startswith('../') or
            url.startswith('/') or url.startswith('file://'))


def add_alternate(gitdir: str, pool_path: Path):
    objects_dir = Path(gitdir) / 'objects'
    info_dir = objects_dir / 'info'
    info_dir.mkdir(parents=True, exist_ok=True)
    alternates = info_dir / 'alternates'
    alternate = str(pool_path / 'objects')
    lines = []
    if alternates.exists():
        with open(alternates, 'r') as f:
            lines = [line.strip() for line in f if line.strip()]
    if alternate not in lines:
        with open(alternates, 'a') as f:
            f.write(alternate + '\n')


# ---------- clone / fetch ----------
def do_clone(global_opts: List[str], subcmd_args: List[str]):
    clone_args = subcmd_args
    if (has_option(clone_args, '--reference-if-able') or has_option(clone_args, '--shared') or
            has_option(clone_args, '--bare') or has_option(clone_args, '--mirror') or
            has_option(clone_args, '--dissociate')):
        return exec_git(global_opts + ['clone'] + subcmd_args)

    passthrough_args, positionals = parse_clone_args(clone_args)
    if not positionals:
        return exec_git(global_opts + ['clone'] + subcmd_args)

    url = _sanitize_url(positionals[0])
    if invalid_pool_url(url):
        return exec_git(global_opts + ['clone'] + subcmd_args)

    dest = positionals[1] if len(positionals) > 1 else os.path.basename(url.rstrip('/')).removesuffix('.git')
    pool_path = pool_repo_path(url)
    ensure_pool_repo(url, pool_path)

    new_argv = global_opts + ['clone']
    new_argv.extend(passthrough_args)
    new_argv.append(f'--reference={pool_path}')
    new_argv.append(url)
    new_argv.append(dest)

    run_git(new_argv, capture=False)

    clone_cwd = effective_cwd(global_opts)
    new_gitdir = os.path.join(os.path.abspath(os.path.join(clone_cwd, dest)), '.git')
    if os.path.isdir(new_gitdir):
        register_shell(new_gitdir)
    elif os.path.isfile(new_gitdir):
        with open(new_gitdir, 'r') as f:
            content = f.read().strip()
        if content.startswith('gitdir:'):
            real_gitdir = content[7:].strip()
            if not os.path.isabs(real_gitdir):
                real_gitdir = str((Path(new_gitdir).parent / real_gitdir).resolve())
            register_shell(real_gitdir)


def extract_gitdir_override(global_opts: List[str]) -> Optional[str]:
    i = 0
    while i < len(global_opts):
        arg = global_opts[i]
        if arg == '--git-dir' and i + 1 < len(global_opts):
            return global_opts[i + 1]
        if arg.startswith('--git-dir='):
            return arg.split('=', 1)[1]
        i += 1
    return None


def do_fetch(global_opts: List[str], subcmd_args: List[str]):
    gitdir_override = extract_gitdir_override(global_opts)
    gitdir = gitdir_override if gitdir_override else find_gitdir(effective_cwd(global_opts))
    if not gitdir:
        return exec_git(global_opts + ['fetch'] + subcmd_args)
    # --unshallow / --update-shallow 不触发池更新，直接透传
    _DEPTH_FLAGS = {'--unshallow', '--update-shallow'}
    if _DEPTH_FLAGS.intersection(subcmd_args):
        return exec_git(global_opts + ['fetch'] + subcmd_args)
    if '--dry-run' in subcmd_args:
        return exec_git(global_opts + ['fetch'] + subcmd_args)
    # 从 subcmd_args 中取用户指定的 remote 名（第一个非选项参数）
    remote_name = 'origin'
    for a in subcmd_args:
        if not a.startswith('-'):
            remote_name = a
            break
    remote_url = run_git(['--git-dir', gitdir, 'config', '--get', f'remote.{remote_name}.url'],
                         capture=True, check=False).stdout.strip()
    remote_url = _sanitize_url(remote_url)
    if remote_url and not invalid_pool_url(remote_url):
        pool_path = pool_repo_path(remote_url)
        ensure_pool_repo(remote_url, pool_path)
    return exec_git(global_opts + ['fetch'] + subcmd_args)


# ---------- submodule（自驱方案） ----------
def resolve_relative_url(parent_remote: str, sub_url: str) -> str:
    """相对 URL（./../）相对父仓 remote.origin.url 解析为绝对 URL。"""
    if not (sub_url.startswith('./') or sub_url.startswith('../')):
        return sub_url
    if not parent_remote:
        return sub_url
    base = parent_remote.rstrip('/')
    parts = sub_url.split('/')
    for p in parts:
        if p == '..':
            base = base.rsplit('/', 1)[0]
        elif p == '.' or p == '':
            continue
        else:
            base = base + '/' + p
    return base


def parse_gitmodules(worktree_root: str) -> List[Dict[str, str]]:
    """解析 .gitmodules，返回 submodule 条目列表。"""
    gm = os.path.join(worktree_root, '.gitmodules')
    if not os.path.exists(gm):
        return []
    res = run_git(['-C', worktree_root, 'config', '--file', '.gitmodules', '--list'],
                  capture=True, check=False)
    if res.returncode != 0:
        return []
    subs: Dict[str, Dict[str, str]] = {}
    for line in res.stdout.splitlines():
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        if not key.startswith('submodule.'):
            continue
        # key 形如 submodule.<name>.<attr>，name 中可能含 '.'
        rest = key[len('submodule.'):]
        idx = rest.rfind('.')
        if idx < 0:
            continue
        name = rest[:idx]
        attr = rest[idx + 1:]
        subs.setdefault(name, {})[attr] = val
    result = []
    for name, attrs in subs.items():
        if 'path' not in attrs or 'url' not in attrs:
            continue
        result.append({
            'name': name,
            'path': attrs['path'],
            'url': attrs['url'],
            'update': attrs.get('update', 'checkout'),
            'branch': attrs.get('branch', ''),
        })
    return result


def submodule_target_commit(parent_worktree: str, sub_path: str) -> Optional[str]:
    """从父 worktree 取 submodule 在 HEAD 中的目标 commit。"""
    res = run_git(['-C', parent_worktree, 'ls-tree', 'HEAD', sub_path],
                  capture=True, check=False)
    if res.returncode != 0:
        return None
    line = res.stdout.strip()
    if not line:
        return None
    # "<mode> <type> <hash>\t<path>"
    head, _tab, _rest = line.partition('\t')
    parts = head.split()
    if len(parts) < 3 or parts[0] != '160000':
        return None
    return parts[2]


def get_remote_url(worktree: str) -> str:
    res = run_git(['-C', worktree, 'config', '--get', 'remote.origin.url'],
                  capture=True, check=False)
    return _sanitize_url(res.stdout.strip())


def get_remote_url_by_name(gitdir: str, remote_name: str) -> str:
    """返回指定 remote 名的 fetch URL（已 sanitize）；不存在或为本地路径时返回空串。"""
    r = run_git(['--git-dir', gitdir, 'remote', 'get-url', remote_name], capture=True, check=False)
    if r.returncode != 0:
        return ''
    return _sanitize_url(r.stdout.strip())


def native_submodule_update_pool_first(parent_worktree: str, sub_path: str,
                                       recursive: bool, depth: Optional[str], buf: List[str]):
    """update=rebase/merge 等场景：池已预热后，调原生 git 执行原始策略。"""
    args = ['-C', parent_worktree, 'submodule', 'update', '--init']
    if recursive:
        args.append('--recursive')
    if depth:
        args.extend(['--depth', str(depth)])
    args.append('--')
    args.append(sub_path)
    res = subprocess.run([SYSTEM_GIT] + args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if res.stdout:
        buf.append(res.stdout.rstrip('\n'))
    if res.stderr:
        buf.append(res.stderr.rstrip('\n'))


def process_one_submodule(parent_worktree: str, parent_remote: str, toplevel_gitdir: Optional[str],
                          sub: Dict[str, str], recursive: bool, jobs: int,
                          depth: Optional[str], filter_: Optional[str], buf: List[str]):
    name = sub['name']
    sub_path = sub['path']
    raw_url = sub['url']
    update = sub.get('update') or 'checkout'

    # update=none：纯 skip
    if update == 'none':
        return

    # 相对 URL 解析为绝对 URL
    url = resolve_relative_url(parent_remote, raw_url)
    url = _sanitize_url(url)

    sub_worktree = os.path.join(parent_worktree, sub_path)

    # 本地路径 / file:// → 透传原生 git，不走池
    if url.startswith('/') or url.startswith('file://'):
        buf.append(f"[Wrapper] 本地 URL 透传原生 git: submodule {sub_path} ({url})")
        native_submodule_update_pool_first(parent_worktree, sub_path, False, depth, buf)
        if recursive and os.path.isdir(sub_worktree):
            nested = parse_gitmodules(sub_worktree)
            if nested:
                nested_remote = get_remote_url(sub_worktree) or url
                nested_toplevel = toplevel_gitdir
                process_submodule_level(sub_worktree, nested_remote, nested_toplevel,
                                        nested, recursive, jobs, depth, filter_)
        return

    # 取目标 commit
    commit = submodule_target_commit(parent_worktree, sub_path)
    if not commit:
        buf.append(f"[Wrapper] warning: cannot resolve target commit for submodule {sub_path}, skip")
        return

    # 计算 gitdir：toplevel/.git/modules/<name>
    if toplevel_gitdir:
        gitdir = os.path.join(toplevel_gitdir, 'modules', name)
    else:
        parent_gd = find_gitdir(parent_worktree)
        if not parent_gd:
            buf.append(f"[Wrapper] warning: cannot find parent gitdir for {sub_path}, skip")
            return
        gitdir = os.path.join(parent_gd, 'modules', name)

    pool_path = pool_repo_path(url)

    # 判断是否已初始化
    initialized = (os.path.isdir(sub_worktree) and os.path.isdir(gitdir) and
                   (os.path.isfile(os.path.join(sub_worktree, '.git')) or
                    os.path.isdir(os.path.join(sub_worktree, '.git'))))

    if not initialized:
        # 首次：clone 进池 → 通过 --reference 引用裸仓
        ensure_pool_repo(url, pool_path)
        os.makedirs(os.path.dirname(gitdir), exist_ok=True)
        os.makedirs(sub_worktree, exist_ok=True)
        clone_args = ['clone', '--no-checkout',
                      '--separate-git-dir', gitdir,
                      '--reference', str(pool_path)]
        if depth:
            clone_args.extend(['--depth', str(depth)])
        if filter_:
            clone_args.extend([f'--filter={filter_}'])
        clone_args.extend([url, sub_worktree])
        res = run_git(clone_args, capture=True, check=False)
        if res.returncode != 0:
            if res.stderr:
                buf.append(res.stderr.rstrip('\n'))
            buf.append(f"[Wrapper] error: clone failed for submodule {sub_path}")
            return
        add_alternate(gitdir, pool_path)
        register_shell(gitdir)
    else:
        # 已存在：定位 gitdir + 池预热 + alternates + fetch
        existing_gd = find_gitdir(sub_worktree)
        if existing_gd:
            gitdir = existing_gd
        ensure_pool_repo(url, pool_path)
        add_alternate(gitdir, pool_path)
        register_shell(gitdir)
        run_git(['--git-dir', gitdir, 'fetch', '--all', '--prune'],
                capture=True, check=False)

    # checkout / rebase / merge
    if update in ('rebase', 'merge'):
        # pool 已预热，fallback 到原生 git submodule update 处理 rebase/merge 策略
        native_submodule_update_pool_first(parent_worktree, sub_path, False, depth, buf)
    else:
        res = run_git(['--git-dir', gitdir, '--work-tree', sub_worktree,
                       'checkout', '--detach', commit, '-q'],
                      capture=True, check=False)
        if res.returncode != 0:
            # 若本地缺该 commit，再 fetch 一次
            run_git(['--git-dir', gitdir, 'fetch', 'origin', commit],
                    capture=True, check=False)
            res = run_git(['--git-dir', gitdir, '--work-tree', sub_worktree,
                           'checkout', '--detach', commit, '-q'],
                          capture=True, check=False)
        if res.returncode == 0:
            buf.append(f"Submodule path '{sub_path}': checked out '{commit}'")
        else:
            if res.stderr:
                buf.append(res.stderr.rstrip('\n'))
            buf.append(f"[Wrapper] error: checkout {commit} failed for submodule {sub_path}")
            return

    # 递归处理嵌套 submodule
    if recursive and os.path.isdir(sub_worktree):
        nested = parse_gitmodules(sub_worktree)
        if nested:
            nested_remote = url  # 嵌套相对 URL 相对于本 submodule 的 remote
            process_submodule_level(sub_worktree, nested_remote, toplevel_gitdir,
                                    nested, recursive, jobs, depth, filter_)


def process_submodule_level(parent_worktree: str, parent_remote: str,
                            toplevel_gitdir: Optional[str],
                            subs: List[Dict[str, str]], recursive: bool,
                            jobs: int, depth: Optional[str], filter_: Optional[str]):
    """同层 submodule 并发处理；每个 submodule 缓冲输出，避免交错。"""
    bufs: List[List[str]] = [[] for _ in subs]

    def worker(i: int):
        try:
            process_one_submodule(parent_worktree, parent_remote, toplevel_gitdir,
                                  subs[i], recursive, jobs, depth, filter_, bufs[i])
        except SystemExit as e:
            bufs[i].append(f"[Wrapper] error processing submodule {subs[i].get('name')}: exit {e.code}")
        except Exception as e:
            bufs[i].append(f"[Wrapper] error processing submodule {subs[i].get('name')}: {e}")

    if jobs and jobs > 1 and len(subs) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            list(ex.map(worker, range(len(subs))))
    else:
        for i in range(len(subs)):
            worker(i)

    for buf in bufs:
        for line in buf:
            if line:
                print(line, file=sys.stderr)


def parse_submodule_update_options(subcmd_args: List[str]) -> Tuple[bool, int, Optional[str], Optional[str], List[str]]:
    """解析 git submodule update 的 --recursive / --jobs / --depth / --filter，以及末尾 path 过滤。"""
    recursive = False
    jobs = 1
    depth: Optional[str] = None
    filter_: Optional[str] = None
    paths: List[str] = []
    i = 0
    past_separator = False
    while i < len(subcmd_args):
        a = subcmd_args[i]
        if a == '--':
            past_separator = True
            i += 1
            paths.extend(subcmd_args[i:])
            break
        if past_separator or (not a.startswith('-')):
            # skip known subcommand "update" itself
            if a not in ('update',):
                paths.append(a)
            i += 1
            continue
        if a == '--recursive':
            recursive = True
        elif a in ('-j', '--jobs') and i + 1 < len(subcmd_args):
            try:
                jobs = int(subcmd_args[i + 1])
            except ValueError:
                pass
            i += 1
        elif a.startswith('--jobs='):
            try:
                jobs = int(a.split('=', 1)[1])
            except ValueError:
                pass
        elif a == '--depth' and i + 1 < len(subcmd_args):
            depth = subcmd_args[i + 1]
            i += 1
        elif a.startswith('--depth='):
            depth = a.split('=', 1)[1]
        elif a == '--filter' and i + 1 < len(subcmd_args):
            filter_ = subcmd_args[i + 1]
            i += 1
        elif a.startswith('--filter='):
            filter_ = a.split('=', 1)[1]
        i += 1
    if jobs < 1:
        jobs = 1
    return recursive, jobs, depth, filter_, paths


def do_submodule_update_init(global_opts: List[str], subcmd_args: List[str]):
    """自驱实现 submodule update --init [--recursive --jobs --depth --filter]。

    核心不变式：
    1) 任何网络下载必先进池（ensure_pool_repo）
    2) worktree 仅通过 --reference 引用池对象，不加 --dissociate
    3) 菱形/共享依赖复用同一池裸仓（URL 相同 → 路径相同）
    4) update=none 纯 skip
    5) 相对 URL 先解析为绝对 URL 再走池流程
    """
    recursive, jobs, depth, filter_, paths = parse_submodule_update_options(subcmd_args)

    root = effective_cwd(global_opts)
    toplevel_gitdir = find_gitdir(root)
    parent_remote = get_remote_url(root)

    subs = parse_gitmodules(root)
    if paths:
        subs = [s for s in subs if s['path'] in paths or s['name'] in paths]
    if not subs:
        return

    process_submodule_level(root, parent_remote, toplevel_gitdir,
                            subs, recursive, jobs, depth, filter_)


def do_submodule(global_opts: List[str], subcmd_args: List[str]):
    """仅拦截 update --init（且无 --remote）；其他 submodule 子命令一律透传原生 git。"""
    if (subcmd_args and subcmd_args[0] == 'update'
            and '--init' in subcmd_args
            and '--remote' not in subcmd_args):
        do_submodule_update_init(global_opts, subcmd_args)
        return
    return exec_git(global_opts + ['submodule'] + subcmd_args)


# ---------- gc / 池维护 ----------
def read_alternates(gitdir: str) -> List[str]:
    alternates = Path(gitdir) / 'objects' / 'info' / 'alternates'
    if not alternates.exists():
        return []
    result = []
    with open(alternates, 'r') as f:
        for line in f:
            item = line.strip()
            if not item or item.startswith('#'):
                continue
            if not os.path.isabs(item):
                item = str((alternates.parent / item).resolve())
            result.append(os.path.abspath(item))
    return result


def pool_repos() -> List[Path]:
    root = Path(GIT_POOL)
    if not root.exists():
        return []
    repos = []
    # 池路径形如 <host>/<group>/<repo>，无 .git 后缀；递归遍历查找有效裸仓
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # 跳过隐藏目录（.lock、.registered_shells 等）
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        # 裸仓特征：包含 HEAD 与 objects/
        if 'HEAD' in filenames and 'objects' in dirnames:
            result = run_git(['--git-dir', dirpath, 'rev-parse', '--git-dir'], capture=True, check=False)
            if result.returncode == 0:
                repos.append(Path(dirpath))
                # 裸仓内部不再下钻
                dirnames[:] = []
    return repos


def registered_shells() -> List[str]:
    clean_registry()
    if not REGISTRY_FILE.exists():
        return []
    with open(REGISTRY_FILE, 'r') as f:
        return [line.strip() for line in f if line.strip() and os.path.isdir(line.strip())]


def collect_pool_dependencies(shells: List[str], repos: List[Path]) -> dict:
    deps = {}
    pool_by_objects = {os.path.abspath(str(repo / 'objects')): repo for repo in repos}
    for shell in shells:
        for alternate in read_alternates(shell):
            repo = pool_by_objects.get(alternate)
            if repo is None:
                continue
            deps.setdefault(str(repo), []).append(shell)
    return deps


def rev_list_objects(gitdir: str) -> Optional[set]:
    result = run_git(['--git-dir', gitdir, 'rev-list', '--all', '--objects'], capture=True, check=False)
    if result.returncode != 0:
        print(f'[Wrapper] warning: rev-list failed for {gitdir}', file=sys.stderr)
        return None
    objects = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        objects.add(line.split()[0])
    return objects


def collect_shell_live_objects(shells: List[str]) -> Optional[set]:
    live = set()
    failed = False
    for shell in shells:
        objects = rev_list_objects(shell)
        if objects is None:
            failed = True
            continue
        live.update(objects)
    if failed and not live:
        return None
    return live


def gc_pool_repos():
    shells = registered_shells()
    repos = pool_repos()
    deps = collect_pool_dependencies(shells, repos)
    shell_live = collect_shell_live_objects(shells)
    for repo in repos:
        dependents = deps.get(str(repo), [])
        if not dependents:
            print(f'[Wrapper] GC 池裸仓: {repo} (无已注册 worktree 依赖，保守 prune=never)', file=sys.stderr)
            run_git(['--git-dir', str(repo), 'gc', '--prune=never'], check=False)
            continue
        pool_objects = rev_list_objects(str(repo))
        if shell_live is None or pool_objects is None:
            print(f'[Wrapper] GC 池裸仓: {repo} (依赖分析失败，prune=never)', file=sys.stderr)
            run_git(['--git-dir', str(repo), 'gc', '--prune=never'], check=True)
            continue
        redundant = pool_objects - shell_live
        print(f'[Wrapper] GC 池裸仓: {repo} (被 {len(dependents)} 个 worktree 依赖，'
              f'live={len(pool_objects) - len(redundant)} redundant={len(redundant)})', file=sys.stderr)
        try:
            run_git(['--git-dir', str(repo), 'prune', '--expire=now'], check=True)
            run_git(['--git-dir', str(repo), 'repack', '-Ad'], check=True)
        except SystemExit:
            print(f'[Wrapper] warning: pool gc failed for {repo}, fallback prune=never', file=sys.stderr)
            run_git(['--git-dir', str(repo), 'gc', '--prune=never'], check=True)


def do_gc(global_opts: List[str], subcmd: str, subcmd_args: List[str]):
    lock_fd = acquire_lock()
    try:
        gc_pool_repos()
    finally:
        release_lock(lock_fd)
    return exec_git(global_opts + [subcmd] + subcmd_args)


# ---------- migrate ----------
_MIGRATE_TMP_REF_RE = re.compile(r'^refs/migrate-([0-9a-f]{32})/')


def _cleanup_migrate_tmp_refs(pool_path: Path):
    """清理池裸仓里所有 refs/migrate-<uuid32>/（上次异常中断的残留）。

    只删除 namespace 部分严格为 32 位小写十六进制（uuid4().hex）的 ref，
    避免误删用户自建的其他 refs/migrate-* ref。
    """
    res = run_git(['--git-dir', str(pool_path), 'for-each-ref',
                   '--format=%(refname)', 'refs/migrate-'],
                  capture=True, check=False)
    for refname in res.stdout.splitlines():
        refname = refname.strip()
        if refname and _MIGRATE_TMP_REF_RE.match(refname):
            run_git(['--git-dir', str(pool_path), 'update-ref', '-d', refname],
                    capture=True, check=False)


def _fetch_local_to_pool(gitdir: str, pool_path: Path):
    """将本地仓库所有对象 fetch 进池裸仓，完成后删除临时 ref。

    使用 UUID 命名临时 ref 命名空间（refs/migrate-<uuid>/...），
    避免与任何已有 ref 冲突。
    """
    uid = uuid.uuid4().hex
    tmp_heads = f'refs/migrate-{uid}/heads/*'

    # fetch 本地 heads 进池
    run_git(['--git-dir', str(pool_path), 'fetch', gitdir,
             f'+refs/heads/*:{tmp_heads}'],
            capture=True, check=False)

    # 立刻删除临时 ref（对象已在 pack 中，ref 无需保留）
    res = run_git(['--git-dir', str(pool_path), 'for-each-ref',
                   '--format=%(refname)', f'refs/migrate-{uid}/'],
                  capture=True, check=False)
    for refname in res.stdout.splitlines():
        refname = refname.strip()
        if refname:
            run_git(['--git-dir', str(pool_path), 'update-ref', '-d', refname],
                    capture=True, check=False)


def _migrate_gitdir(gitdir: str, remote_url: str, label: str) -> bool:
    """对一个已知 gitdir + remote_url 执行迁移核心逻辑（供父仓和 submodule 共用）。

    label 用于日志前缀，例如 repo_path 或 "submodule <name>"。
    返回 True 表示成功（含幂等跳过），False 表示失败。
    """
    pool_path = pool_repo_path(remote_url)
    print(f'[Wrapper] migrate: {label}', file=sys.stderr)
    print(f'[Wrapper]   remote : {remote_url}', file=sys.stderr)
    print(f'[Wrapper]   pool   : {pool_path}', file=sys.stderr)

    # 清理上次异常残留的临时 ref
    _cleanup_migrate_tmp_refs(pool_path)

    # 检查是否已有 alternates 指向该池路径（幂等）
    existing = read_alternates(gitdir)
    target_objects = os.path.abspath(str(pool_path / 'objects'))
    already_linked = target_objects in existing
    if already_linked:
        print(f'[Wrapper]   已在池中，仅更新池裸仓', file=sys.stderr)

    ensure_pool_repo(remote_url, pool_path)

    # 将本地所有对象（含其他 remote fetch 来的）push 进池，最大化收缩本地
    print(f'[Wrapper]   fetch 本地对象进池 ...', file=sys.stderr)
    _fetch_local_to_pool(gitdir, pool_path)

    if not already_linked:
        add_alternate(gitdir, pool_path)
    register_shell(gitdir)

    print(f'[Wrapper]   repack --local ...', file=sys.stderr)
    res = run_git(['--git-dir', gitdir, 'repack', '-a', '-d', '--local'], capture=True, check=False)
    if res.returncode != 0:
        print(f'[Wrapper]   repack 失败: {res.stderr.strip()}', file=sys.stderr)
        return False

    print(f'[Wrapper]   ✓ 迁移完成', file=sys.stderr)
    return True


def migrate_one(repo_path: str, recursive: bool = False, remote_name: str = 'origin') -> bool:
    """将一个已有仓库迁移进对象池。

    流程：
    1. 按 remote_name 取 URL（默认 origin）；不存在则报错退出
    2. 以该 URL 决定池路径，ensure_pool_repo
    3. 将本地所有对象（含其他 remote 带来的）fetch 进池
    4. 写 alternates + repack --local
    5. 注册到 registry
    若 recursive=True，递归处理所有 submodule（gitdir 位于 .git/modules/<name>）。

    返回 True 表示成功，False 表示跳过或失败。
    """
    repo_path = os.path.abspath(repo_path)
    gitdir = find_gitdir(repo_path)
    if not gitdir:
        print(f'[Wrapper] migrate: {repo_path} 不是 git 仓库，跳过', file=sys.stderr)
        return False

    primary_url = get_remote_url_by_name(gitdir, remote_name)
    if not primary_url:
        print(f'[Wrapper] migrate: remote "{remote_name}" 不存在或 URL 为空，请通过 --remote 指定正确的 remote 名', file=sys.stderr)
        return False
    if invalid_pool_url(primary_url):
        print(f'[Wrapper] migrate: remote "{remote_name}" URL 为本地路径，跳过', file=sys.stderr)
        return False

    ok = _migrate_gitdir(gitdir, primary_url, repo_path)

    if recursive:
        # worktree_root：find_gitdir 可能返回 .git（普通仓库），worktree 就是其父目录
        worktree = str(Path(gitdir).parent) if Path(gitdir).name == '.git' else repo_path

        def _migrate_recursive(cur_worktree: str, parent_url: str, depth_label: str) -> bool:
            sub_ok_all = True
            cur_subs = parse_gitmodules(cur_worktree)
            for s in cur_subs:
                s_name = s['name']
                s_url = resolve_relative_url(parent_url, s['url'])
                if invalid_pool_url(s_url):
                    print(f'[Wrapper] migrate: submodule {s_name} URL 为本地路径，跳过', file=sys.stderr)
                    continue
                s_worktree = os.path.join(cur_worktree, s['path'])
                if not os.path.isdir(s_worktree):
                    print(f'[Wrapper] migrate: submodule {s_name} worktree 不存在（未 init），跳过', file=sys.stderr)
                    continue
                s_gitdir = find_gitdir(s_worktree)
                if not s_gitdir:
                    print(f'[Wrapper] migrate: submodule {s_name} gitdir 不存在（未 init），跳过', file=sys.stderr)
                    continue
                label = f'submodule {s_name}' if not depth_label else f'{depth_label} / submodule {s_name}'
                if not _migrate_gitdir(s_gitdir, s_url, label):
                    sub_ok_all = False
                # 嵌套继续
                if not _migrate_recursive(s_worktree, s_url, label):
                    sub_ok_all = False
            return sub_ok_all

        if not _migrate_recursive(worktree, primary_url, ''):
            ok = False

    return ok


def do_migrate(args: List[str]):
    """git migrate [--recursive|-r] [--remote <name>]

    将当前仓库迁移进对象池（在仓库目录内执行，无需传路径）。
    --recursive / -r：同时迁移所有已 init 的 submodule。
    --remote <name>：指定用哪个 remote 的 URL 作为池的来源（默认 origin）。
                     未指定且无 origin 时报错退出。
    """
    recursive = False
    remote_name = 'origin'
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('--recursive', '-r'):
            recursive = True
        elif a == '--remote':
            i += 1
            if i >= len(args):
                print('[Wrapper] migrate: --remote 需要一个参数', file=sys.stderr)
                sys.exit(1)
            if args[i].startswith('-'):
                print(f'[Wrapper] migrate: --remote 后接的 {args[i]!r} 看起来是选项而非 remote 名', file=sys.stderr)
                sys.exit(1)
            remote_name = args[i]
        else:
            print(f'[Wrapper] migrate: 未知参数 {a!r}', file=sys.stderr)
            sys.exit(1)
        i += 1

    migrate_one(os.getcwd(), recursive=recursive, remote_name=remote_name)


# ---------- main ----------
def main():
    init_pool()

    if len(sys.argv) < 2:
        os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])

    global_opts, subcmd, subcmd_args = parse_global_options(sys.argv)
    if not subcmd:
        os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])

    if subcmd == 'clone':
        do_clone(global_opts, subcmd_args)
        return
    if subcmd == 'submodule':
        do_submodule(global_opts, subcmd_args)
        return
    if subcmd == 'fetch':
        do_fetch(global_opts, subcmd_args)
        return
    if subcmd == 'gc':
        do_gc(global_opts, subcmd, subcmd_args)
        return
    if subcmd == 'prune':
        exec_git(global_opts + ['prune'] + subcmd_args)
        return
    if subcmd == 'migrate':
        do_migrate(subcmd_args)
        return

    os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])


if __name__ == '__main__':
    if SYSTEM_GIT is None:
        print('错误：找不到系统 Git 命令', file=sys.stderr)
        sys.exit(1)
    main()
