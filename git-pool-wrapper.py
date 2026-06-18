#!/usr/bin/env python3
"""
Git Wrapper - 全局对象池共享方案（自驱 submodule 版）
拦截 clone / submodule update --init / fetch / gc / prune，实现跨仓库对象共享
"""

import os
import sys
import subprocess
import tempfile
if sys.platform != 'win32':
    import fcntl
else:
    import msvcrt
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple, Dict, NamedTuple
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


class GitCommandError(Exception):
    """git 子进程（经 run_git(check=True) 调用）失败，或 wrapper 层参数校验失败时抛出。

    携带退出码与（capture 模式下捕获到的）stderr，供顶层 main() 统一打印并退出。
    library 层不再直接 sys.exit，退出决策集中在 main()；exec 系函数
    （_exec_passthrough）例外——它们的语义就是终止当前进程。
    """

    def __init__(self, message: str, returncode: int = 1, stderr: str = ''):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


# ---------- 配置 ----------
GIT_POOL = os.environ.get('GIT_POOL', os.path.expanduser('~/.git-pool'))
POOL_DIR: Path = Path(GIT_POOL)
# 全局唯一对象池：所有 clone / fetch / submodule / migrate 共享同一裸仓
POOL_PATH: Path = POOL_DIR / 'pool.git'
POOL_LOCK_PATH = POOL_DIR / '.lock'
REGISTRY_FILE = POOL_DIR / '.registered_shells'
GC_LAST_RUN_FILE = POOL_DIR / '.gc_last_run'
GC_MIN_INTERVAL = 3600  # 全池 GC 最小间隔（秒）：不足 1 小时则跳过
_REMOTE_NAME_MAX_LEN = 100
_SHALLOW_BYPASS_FLAGS = {'--unshallow', '--update-shallow'}
POOL_GC_WHITELIST = {'--quiet', '--no-quiet', '--auto',
                     '--aggressive', '--prune=now', '--prune=never'}


def _resolve_system_git() -> str:
    """解析系统原生 git 路径，避免解析到 wrapper 自身。

    优先级：SYSTEM_GIT 环境变量 > PATH 中第一个非 wrapper 的 git > /usr/bin/git
    """
    from_env = os.environ.get('SYSTEM_GIT')
    if from_env:
        return from_env
    # shutil.which 可能找到 wrapper 自身（如 PATH 中有同名 shim），需跳过
    wrapper_path = os.path.realpath(__file__)
    candidate = shutil.which('git')
    if candidate:
        if os.path.realpath(candidate) != wrapper_path:
            return candidate
    return '/usr/bin/git'


SYSTEM_GIT = _resolve_system_git()

_registry_lock = threading.Lock()

# 可重入的池锁：threading.RLock 保证同进程多线程互斥（同线程可嵌套），
# fcntl.flock 仅在最外层 acquire 时获取，保证跨进程互斥；最外层 release 时才释放。
_pool_lock_rlock = threading.RLock()
_pool_lock_state = {'fd': None, 'count': 0}

# 本进程内已成功 fetch 过的 URL 集合：同 URL 在同一进程内只 fetch 一次（去重加速）。
_fetched_urls: set = set()


# ---------- 工具函数 ----------
def _ensure_pool_dir():
    """确保池目录骨架存在：GIT_POOL 目录、registry 文件、锁文件。"""
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.touch(exist_ok=True)
    POOL_LOCK_PATH.touch(exist_ok=True)


def _sanitize_url(url: str) -> str:
    """去除 URL 中可能混入的零宽字符及不可见控制字符（Unicode category Cf/Cc/Cs）。"""
    return ''.join(c for c in url if unicodedata.category(c)
                   not in ('Cf', 'Cc', 'Cs'))


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
        after_colon = url[colon_idx + 1:]
        # 若 ':' 后紧接纯数字（端口号，如 host:443/path），丢弃端口而非转路径段
        port_end = 0
        while port_end < len(after_colon) and after_colon[port_end].isdigit():
            port_end += 1
        if port_end > 0 and (port_end == len(after_colon)
                             or after_colon[port_end] == '/'):
            # host:443 -> host  /  host:443/path -> host/path
            url = url[:colon_idx] + after_colon[port_end:]
        else:
            # 标准 SCP 语法 git@host:org/repo -> host/org/repo
            url = url[:colon_idx] + '/' + after_colon
    # 4) 去掉末尾 / 再去掉末尾 .git
    url = url.rstrip('/')
    url = re.sub(r'\.git$', '', url)
    # 5) 折叠多余连续斜杠
    url = re.sub(r'/+', '/', url)
    return url.lower().strip('/')


def _url_to_remote_name(url: str) -> str:
    """将 URL 转成池内合法且尽量唯一的 remote 名。

    复用 normalize_url 得到 host/path 形式（已去协议/用户/端口/.git），再把所有非
    字母数字字符折叠为 '-'，去首尾 '-'，并截断到合理长度，保证可作为 git remote 名。
    例：https://host/storage/foo.git -> host-storage-foo
    """
    key = normalize_url(_sanitize_url(url))
    name = re.sub(r'[^a-zA-Z0-9]+', '-', key).strip('-')
    if not name:
        name = 'pool'
    return name[:_REMOTE_NAME_MAX_LEN]


@contextmanager
def pool_lock():
    """全局对象池可重入锁上下文管理器：用 `with pool_lock():` 包裹临界区。

    threading.RLock 保证同进程多线程互斥（同线程可嵌套），文件锁仅在最外层
    进入时获取、最外层退出时释放，保证跨进程互斥。

    open()/lock() 失败时回滚已自增的计数并释放 rlock（顺带关闭已打开的 fd），
    避免把 fd=None 的半成品状态留给退出逻辑而崩溃。
    """
    _pool_lock_rlock.acquire()
    _pool_lock_state['count'] += 1
    if _pool_lock_state['count'] == 1:
        fd = None
        try:
            if sys.platform == 'win32':
                # Windows: 用 os.open 获取原始文件描述符，再用 msvcrt.locking 锁定
                raw_fd = os.open(str(POOL_LOCK_PATH), os.O_CREAT | os.O_RDWR)
                # msvcrt.locking 需要文件非空才能锁定 byte range 0..1
                if os.lseek(raw_fd, 0, os.SEEK_END) == 0:
                    os.write(raw_fd, b'\x00')
                os.lseek(raw_fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(raw_fd, msvcrt.LK_LOCK, 1)
                except OSError:
                    os.close(raw_fd)
                    raise
                fd = raw_fd
            else:
                fd = open(POOL_LOCK_PATH, 'w')
                fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            if fd is not None:
                if sys.platform == 'win32':
                    os.close(fd)
                else:
                    fd.close()
            _pool_lock_state['count'] -= 1
            _pool_lock_rlock.release()
            raise
        _pool_lock_state['fd'] = fd
    try:
        yield
    finally:
        _pool_lock_state['count'] -= 1
        if _pool_lock_state['count'] == 0:
            fd = _pool_lock_state['fd']
            _pool_lock_state['fd'] = None
            try:
                if sys.platform == 'win32':
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                if sys.platform == 'win32':
                    os.close(fd)
                else:
                    fd.close()
        _pool_lock_rlock.release()


def run_git(args: List[str], capture=False, check=True, env=None):
    """运行系统 git 子命令。

    错误处理 invariant：
    - 默认 check=True：git 退出码非 0 时抛 GitCommandError（携带 returncode 与
      stderr）。library 层不再 sys.exit，退出统一由 main() 决定；exec 系函数
      （_exec_passthrough）例外——它们的语义就是终止当前进程。
    - 仅在「确实需要忽略错误、由调用方自行判断 returncode/stdout」的探测型调用上
      显式传 check=False，并在调用点附注释说明原因（例如 config --get / rev-parse /
      cat-file -e / for-each-ref 等查询失败属正常分支）。
    - capture=True 时捕获 stdout/stderr，供调用方读取；capture=False 时 stderr
      继承父进程，git 进度信息直接可见，stdout 也继承父进程。
    """
    if env is None:
        env = os.environ.copy()
    # 屏蔽 nvm 相关变量：git repack 内部会 fork /bin/sh，nvm.sh 在非交互式环境下
    # 可能向 stderr 打印噪声（如 "type: manpath: not found"），导致 fatal: bad revision。
    for _nvm_key in ('NVM_DIR', 'NVM_BIN', 'NVM_INC', 'NVM_RC_VERSION', 'NVM_CD_FLAGS'):
        env.pop(_nvm_key, None)
    cmd = [SYSTEM_GIT] + args
    if capture:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL)
    else:
        # stderr 继承父进程，让 git clone/fetch 的进度信息直接可见；stdout 也继承父进程。
        result = subprocess.run(
            cmd,
            env=env,
            stderr=None,
            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        stderr = result.stderr if result.stderr else ''
        raise GitCommandError(
            f'git {" ".join(args)} 失败（exit {result.returncode}）',
            returncode=result.returncode, stderr=stderr)
    return result


def _exec_passthrough(argv: List[str]):
    """将当前进程替换为原生 git（Unix execvp），或在 Windows 下以子进程运行后退出。"""
    if sys.platform == 'win32':
        result = subprocess.run([SYSTEM_GIT] + argv)
        sys.exit(result.returncode)
    os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + argv)


def register_shell(gitdir: str):
    gitdir = os.path.abspath(gitdir)
    if not os.path.isdir(gitdir):
        return
    with pool_lock():
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
    with pool_lock():
        with _registry_lock:
            with open(REGISTRY_FILE, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
            # 保留条件：绝对路径，且（确实是目录，或路径项存在但当前无法解析为存在的目标=
            # 疑似临时不可达，保留）。普通存在的非目录文件、不存在且无 lexists 的条目，剔除。
            valid = [
                l for l in lines
                if os.path.isabs(l) and (
                    os.path.isdir(l) or (
                        os.path.lexists(l) and not os.path.exists(l))
                )
            ]
            with open(REGISTRY_FILE, 'w') as f:
                f.write('\n'.join(valid) + ('\n' if valid else ''))


def resolve_gitdir(dot_git_path: str) -> str:
    """把一个 `.git` 路径解析为真实的 gitdir 绝对路径。

    - `.git` 为目录：返回其 resolve() 后的绝对路径；
    - `.git` 为文件（worktree / submodule 的 `gitdir: <path>` 指针）：读取指针，
      相对路径相对 `.git` 所在目录解析为绝对路径后返回；
    - 其他情况（不存在、非 gitdir 指针文件）：返回空串。

    供 find_gitdir 与 do_clone 共用，避免两处各写一遍 `.git` 文件解析逻辑。
    """
    p = Path(dot_git_path)
    if p.is_dir():
        return str(p.resolve())
    if p.is_file():
        with open(p, 'r') as f:
            content = f.read().strip()
        if content.startswith('gitdir:'):
            gitdir = content[len('gitdir:'):].strip()
            if not os.path.isabs(gitdir):
                gitdir = str((p.parent / gitdir).resolve())
            return gitdir
    return ''


def find_gitdir(path: str) -> Optional[str]:
    path = Path(path).resolve()
    while path != path.parent:
        gitpath = path / '.git'
        if gitpath.exists():
            resolved = resolve_gitdir(str(gitpath))
            if resolved:
                return resolved
        path = path.parent
    return None


def parse_git_options(args: List[str], options_with_value: set, flags: Optional[set] = None,
                      stop_at_positional: bool = False, strict: bool = False) -> Tuple[List[str], List[str]]:
    """通用 git 风格 argv 分词器：被 clone / global / submodule / migrate 解析复用。

    把 args 切分为 (options, rest)：
    - options：识别到的选项 token，`--opt value` 空格形式会把值一并保留为相邻元素。
    - rest：未被当作选项消费的剩余部分。

    参数：
    - options_with_value：取值选项名集合（空格形式吞掉下一个 token；`--opt=value`
      形式自带值，不再吞 token）。
    - flags：已知无值开关集合（仅在 strict=True 时用于「识别/未识别」判定）。
    - stop_at_positional：遇到第一个非选项（positional）即停止，把它及其后所有 token
      作为 rest 返回（global 解析借此定位 subcommand 边界）。
    - strict：遇到第一个「未识别」的 `-` token（既不是已知 flag，也不是已知取值选项）
      即停止，把它及其后的 token 作为 rest 返回（global 解析借此对未知全局选项
      fallthrough 到原生 git）。

    非 strict / 非 stop 模式：`--` 之后的所有 token 均作为 positionals（`--` 本身丢弃），
    与 clone / submodule 原有行为一致；strict / stop 模式把 `--` 连同其后内容作为 rest 返回。
    """
    flags = flags or set()
    options: List[str] = []
    positionals: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        if arg == '--':
            if stop_at_positional or strict:
                return options, args[i:]
            positionals.extend(args[i + 1:])
            return options, positionals
        if not arg.startswith('-'):
            if stop_at_positional:
                return options, args[i:]
            positionals.append(arg)
            i += 1
            continue
        opt = arg.split('=', 1)[0]
        if strict and not (arg in flags or opt in options_with_value):
            return options, args[i:]
        options.append(arg)
        if '=' not in arg and opt in options_with_value and i + 1 < n:
            i += 1
            options.append(args[i])
        i += 1
    return options, positionals


def parse_git_option_values(
        args: List[str], options_with_value: set) -> Tuple[Dict[str, object], List[str]]:
    """plain 模式单趟解析，直接产出「选项 -> 值」字典，调用方一步取值无需二次循环。

    返回 (values, positionals)：
    - values：取值选项（空格形式 `--opt value` 或等号形式 `--opt=value`）映射到其
      字符串值；无值开关映射到 True。同名选项重复出现时后者覆盖前者，与原 parse_git_options
      + 调用方二次循环「后值生效」的语义一致。
    - positionals：非选项参数；`--` 之后内容并入 positionals（`--` 本身丢弃）。

    仅供「需要按名取值」的 value-consumer 复用（submodule update / migrate）。clone /
    global 需要把原始 token（含重复 -c）原样转发给原生 git，故仍走 parse_git_options。
    """
    values: Dict[str, object] = {}
    positionals: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        if arg == '--':
            positionals.extend(args[i + 1:])
            break
        if not arg.startswith('-'):
            positionals.append(arg)
            i += 1
            continue
        if '=' in arg:
            name, val = arg.split('=', 1)
            values[name] = val
        elif arg in options_with_value and i + 1 < n:
            i += 1
            values[arg] = args[i]
        else:
            values[arg] = True
        i += 1
    return values, positionals


def parse_clone_args(args: List[str]) -> Tuple[List[str], List[str]]:
    options_with_value = {
        '-b', '--branch', '--depth', '--origin', '-o', '--template', '--reference',
        '--reference-if-able', '--separate-git-dir', '-c', '--config', '--server-option',
        '--jobs', '-j', '--filter', '--shallow-since', '--shallow-exclude',
        '--upload-pack', '-u'
    }
    return parse_git_options(args, options_with_value)


def has_option(args: List[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + '=') for arg in args)


class ParsedCommand(NamedTuple):
    """parse_global_options 的解析结果：全局选项 + subcommand + 其参数。"""
    global_opts: List[str]
    subcmd: str
    subcmd_args: List[str]


def parse_global_options(argv: List[str]) -> Optional[ParsedCommand]:
    """解析 git 全局选项并定位 subcommand。

    返回 ParsedCommand(global_opts, subcmd, subcmd_args)；返回 None 表示应
    fallthrough 到原生 git（无 subcommand / `git -- ...` / 未识别全局选项）。
    解析函数不再自行 execvp，进程替换的副作用上移到 main()，使「解析」保持纯函数
    语义、控制流可读。
    """
    args = argv[1:]
    options_with_value = {
        '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--exec-path', '--list-cmds'
    }
    flag_options = {
        '-v', '--version', '--help', '--html-path', '--man-path', '--info-path', '-p', '--paginate',
        '-P', '--no-pager', '--no-replace-objects', '--bare', '--no-optional-locks', '--no-advice'
    }
    global_opts, rest = parse_git_options(
        args, options_with_value, flags=flag_options,
        stop_at_positional=True, strict=True)
    if not rest:
        return None
    head = rest[0]
    # 空 token / `git -- ...`（-- 作全局 separator，语义不明）/ 未识别全局选项：
    # 均 fallthrough 到原生 git（行为与原 execvp 一致）。
    if not head or head == '--' or head.startswith('-'):
        return None
    return ParsedCommand(global_opts, head, rest[1:])


def effective_cwd(global_opts: List[str]) -> str:
    cwd = os.getcwd()
    i = 0
    while i < len(global_opts):
        arg = global_opts[i]
        if arg == '-C' and i + 1 < len(global_opts):
            path = global_opts[i + 1]
            cwd = path if os.path.isabs(
                path) else os.path.abspath(os.path.join(cwd, path))
            i += 1
        i += 1
    return cwd


def _init_or_repair_pool() -> None:
    """确保 POOL_PATH 是一个有效的裸仓：不存在则 init；探测损坏则归档重建。

    调用方须持有 pool_lock。
    """
    if not POOL_PATH.exists():
        print(f'[Wrapper] 初始化全局对象池: {POOL_PATH}', file=sys.stderr)
        run_git(['init', '--bare', str(POOL_PATH)])
        run_git(['--git-dir', str(POOL_PATH), 'config', 'gc.auto', '0'], check=False)
        return
    # check=False: 用 rev-parse 探测裸仓是否完好，失败即视作损坏并重建
    result = run_git(['--git-dir', str(POOL_PATH), 'rev-parse', '--git-dir'],
                     capture=True, check=False)
    if result.returncode == 0:
        return
    # 探测到一次损坏即归档重建：进程级计数无法跨进程累计，
    # 「连续 N 次」语义不成立，故一旦探测失败立刻归档并重建。
    broken_path = POOL_PATH.parent / \
        f'{POOL_PATH.name}.broken.{int(time.time() * 1000)}'
    print(f'[Wrapper] warning: 全局对象池损坏（探测失败），'
          f'归档到 {broken_path} 后重新初始化: {POOL_PATH}', file=sys.stderr)
    POOL_PATH.rename(broken_path)
    init_res = run_git(
        ['init', '--bare', str(POOL_PATH)], capture=True, check=False)
    if init_res.returncode != 0:
        print(f'[Wrapper] warning: 池 init --bare 失败，回滚归档: '
              f'{(init_res.stderr or "").strip()}', file=sys.stderr)
        broken_path.rename(POOL_PATH)


def _register_pool_remote(url: str) -> str:
    """为 url 生成唯一 remote 名并登记/更新到池中，返回该 remote 名。

    不存在则 add，URL 变化则 set-url。调用方须持有 pool_lock。
    """
    name = _url_to_remote_name(url)
    # check=False: remote 不存在时 get-url 返回非零，按「需新增」处理
    existing = run_git(['--git-dir', str(POOL_PATH), 'remote', 'get-url', name],
                       capture=True, check=False)
    if existing.returncode != 0:
        run_git(['--git-dir', str(POOL_PATH), 'remote', 'add', name, url])
        print(f'[Wrapper] 预热对象池（首次拉取 {url} 可能需要较长时间）...', file=sys.stderr)
    elif existing.stdout.strip() != url:
        run_git(['--git-dir', str(POOL_PATH),
                'remote', 'set-url', name, url])
    return name


def _fetch_pool_remote(name: str, url: str) -> None:
    """fetch 指定池内 remote（--prune）。进程内同 URL 只 fetch 一次（去重加速）。

    调用方须持有 pool_lock。
    """
    global _fetched_urls
    # 进程内去重：同一 URL 在本次进程内只 fetch 一次，避免递归 submodule 重复拉取
    if url in _fetched_urls:
        return
    # check=False: 池更新失败不应中断主命令（worktree 仍可用旧对象 + 后续直连）
    run_git(['--git-dir', str(POOL_PATH), 'fetch', name, '--prune', '--progress'],
            capture=False, check=False)
    _fetched_urls.add(url)


def ensure_pool_bare_repo(url: str):
    """全局唯一池裸仓 ensure：固定使用 POOL_PATH。并发安全（文件锁）。

    编排四个职责单一的步骤：
    1) 初始化池目录骨架（_ensure_pool_dir）
    2) 确保池裸仓有效（_init_or_repair_pool：不存在则 init，损坏则归档重建）
    3) 登记/更新该 URL 的池内 remote（_register_pool_remote）
    4) fetch 该 remote（_fetch_pool_remote，进程内同 URL 去重）
    """
    _ensure_pool_dir()
    with pool_lock():
        _init_or_repair_pool()
        name = _register_pool_remote(url)
        _fetch_pool_remote(name, url)


def should_skip_pool(url: str) -> bool:
    """返回 True 表示该 URL 不应进池（本地路径 / file:// / 相对路径）。
    注意：相对 URL 在调用前应先用 resolve_relative_url 解析为绝对 URL。"""
    if not url:
        return True
    # 统一基于 expanded 判断：os.path.expanduser 仅展开 ~ 前缀，file:// 等不含 ~ 的串
    # 原样返回，故 expanded.startswith('file://') 与 url.startswith('file://') 等价，
    # 这里统一用 expanded，避免 url / expanded 混用。
    expanded = os.path.expanduser(url)
    return (expanded.startswith('./') or expanded.startswith('../') or
            expanded.startswith('/') or expanded.startswith('file://'))


def replace_alternates(gitdir: str, pool_path: Path):
    objects_dir = Path(gitdir) / 'objects'
    info_dir = objects_dir / 'info'
    info_dir.mkdir(parents=True, exist_ok=True)
    alternates = info_dir / 'alternates'
    alternate = str(pool_path / 'objects')
    # 幂等：文件已存在且内容完全匹配，跳过写入
    if alternates.exists():
        try:
            existing = [l.strip() for l in alternates.read_text().splitlines() if l.strip()]
            if existing == [alternate]:
                return
        except OSError:
            pass
    tmp = info_dir / 'alternates.tmp'
    with open(tmp, 'w') as f:
        f.write(alternate + '\n')
    os.replace(str(tmp), str(alternates))


# ---------- clone / fetch ----------
def do_clone(global_opts: List[str], subcmd_args: List[str]):
    clone_args = subcmd_args
    if (has_option(clone_args, '--reference-if-able') or has_option(clone_args, '--reference') or
            has_option(clone_args, '--shared') or
            has_option(clone_args, '--bare') or has_option(clone_args, '--mirror') or
            has_option(clone_args, '--dissociate')):
        return _exec_passthrough(global_opts + ['clone'] + subcmd_args)

    passthrough_args, positionals = parse_clone_args(clone_args)
    if not positionals:
        return _exec_passthrough(global_opts + ['clone'] + subcmd_args)

    url = _sanitize_url(positionals[0])
    if should_skip_pool(url):
        return _exec_passthrough(global_opts + ['clone'] + subcmd_args)

    dest = positionals[1] if len(positionals) > 1 else os.path.basename(
        url.rstrip('/')).removesuffix('.git')
    ensure_pool_bare_repo(url)

    new_argv = global_opts + ['clone']
    new_argv.extend(passthrough_args)
    new_argv.append(f'--reference={str(POOL_PATH)}')
    new_argv.append(url)
    new_argv.append(dest)

    run_git(new_argv, capture=False)

    clone_cwd = effective_cwd(global_opts)
    new_dot_git = os.path.join(
        os.path.abspath(
            os.path.join(
                clone_cwd,
                dest)),
        '.git')
    # 复用 resolve_gitdir 解析 `.git`（目录或 gitdir: 指针文件），不再在此重复解析逻辑
    real_gitdir = resolve_gitdir(new_dot_git)
    if real_gitdir:
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


def _current_tracking_remote(gitdir: str) -> str:
    """返回当前分支配置的 tracking remote 名（branch.<name>.remote）；无法确定时返回空串。"""
    # check=False: detached HEAD / 无分支时返回非零，按「无 tracking remote」处理
    br = run_git(['--git-dir', gitdir, 'symbolic-ref', '--short', '-q', 'HEAD'],
                 capture=True, check=False)
    branch = br.stdout.strip()
    if br.returncode != 0 or not branch:
        return ''
    # check=False: 分支未配置 remote 时返回非零，按空串处理
    rm = run_git(['--git-dir', gitdir, 'config', '--get', f'branch.{branch}.remote'],
                 capture=True, check=False)
    return rm.stdout.strip() if rm.returncode == 0 else ''


def _snapshot_objects(gd: str) -> Tuple:
    pack_dir = os.path.join(gd, 'objects', 'pack')
    obj_dir = os.path.join(gd, 'objects')
    try:
        packs = frozenset(
            f for f in os.listdir(pack_dir) if f.endswith('.pack')
        ) if os.path.isdir(pack_dir) else frozenset()
    except OSError:
        packs = frozenset()
    try:
        obj_mtime = os.stat(obj_dir).st_mtime_ns
    except OSError:
        obj_mtime = 0
    try:
        pack_mtime = os.stat(pack_dir).st_mtime_ns if os.path.isdir(
            pack_dir) else 0
    except OSError:
        pack_mtime = 0
    return (packs, obj_mtime, pack_mtime)


def do_fetch(global_opts: List[str], subcmd_args: List[str]):
    gitdir_override = extract_gitdir_override(global_opts)
    gitdir = gitdir_override if gitdir_override else find_gitdir(
        effective_cwd(global_opts))
    if not gitdir:
        return _exec_passthrough(global_opts + ['fetch'] + subcmd_args)
    # --unshallow / --update-shallow / --dry-run 不触发池处理，直接透传（进程替换）
    if _SHALLOW_BYPASS_FLAGS.intersection(subcmd_args):
        return _exec_passthrough(global_opts + ['fetch'] + subcmd_args)
    if '--dry-run' in subcmd_args:
        return _exec_passthrough(global_opts + ['fetch'] + subcmd_args)

    # 注意：_exec_passthrough 是进程替换/退出，会导致后续池处理无法执行。
    # 改为先用 run_git 执行原生 fetch（透传用户参数），再做池后处理，最后按其退出码退出。
    # check=False: fetch 失败也要回收退出码，由本函数末尾 sys.exit 统一返回

    # 已 migrate 时：fetch 前快照本地 objects 状态，用于事后判断是否真的有新对象落地。
    # pack 文件名（含 SHA1）变化 → 有新 pack 进来；objects/ 目录 mtime 变化 → 有 loose object 落地。
    # 两者都没变说明 fetch 没带来任何新对象，可安全跳过 _fetch_local_to_pool + repack。
    target_objects = os.path.abspath(str(POOL_PATH / 'objects'))
    already_migrated = target_objects in read_alternates(gitdir)

    pre_snapshot = _snapshot_objects(gitdir) if already_migrated else None

    # 已 migrate 的 submodule 的 pre-snapshot：与主仓库 pre-snapshot 同一时机采集。
    # `git fetch --all` 会拉所有已初始化 submodule 的新对象，落到各自 .git/modules/<sub>/objects/，
    # 不进池。这里枚举 .git/modules/ 下已 migrate 的 submodule gitdir，事后判断是否需要进池。
    sub_pre_snapshots: Dict[str, tuple] = {}
    modules_dir = Path(gitdir) / 'modules'
    if already_migrated and modules_dir.is_dir():
        for sub_gitdir in _scan_orphan_module_gitdirs(modules_dir):
            sub_gitdir_str = str(sub_gitdir)
            # 未 migrate 的 submodule 跳过
            if target_objects in read_alternates(sub_gitdir_str):
                sub_pre_snapshots[sub_gitdir_str] = _snapshot_objects(
                    sub_gitdir_str)

    fetch_result = run_git(global_opts + ['fetch'] + subcmd_args, check=False)

    if already_migrated:
        post_snapshot = _snapshot_objects(gitdir)
        objects_changed = (pre_snapshot != post_snapshot)
        if objects_changed:
            # fetch 带来了新对象：搬进池，再 repack 收缩本地存储
            _fetch_local_to_pool(gitdir, POOL_PATH)
            # check=False: repack 失败仅打印警告，不抛
            res = run_git(['--git-dir', gitdir, 'repack', '-a',
                          '-d', '--local'], capture=True, check=False)
            if res.returncode != 0:
                print(
                    f'[Wrapper] warning: fetch 后 repack 失败: {res.stderr.strip()}',
                    file=sys.stderr)
        # objects_changed == False：up-to-date，无新对象落地，跳过后处理

        # 已 migrate 的 submodule 的对称后处理：对比前后快照，有变化才进池 + repack。
        # 失败仅 warning，不影响主仓库 fetch 退出码。
        for sub_gitdir_str, sub_pre in sub_pre_snapshots.items():
            try:
                sub_post = _snapshot_objects(sub_gitdir_str)
                if sub_pre == sub_post:
                    continue
                _fetch_local_to_pool(sub_gitdir_str, POOL_PATH)
                res = run_git(['--git-dir', sub_gitdir_str, 'repack', '-a',
                              '-d', '--local'], capture=True, check=False)
                if res.returncode != 0:
                    print(
                        f'[Wrapper] warning: submodule {sub_gitdir_str} fetch 后 repack 失败: {res.stderr.strip()}',
                        file=sys.stderr)
            except Exception as e:
                print(
                    f'[Wrapper] warning: submodule {sub_gitdir_str} 池后处理失败: {e}',
                    file=sys.stderr)
    else:
        # 未 migrate：取 remote URL（当前分支 tracking remote → fallback origin），预热全局池
        remote_url = ''
        tracking = _current_tracking_remote(gitdir)
        if tracking:
            remote_url = get_remote_url_by_name(gitdir, tracking)
        if not remote_url:
            remote_url = get_remote_url_by_name(gitdir, 'origin')
        if remote_url and not should_skip_pool(remote_url):
            ensure_pool_bare_repo(remote_url)

    sys.exit(fetch_result.returncode)


# ---------- submodule（自驱方案） ----------
def resolve_relative_url(parent_remote: str, sub_url: str) -> str:
    """相对 URL（./../）相对父仓 remote.origin.url 解析为绝对 URL。

    parent_remote 为空时返回原相对路径，should_skip_pool 会将其判定为本地路径跳过池化，
    属预期安全降级。
    """
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
    # check=False: 无 .gitmodules 或解析失败时按「无 submodule」处理，返回空列表
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


def submodule_target_commit(parent_worktree: str,
                            sub_path: str) -> Optional[str]:
    """从父 worktree 取 submodule 在 HEAD 中的目标 commit。"""
    # check=False: HEAD 缺失 / 路径非 submodule 时返回非零，按「无目标 commit」处理
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
    # check=False: 未配置 remote.origin.url 时返回非零，按空 URL 处理
    res = run_git(['-C', worktree, 'config', '--get', 'remote.origin.url'],
                  capture=True, check=False)
    return _sanitize_url(res.stdout.strip())


def get_remote_url_by_name(gitdir: str, remote_name: str) -> str:
    """返回指定 remote 名的 fetch URL（已 sanitize）；不存在或为本地路径时返回空串。"""
    # check=False: remote 不存在时返回非零，按空 URL 处理
    r = run_git(['--git-dir', gitdir, 'remote', 'get-url',
                remote_name], capture=True, check=False)
    if r.returncode != 0:
        return ''
    return _sanitize_url(r.stdout.strip())


def run_native_submodule_update(parent_worktree: str, sub_path: str,
                                recursive: bool, depth: Optional[str], buf: List[str]):
    """update=rebase/merge 等场景：池已预热后，调原生 git 执行原始策略。"""
    args = ['-C', parent_worktree, 'submodule', 'update', '--init']
    if recursive:
        args.append('--recursive')
    if depth:
        args.extend(['--depth', str(depth)])
    args.append('--')
    args.append(sub_path)
    res = subprocess.run(
        [SYSTEM_GIT] + args,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL)
    if res.stdout:
        buf.append(res.stdout.rstrip('\n'))
    if res.stderr:
        buf.append(res.stderr.rstrip('\n'))


def _resolve_submodule_gitdir(parent_worktree: str, toplevel_gitdir: Optional[str],
                              name: str) -> Optional[str]:
    """计算 submodule 的 gitdir 路径（<toplevel-or-parent>/.git/modules/<name>）。

    无法定位父仓 gitdir 时返回 None。
    """
    if toplevel_gitdir:
        return os.path.join(toplevel_gitdir, 'modules', name)
    parent_gd = find_gitdir(parent_worktree)
    if not parent_gd:
        return None
    return os.path.join(parent_gd, 'modules', name)


def _detect_submodule_initialized(sub_worktree: str, gitdir: str, sub_path: str,
                                  buf: List[str]) -> Tuple[bool, str]:
    """判断 submodule 是否已初始化，返回 (initialized, gitdir)。

    三种「已初始化」情形（命中即按更新路径处理）：
      a) worktree 与计算出的 gitdir 都存在，且 worktree 内有 .git（指针文件或目录）；
      b) worktree 内有 .git（可能由父 submodule 递归初始化），gitdir 以真实解析为准；
      c) worktree 已存在且非空但无 .git：跳过 clone，按已初始化走更新路径。
    """
    wt_git = os.path.join(sub_worktree, '.git')
    has_dot_git = os.path.isfile(wt_git) or os.path.isdir(wt_git)
    # 情形 a：保留计算出的 gitdir（更新路径会再用 find_gitdir 校正）
    if os.path.isdir(sub_worktree) and os.path.isdir(gitdir) and has_dot_git:
        return True, gitdir
    # 情形 b：worktree 已由外部建好，找真实 gitdir
    if os.path.isdir(sub_worktree) and has_dot_git:
        return True, find_gitdir(sub_worktree) or gitdir
    # 情形 c：非空 worktree 无 .git，跳过 clone
    if os.path.isdir(sub_worktree) and os.listdir(sub_worktree):
        buf.append(
            f"[Wrapper] submodule {sub_path}: worktree 已存在且非空，跳过 clone")
        return True, find_gitdir(sub_worktree) or gitdir
    return False, gitdir


def _clone_submodule(url: str, gitdir: str, sub_worktree: str, sub_path: str,
                     depth: Optional[str], filter_: Optional[str], buf: List[str]) -> bool:
    """首次初始化：clone 进池（--no-checkout --separate-git-dir --reference 池）。

    成功返回 True，失败记录错误并返回 False。
    """
    ensure_pool_bare_repo(url)
    os.makedirs(os.path.dirname(gitdir), exist_ok=True)
    os.makedirs(sub_worktree, exist_ok=True)
    clone_args = ['clone', '--no-checkout',
                  '--separate-git-dir', gitdir,
                  '--reference', str(POOL_PATH)]
    if depth:
        clone_args.extend(['--depth', str(depth)])
    if filter_:
        clone_args.extend([f'--filter={filter_}'])
    clone_args.extend([url, sub_worktree])
    # check=False: clone 失败由下方据 returncode 显式记录错误并跳过该 submodule
    res = run_git(clone_args, capture=True, check=False)
    if res.returncode != 0:
        if res.stderr:
            buf.append(res.stderr.rstrip('\n'))
        buf.append(
            f"[Wrapper] error: clone failed for submodule {sub_path}")
        return False
    replace_alternates(gitdir, POOL_PATH)
    register_shell(gitdir)
    return True


def _update_existing_submodule(url: str, gitdir: str, sub_worktree: str,
                               commit: str) -> str:
    """已存在：定位真实 gitdir + 池预热 + alternates + fetch，返回最终 gitdir。

    快路径：已 alternates 到池且目标 commit 本地可达时，跳过 pool fetch 和 gitdir fetch。
    """
    existing_gd = find_gitdir(sub_worktree)
    if existing_gd:
        gitdir = existing_gd
    target_objects = os.path.abspath(str(POOL_PATH / 'objects'))
    already_in_pool = target_objects in read_alternates(gitdir)
    commit_reachable = (
        already_in_pool and
        run_git(['--git-dir', gitdir, 'cat-file', '-e', commit],
                capture=True, check=False).returncode == 0
    )
    if commit_reachable:
        # alternates 已是目标池，幂等调用直接返回；register_shell 同样幂等
        replace_alternates(gitdir, POOL_PATH)
        register_shell(gitdir)
    else:
        ensure_pool_bare_repo(url)
        replace_alternates(gitdir, POOL_PATH)
        register_shell(gitdir)
        # check=False: 池已预热，fetch 更新失败不应中断 checkout（可用已有对象）
        run_git(['--git-dir', gitdir, 'fetch', '--all', '--prune'],
                capture=True, check=False)
    return gitdir


def _checkout_submodule(gitdir: str, sub_worktree: str, sub_path: str,
                        commit: str, buf: List[str]) -> bool:
    """checkout --detach 到目标 commit；本地缺对象时 fetch 兜底并重试一次。

    成功返回 True，失败记录错误并返回 False。
    """
    # check=False: 首次 checkout 失败后走 fetch 兜底重试，故此处忽略错误
    res = run_git(['--git-dir', gitdir, '--work-tree', sub_worktree,
                   'checkout', '--detach', commit, '-q'],
                  capture=True, check=False)
    if res.returncode != 0:
        # 若本地缺该 commit，再 fetch 一次
        # check=False: fetch 兜底，失败由下方二次 checkout 的 returncode 统一判定
        run_git(['--git-dir', gitdir, 'fetch', 'origin', commit],
                capture=True, check=False)
        # check=False: 二次 checkout 结果由下方 returncode 决定成功/失败分支
        res = run_git(['--git-dir', gitdir, '--work-tree', sub_worktree,
                       'checkout', '--detach', commit, '-q'],
                      capture=True, check=False)
    if res.returncode == 0:
        buf.append(f"Submodule path '{sub_path}': checked out '{commit}'")
        return True
    if res.stderr:
        buf.append(res.stderr.rstrip('\n'))
    buf.append(
        f"[Wrapper] error: checkout {commit} failed for submodule {sub_path}")
    return False


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
        run_native_submodule_update(
            parent_worktree, sub_path, False, depth, buf)
        if recursive and os.path.isdir(sub_worktree):
            nested = parse_gitmodules(sub_worktree)
            if nested:
                nested_remote = get_remote_url(sub_worktree) or url
                process_submodule_level(sub_worktree, nested_remote, toplevel_gitdir,
                                        nested, recursive, jobs, depth, filter_)
        return

    # 取目标 commit
    commit = submodule_target_commit(parent_worktree, sub_path)
    if not commit:
        buf.append(
            f"[Wrapper] warning: cannot resolve target commit for submodule {sub_path}, skip")
        return

    # 计算 gitdir：<toplevel-or-parent>/.git/modules/<name>
    gitdir = _resolve_submodule_gitdir(parent_worktree, toplevel_gitdir, name)
    if gitdir is None:
        buf.append(
            f"[Wrapper] warning: cannot find parent gitdir for {sub_path}, skip")
        return

    # 判断是否已初始化（已存在则在此校正 gitdir）
    initialized, gitdir = _detect_submodule_initialized(
        sub_worktree, gitdir, sub_path, buf)

    if not initialized:
        # 首次：clone 进池 → 通过 --reference 引用全局唯一池
        if not _clone_submodule(url, gitdir, sub_worktree, sub_path, depth, filter_, buf):
            return
    else:
        # 已存在：定位 gitdir + 池预热 + alternates + fetch
        gitdir = _update_existing_submodule(url, gitdir, sub_worktree, commit)

    # checkout / rebase / merge
    if update in ('rebase', 'merge'):
        # 预热 pool 后再 fallback 到原生 git submodule update 处理 rebase/merge 策略。
        # 修复：快路径（commit_reachable=True）下 _update_existing_submodule 跳过了
        # ensure_pool_bare_repo，导致原生 update 时 pool 未预热；此处显式预热（_fetched_urls
        # 去重，已预热则为幂等空操作），保证 rebase/merge 分支同样满足「池已预热」前提。
        ensure_pool_bare_repo(url)
        run_native_submodule_update(
            parent_worktree, sub_path, False, depth, buf)
    else:
        if not _checkout_submodule(gitdir, sub_worktree, sub_path, commit, buf):
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
        except GitCommandError as e:
            bufs[i].append(
                f"[Wrapper] error processing submodule {subs[i].get('name')}: exit {e.returncode}")
        except Exception as e:
            bufs[i].append(
                f"[Wrapper] error processing submodule {subs[i].get('name')}: {e}")

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


def parse_submodule_update_options(
        subcmd_args: List[str]) -> Tuple[bool, int, Optional[str], Optional[str], List[str]]:
    """解析 git submodule update 的 --recursive / --jobs / --depth / --filter 与末尾 path。

    复用 parse_git_option_values 单趟分词并直接产出「选项 -> 值」字典，一步取值即可，
    无需「先合并选项再二次循环提取」。调用方保证 subcmd_args[0] 为 'update'，这里先剥掉
    该 subcommand 名，避免靠字符串比较把它从 path 列表中排除。
    """
    args = subcmd_args[1:] if subcmd_args and subcmd_args[0] == 'update' else subcmd_args
    options_with_value = {'-j', '--jobs', '--depth', '--filter'}
    values, paths = parse_git_option_values(args, options_with_value)
    recursive = False
    jobs = 1
    depth: Optional[str] = None
    filter_: Optional[str] = None
    for name, val in values.items():
        if name == '--recursive':
            recursive = True
        elif name in ('-j', '--jobs') and isinstance(val, str):
            try:
                jobs = int(val)
            except ValueError:
                pass
        elif name == '--depth' and isinstance(val, str):
            depth = val
        elif name == '--filter' and isinstance(val, str):
            filter_ = val
    if jobs < 1:
        jobs = 1
    return recursive, jobs, depth, filter_, paths


def do_submodule_update_init(global_opts: List[str], subcmd_args: List[str]):
    """自驱实现 submodule update --init [--recursive --jobs --depth --filter]。

    核心不变式：
    1) 任何网络下载必先进池（ensure_pool_bare_repo）
    2) worktree 仅通过 --reference 引用池对象，不加 --dissociate
    3) 菱形/共享依赖复用同一池裸仓（URL 相同 → 路径相同）
    4) update=none 纯 skip
    5) 相对 URL 先解析为绝对 URL 再走池流程
    """
    recursive, jobs, depth, filter_, paths = parse_submodule_update_options(
        subcmd_args)

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
    return _exec_passthrough(global_opts + ['submodule'] + subcmd_args)


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


def registered_shells() -> List[str]:
    clean_registry()
    if not REGISTRY_FILE.exists():
        return []
    with open(REGISTRY_FILE, 'r') as f:
        return [line.strip() for line in f if line.strip()
                and os.path.isdir(line.strip())]


def pool_dependents(shells: List[str]) -> List[str]:
    """返回 alternates 指向全局唯一池（POOL_PATH/objects）的 worktree 列表。"""
    target_objects = os.path.abspath(str(POOL_PATH / 'objects'))
    return [shell for shell in shells if target_objects in read_alternates(shell)]


def _check_rev_list_ok(gitdir: str) -> bool:
    # check=False: 枚举失败时返回 False，让调用方走保守路径，绝不返回残缺集合
    result = run_git(['--git-dir', gitdir, 'rev-list', '--all', '--objects'],
                     capture=True, check=False)
    if result.returncode != 0:
        print(
            f'[Wrapper] warning: rev-list failed for {gitdir}',
            file=sys.stderr)
        return False
    return True


def all_shells_enumerable(shells: List[str]) -> bool:
    """探测所有依赖 worktree 能否完整枚举可达对象（rev-list --all --objects）。

    原 collect_shell_live_objects 把所有 worktree 的可达对象汇成一个巨大的 set，但
    调用方仅用它判断「能否完整枚举」（is None），从不查询集合成员；大仓库下会无谓驻留
    百万级 SHA 字符串。这里改为逐个 worktree 探测、只返回布尔，per-shell 的临时集合用完
    即弃，不再跨 worktree 累积。

    任一 worktree 枚举失败返回 False，让调用方走保守 prune=never，绝不误删。
    """
    for shell in shells:
        if not _check_rev_list_ok(shell):
            print(f'[Wrapper] warning: all_shells_enumerable 失败于 {shell}，'
                  f'无法完整枚举（保守处理）', file=sys.stderr)
            return False
    return True


def count_pool_objects(gitdir: str) -> Optional[int]:
    """统计仓库可达对象数量（rev-list --all --objects 的行数），失败返回 None。

    原 gc_pool_repos 用 rev-list 建出完整 SHA 集合，但仅用于打印数量与 None
    判定，从不查询成员；这里只累加计数，省去驻留整套对象集合的内存。
    """
    # check=False: 枚举失败时返回 None，让调用方走保守路径
    result = run_git(['--git-dir', gitdir, 'rev-list', '--all', '--objects'],
                     capture=True, check=False)
    if result.returncode != 0:
        print(f'[Wrapper] warning: rev-list failed for {gitdir}', file=sys.stderr)
        return None
    count = 0
    for line in result.stdout.splitlines():
        if line:
            count += 1
    return count


def collect_shell_live_commits(shells: List[str]) -> Optional[set]:
    """收集所有依赖 worktree 中 rev-list --all 可达的 commit SHA 集合。
    用于在池中建临时保护 ref（refs/object-pool/<sha>），保证 prune
    真正按 "shell 实际需要的对象" 做保护，而不仅是 ref tip。任一失败返回 None。
    """
    tmp = None
    try:
        tmp = tempfile.NamedTemporaryFile(mode='w+', encoding='ascii', delete=False)
        tmp_path = tmp.name
        with tmp:
            for shell in shells:
                # check=False: 枚举失败时返回 None，调用方据此走保守 prune=never
                res = run_git(['--git-dir', shell, 'rev-list', '--all'],
                              capture=True, check=False)
                if res.returncode != 0:
                    print(f'[Wrapper] warning: rev-list --all 失败于 {shell}，'
                          f'放弃 live commits 集合（保守处理）', file=sys.stderr)
                    return None
                tmp.write(res.stdout)
                if res.stdout and not res.stdout.endswith('\n'):
                    tmp.write('\n')

        sort_res = subprocess.run(['sort', '-u', tmp_path],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  text=True, check=False)
        if sort_res.returncode != 0:
            stderr = sort_res.stderr.strip() if sort_res.stderr else ''
            print(f'[Wrapper] warning: sort -u live commits 失败（exit {sort_res.returncode}）: {stderr}',
                  file=sys.stderr)
            return None

        commits = set()
        for line in sort_res.stdout.splitlines():
            sha = line.strip()
            if sha:
                commits.add(sha)
        return commits
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f'[Wrapper] warning: 删除临时文件失败 {tmp.name}: {e}',
                      file=sys.stderr)


def collect_repo_ref_tips(gitdir: str, include_annotated_tags: bool = False) -> List[str]:
    """收集一个仓库 gitdir 的分支 tip（refs/heads/*）与 HEAD 指向的 commit SHA。

    默认语义：仅含 refs/heads/* 与 HEAD，**不含 tag**。
    migrate 与 gc-protect 的 ref tip 枚举都只需保护分支 tip：
    - migrate：fetch 阶段已用显式 refspec 把所有 heads/tags 对象搬进池；
    - gc-protect：tag 可达的 commit 由 collect_shell_live_commits（rev-list --all，
      含 refs/tags）兜底进 protect_commits。

    include_annotated_tags=True 时额外并入 refs/tags/* 中 objecttype == 'tag' 的注解 tag
    自身对象 sha（不 deref）；轻量 tag（objecttype=commit）其 commit 已由其他途径覆盖，
    不重复加入。gc-protect 给依赖 worktree 建保护 ref 时用此模式（原 collect_worktree_ref_commits）。

    返回去重后的 SHA 列表。
    """
    tips: set = set()
    # check=False: 空仓库 / 无分支时 for-each-ref 仍返回 0 但无输出；非零按空集处理
    res = run_git(['--git-dir', gitdir, 'for-each-ref', '--format=%(objectname)', 'refs/heads/'],
                  capture=True, check=False)
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            sha = line.strip()
            if sha:
                tips.add(sha)
    # check=False: detached / 无 HEAD（空仓库）时 rev-parse 返回非零，按「无 HEAD」处理
    head = run_git(['--git-dir', gitdir, 'rev-parse', '--verify', '-q', 'HEAD'],
                   capture=True, check=False)
    head_sha = head.stdout.strip()
    if head.returncode == 0 and head_sha:
        tips.add(head_sha)
    if include_annotated_tags:
        # check=False: 无 tag 时返回 0 但无输出；非零按空集处理
        res = run_git(['--git-dir', gitdir, 'for-each-ref',
                       '--format=%(objectname) %(objecttype)', 'refs/tags/'],
                      capture=True, check=False)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == 'tag':
                    tips.add(parts[0])
    return list(tips)


_OBJECT_POOL_NS = 'refs/object-pool/'


def add_gc_protect_refs(pool_repo: Path, commits: set) -> int:
    """在池裸仓内为给定 commit 创建临时保护 ref（refs/object-pool/<sha>）。

    仅对已存在于池中的对象创建 ref；先用 git cat-file -e 判断存在性，
    避免 update-ref 因对象缺失而报错。返回成功创建的 ref 数量。
    """
    created = 0
    for sha in commits:
        # check=False: cat-file -e 是存在性探测，对象不在池中返回非零属正常分支
        exists = run_git(['--git-dir', str(pool_repo), 'cat-file', '-e', sha],
                         capture=True, check=False)
        if exists.returncode != 0:
            continue
        # check=False: 个别 ref 创建失败不应中断整体保护，按成功计数即可
        res = run_git(['--git-dir', str(pool_repo), 'update-ref', _OBJECT_POOL_NS + sha, sha],
                      capture=True, check=False)
        if res.returncode == 0:
            created += 1
    return created


def cleanup_object_pool_refs(pool_repo: Path):
    """删除池裸仓内所有 refs/object-pool/ 临时保护 ref（含上次异常残留）。

    migrate 与 gc-protect 共用此命名空间，清理时统一删除。
    """
    # check=False: 枚举/删除均为尽力而为的清理，失败不应让主流程中断
    res = run_git(['--git-dir', str(pool_repo), 'for-each-ref',
                   '--format=%(refname)', _OBJECT_POOL_NS],
                  capture=True, check=False)
    for refname in res.stdout.splitlines():
        refname = refname.strip()
        if refname:
            run_git(['--git-dir', str(pool_repo), 'update-ref', '-d', refname],
                    capture=True, check=False)


def gc_pool_repos(extra_args: Optional[List[str]] = None):
    """全局唯一对象池 GC。

    extra_args 是用户通过 `git gc <flags>` 传入、被白名单允许的参数子集，
    会按需要转发给内部的 repack/prune（见 do_gc 中的白名单）。
    """
    extra_args = extra_args or []
    if not POOL_PATH.exists():
        return
    pass_quiet = '--quiet' in extra_args
    pass_aggressive = '--aggressive' in extra_args
    # --prune=never：用户显式要求不 prune，则池 GC 也跳过 prune（仅 repack 整理）
    prune_never = '--prune=never' in extra_args
    # 频率限制——距上次全池 GC 不足 GC_MIN_INTERVAL 则跳过本次。
    now = time.time()
    try:
        last_run = float(GC_LAST_RUN_FILE.read_text().strip())
    except (OSError, ValueError):
        last_run = 0.0
    if now - last_run < GC_MIN_INTERVAL:
        print(f'[Wrapper] 距上次全池 GC 不足 {GC_MIN_INTERVAL}s '
              f'（{int(now - last_run)}s 前），跳过本次全池 GC', file=sys.stderr)
        return
    # 提前写入时间戳：即便本次 GC 中途异常，也不会让短时间内的重复调用反复触发全池 GC。
    try:
        GC_LAST_RUN_FILE.write_text(str(now))
    except OSError as e:
        print(
            f'[Wrapper] warning: 写入 {GC_LAST_RUN_FILE} 失败: {e}',
            file=sys.stderr)

    shells = registered_shells()
    dependents = pool_dependents(shells)
    # shells_enumerable：所有依赖 worktree 能否完整枚举可达对象。仅作「能否完整枚举」的
    # 保守判据：任一 worktree 枚举失败（False）则对应池仓走 prune=never，避免误删。
    # （不再构造跨 worktree 的巨型对象集合，省内存。）
    shells_enumerable = all_shells_enumerable(shells)
    # shell_live_commits：worktree rev-list --all 的全部 commit（含 refs/tags 可达）。
    # 真正驱动 prune 保护——连同各 worktree 的 branch tip 一起在池内建临时保护 ref。
    shell_live_commits = collect_shell_live_commits(shells)
    repo = POOL_PATH
    gc_quiet_args = ['--quiet'] if pass_quiet else []
    if not dependents:
        print(
            f'[Wrapper] GC 池裸仓: {repo} (无 worktree 依赖，保守 prune=never)',
            file=sys.stderr)
        # check=False: 无依赖时仅整理、不阻断
        run_git(['--git-dir', str(repo), 'gc', '--prune=never'] +
                gc_quiet_args, check=False)
        return
    pool_object_count = count_pool_objects(str(repo))
    if not shells_enumerable or pool_object_count is None or shell_live_commits is None:
        print(
            f'[Wrapper] GC 池裸仓: {repo} (依赖分析失败，prune=never)',
            file=sys.stderr)
        res = run_git(['--git-dir', str(repo), 'gc', '--prune=never'] + gc_quiet_args,
                      capture=True, check=False)
        if res.returncode != 0:
            print(f'[Wrapper] warning: gc --prune=never 失败 for {repo}: '
                  f'{(res.stderr or "").strip()}', file=sys.stderr)
        return
    print(f'[Wrapper] GC 池裸仓: {repo} (被 {len(dependents)} 个 worktree 依赖，'
          f'pool_objects={pool_object_count})', file=sys.stderr)
    # prune/repack 前，把所有依赖 worktree 的 branch tip + HEAD 在池内用临时 ref
    # 保护起来：即使 local-only commit 在池端没有任何 ref 指向（_fetch_local_to_pool
    # fetch 后已删临时 ref），也不会被 prune 误删。再并入 shell_live_commits
    # （worktree rev-list --all 的全部 commit），使 prune 决策与 shell 实际所需挂钩。
    protect_commits = set()
    for dep in dependents:
        protect_commits.update(collect_repo_ref_tips(dep, include_annotated_tags=True))
    protect_commits.update(shell_live_commits)
    # 先清理上次异常残留的保护 ref，再创建本次保护 ref。
    cleanup_object_pool_refs(repo)
    protected = add_gc_protect_refs(repo, protect_commits)
    print(
        f'[Wrapper]   gc-protect: 保护 {protected}/{len(protect_commits)} 个 commit',
        file=sys.stderr)
    try:
        if not prune_never:
            prune_args = ['--git-dir', str(repo), 'prune', '--expire=now']
            if pass_quiet:
                prune_args.append('--quiet')
            res = run_git(prune_args, capture=True, check=False)
            if res.returncode != 0:
                print(f'[Wrapper] warning: pool prune 失败 for {repo}: '
                      f'{(res.stderr or "").strip()}', file=sys.stderr)
        repack_args = ['--git-dir', str(repo), 'repack', '-Ad']
        if pass_quiet:
            repack_args.append('-q')
        if pass_aggressive:
            repack_args.append('-f')  # repack 的 aggressive 等价：强制重新计算 delta
        res = run_git(repack_args, capture=True, check=False)
        if res.returncode != 0:
            print(f'[Wrapper] warning: pool repack 失败 for {repo}: '
                  f'{(res.stderr or "").strip()}', file=sys.stderr)
    finally:
        # 无论 prune/repack 成功与否，都清除临时保护 ref，避免污染池裸仓。
        cleanup_object_pool_refs(repo)


def do_gc_command(global_opts: List[str], subcmd_args: List[str]):
    """拦截 `git gc`：先对整个对象池做一次池 GC，再把原始 gc 透传给原生 git
    （作用于当前仓库）。语义即「先池 gc，再透传」。

    放行常见无害日常 flag（--quiet/--no-quiet/--auto/--aggressive/
    --prune=now/--prune=never）——这些 flag 存在时仍触发池 GC，并按需转发给池内
    repack/prune；只有出现白名单之外、会真正改变行为的参数（如 --prune=<日期>、
    --keep-largest-pack 等）才 bypass 池 GC、直接透传原生 git。
    """
    if subcmd_args and not all(a in POOL_GC_WHITELIST for a in subcmd_args):
        return _exec_passthrough(global_opts + ['gc'] + subcmd_args)
    if POOL_PATH.exists():
        with pool_lock():
            gc_pool_repos(subcmd_args)
    return _exec_passthrough(global_opts + ['gc'] + subcmd_args)


# ---------- migrate ----------
def _fetch_local_to_pool(gitdir: str, pool_path: Path) -> bool:
    """将本地仓库所有对象（含全部 branch / tag）fetch 进池裸仓。

    步骤：
    1) fetch 对象进池：显式写入 refs/object-pool/ 临时命名空间；
    2) fetch 成功后立即将 worktree alternates 指向池对象目录；
    3) finally 清理 refs/object-pool/ 命名空间下所有临时 ref。

    返回 True 表示 fetch 成功（returncode==0），False 表示失败。
    """
    _ensure_pool_dir()
    ok = False
    with pool_lock():
        try:
            # 1) fetch 对象进池：显式写入 refs/object-pool/ 临时命名空间
            # check=False: 迁移属尽力而为，单次 fetch 失败交由调用方据后续 repack 结果判断
            fetch_res = run_git(['--git-dir', str(pool_path), 'fetch',
                                 gitdir,
                                 'refs/heads/*:refs/object-pool/heads/*',
                                 'refs/tags/*:refs/object-pool/tags/*',
                                 'refs/remotes/*:refs/object-pool/remotes/*',
                                 'HEAD:refs/object-pool/HEAD'],
                                capture=True, check=False)
            if fetch_res.returncode != 0:
                stderr = fetch_res.stderr.strip() if fetch_res.stderr else ''
                print(f'[Wrapper] warning: fetch 本地对象进池失败（exit {fetch_res.returncode}）: {stderr}',
                      file=sys.stderr)
            else:
                ok = True
                replace_alternates(gitdir, pool_path)
        finally:
            # 3) 清理 refs/object-pool/ 命名空间下所有临时 ref。
            cleanup_object_pool_refs(pool_path)
    return ok


@contextmanager
def _missing_worktree_stripped(gitdir: str):
    """上下文管理器：临时移除指向缺失路径的 core.worktree 行，退出时原位写回。

    孤立 gitdir（orphan module）的 core.worktree 可能指向不存在的路径。
    git 在初始化阶段（任何子命令执行之前）就会 chdir 到 core.worktree，
    路径不存在时连 `git config --local` 都会立即崩溃（exit 128）。
    因此不能用 run_git 读写该值，必须直接编辑 <gitdir>/config 文件。
    """
    config_path = os.path.join(gitdir, 'config')
    backup: Optional[Tuple[int, str]] = None  # (行号, 原始行内容)
    try:
        with open(config_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        lines = None
    if lines is not None:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.lower().startswith('worktree'):
                continue
            if '=' not in stripped:
                continue
            wt_val = stripped.split('=', 1)[1].strip()
            if os.path.isabs(wt_val):
                wt_abs = wt_val
            else:
                wt_abs = os.path.normpath(os.path.join(gitdir, wt_val))
            if not os.path.isdir(wt_abs):
                backup = (i, lines[i])
                lines[i] = ''  # 临时清空该行（保留行号，restore 时原位写回）
                try:
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                except OSError:
                    backup = None  # 写入失败，放弃恢复
            break  # core section 里只会有一个 worktree 键
    try:
        yield
    finally:
        if backup is not None:
            try:
                with open(config_path, 'r', encoding='utf-8', errors='replace') as f:
                    cur = f.readlines()
                idx, original = backup
                # 确保行数未发生变化（迁移过程不应修改 config 行数）
                if idx < len(cur):
                    cur[idx] = original
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.writelines(cur)
            except OSError:
                pass


def _repack_local_with_dangling_recovery(gitdir: str) -> bool:
    """对 gitdir 执行 repack -a -d --local；撞到 dangling ref（bad object）时
    批量清理后只重试一次。

    返回 True 表示迁移可视为成功（repack 成功，或 bad-object 清理后即便重试仍失败但
    不影响仓库功能）；返回 False 表示遇到不可恢复的 repack 失败。
    """
    print(f'[Wrapper]   repack --local ...', file=sys.stderr)
    # check=False: repack 失败属可恢复（对象已在池中），据 returncode 报错并返回 False
    res = run_git(['--git-dir', gitdir, 'repack', '-a',
                  '-d', '--local'], capture=True, check=False)
    if res.returncode == 0:
        return True
    stderr = res.stderr.strip() if res.stderr else ''
    if 'bad object' not in stderr and 'bad tree object' not in stderr:
        print(f'[Wrapper]   repack 失败: {stderr}', file=sys.stderr)
        return False
    # repack 撞到 dangling ref（remote-tracking ref 指向池中已不存在的对象）。
    # 旧方案逐个从 stderr 解析 refname 删除并循环重试，repo 悬空 ref 多时（10+）
    # 需重试多轮，每轮一次 repack 极慢。
    # 现方案：一次性扫描所有 remote-tracking ref，用 cat-file -e 判断对象是否存在，
    # 批量删除全部悬空 ref，然后只重试一次 repack（不依赖网络）。
    # 仅当 for-each-ref 失败或没找到任何悬空 ref 时，才降级回退到 remote prune。
    print(f'[Wrapper]   warning: repack 失败（bad object），扫描并批量清理 dangling ref: '
          f'{stderr}', file=sys.stderr)
    dangling = []
    # 扫描范围：refs/ 下的全部 ref（含 heads/、tags/、remotes/）。
    # orphan gitdir 的 heads/tags 同样可能指向对象不在本地也不在池中的 sha，
    # 仅扫 remotes/ 会遗漏导致重试 repack 仍失败。
    fer = run_git(['--git-dir', gitdir, 'for-each-ref', 'refs/',
                   '--format=%(refname) %(objectname)'], capture=True, check=False)
    if fer.returncode == 0:
        for line in fer.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            refname, sha = parts[0], parts[1]
            chk = run_git(['--git-dir', gitdir, 'cat-file', '-e', sha],
                          capture=True, check=False)
            if chk.returncode != 0:
                dangling.append(refname)
    if dangling:
        for refname in dangling:
            del_res = run_git(['--git-dir', gitdir, 'update-ref', '-d', refname],
                              capture=True, check=False)
            if del_res.returncode == 0:
                print(
                    f'[Wrapper]   已删除悬空 ref: {refname}',
                    file=sys.stderr)
            else:
                print(f'[Wrapper]   warning: update-ref -d {refname} 失败: '
                      f'{(del_res.stderr or "").strip()}', file=sys.stderr)
    else:
        # 降级：for-each-ref 失败或未发现悬空 ref 时，回退到 remote
        # prune（依赖网络，尽力而为）
        remotes_res = run_git(
            ['--git-dir', gitdir, 'remote'], capture=True, check=False)
        if remotes_res.returncode == 0:
            for remote in remotes_res.stdout.splitlines():
                remote = remote.strip()
                if not remote:
                    continue
                run_git(['--git-dir', gitdir, 'remote', 'prune', remote],
                        capture=True, check=False)
    # 清理完成后只重试一次 repack
    retry = run_git(['--git-dir', gitdir, 'repack', '-a', '-d', '--local'],
                    capture=True, check=False)
    if retry.returncode != 0:
        # 重试仍失败：alternates 与 shell 注册均已完成，仅本地存储未压缩，
        # 不影响仓库功能，按成功返回。
        print(f'[Wrapper]   warning: 批量清理后 repack 仍失败，'
              f'本地存储未压缩但不影响功能: '
              f'{(retry.stderr or "").strip()}', file=sys.stderr)
    return True


def _migrate_gitdir(gitdir: str, label: str) -> bool:
    """对一个已知 gitdir 执行迁移核心逻辑（供父仓和 submodule 共用）。

    不再依赖 URL：固定把本仓库所有对象 fetch 进全局唯一池（``~/.git-pool/pool.git``）。
    label 用于日志前缀，例如 repo_path 或 "submodule <name>"。
    返回 True 表示成功（含幂等跳过），False 表示失败。
    """
    print(f'[Wrapper] migrate: {label}', file=sys.stderr)
    print(f'[Wrapper]   pool   : {POOL_PATH}', file=sys.stderr)

    # core.worktree 指向缺失路径时临时移除该行（迁移完成后由上下文管理器恢复）。
    with _missing_worktree_stripped(gitdir):
        _ensure_pool_dir()
        with pool_lock():
            # 检查是否已有 alternates 指向全局池（幂等）
            existing = read_alternates(gitdir)
            target_objects = os.path.abspath(str(POOL_PATH / 'objects'))
            if target_objects in existing:
                print(f'[Wrapper]   已在池中，重新 fetch 并 repack', file=sys.stderr)

            # 池不存在则就地创建裸仓（不再按 URL ensure_pool_bare_repo）
            if not POOL_PATH.exists():
                print(f'[Wrapper]   初始化全局对象池: {POOL_PATH}', file=sys.stderr)
                run_git(['init', '--bare', str(POOL_PATH)])

            # 将本地所有对象（含其他 remote fetch 来的）fetch 进池，最大化收缩本地
            print(f'[Wrapper]   fetch 本地对象进池 ...', file=sys.stderr)
            ok = _fetch_local_to_pool(gitdir, POOL_PATH)
            if not ok:
                # 特殊情形：fetch 因 "not our ref" 失败，说明该 gitdir 已有 alternates 指向
                # pool，本地 ref 指向的对象实际在 pool 里，upload-pack 不代理 alternates
                # 对象，因此拒绝提供。此时对象已在池中，只需补写 alternates 并继续 repack。
                target_objects = os.path.abspath(str(POOL_PATH / 'objects'))
                already_in_pool = target_objects in read_alternates(gitdir)
                # 从 _fetch_local_to_pool 内部已打印 stderr，此处重新取一份用于判断
                # （通过再次 fetch 开销太大，改为直接检查 alternates 状态）
                if already_in_pool:
                    print(f'[Wrapper]   对象已在池中（alternates 已设），跳过 fetch 直接 repack',
                          file=sys.stderr)
                else:
                    print(f'[Wrapper]   warning: fetch 本地对象进池失败，跳过 alternates/register/repack',
                          file=sys.stderr)
                    return False

            replace_alternates(gitdir, POOL_PATH)
            register_shell(gitdir)

            # repack 前先清理 reflog：reflog 可能引用已不在本地（已迁入池/已 prune）的对象，
            # 导致 repack 报 "bad tree object" / "bad object" 并中止。
            # expire --expire=now 仅清理已过期条目，all 覆盖所有 ref，不影响未过期 reflog。
            run_git(['--git-dir', gitdir, 'reflog', 'expire', '--expire=now', '--all'],
                    capture=True, check=False)

            if not _repack_local_with_dangling_recovery(gitdir):
                return False

            print(f'[Wrapper]   ✓ 迁移完成', file=sys.stderr)
            return True


def _is_git_dir(path: str) -> bool:
    """用 git rev-parse 健壮判定 path 是否为有效 gitdir。

    `git --git-dir=<path> rev-parse --git-dir` 要求 HEAD / objects / refs 齐备才返回 0，
    比「存在名为 HEAD 的文件 + objects/ 目录」这类弱校验可靠（能正确排除 logs/HEAD、
    refs/remotes/*/HEAD 等同名文件所在目录）。
    """
    # check=False: 非 gitdir 时 rev-parse 返回非零属正常分支
    res = run_git(['--git-dir', path, 'rev-parse', '--git-dir'],
                  capture=True, check=False)
    return res.returncode == 0


def _scan_orphan_module_gitdirs(modules_dir: Path) -> List[Path]:
    """枚举 .git/modules/ 下所有有效 gitdir（含嵌套 submodule 的 gitdir）。

    取代原先的 rglob('HEAD')：后者把任意名为 HEAD 的文件（logs/HEAD、
    refs/remotes/*/HEAD 等）误当候选，再靠「objects/ 是否存在」做弱过滤。这里改用
    os.walk + _is_git_dir（git rev-parse）做健壮判定：命中一个 gitdir 后，其内部仅
    modules/ 子目录可能藏有嵌套 submodule 的 gitdir，故只保留 modules/ 继续下探、
    其余内部目录（objects/refs/logs/hooks…）剪枝，既不漏嵌套也不误判。

    返回按路径排序的候选列表，保证遍历顺序确定（与原 sorted(rglob) 一致的确定性）。
    """
    found: List[Path] = []
    for root, dirs, _files in os.walk(modules_dir):
        dirs.sort()  # 确定性遍历顺序
        if _is_git_dir(root):
            found.append(Path(root))
            # 命中 gitdir：仅 modules/ 下可能有嵌套 gitdir，其余内部目录剪枝
            dirs[:] = ['modules'] if 'modules' in dirs else []
    return found


def migrate_one(repo_path: str, recursive: bool = False) -> bool:
    """将一个已有仓库迁移进对象池。

    流程：
    1. 将本仓库所有对象 fetch 进全局唯一池（``~/.git-pool/pool.git``）
    2. 写 alternates + repack --local
    3. 注册到 registry
    若 recursive=True，递归处理所有 submodule（gitdir 位于 .git/modules/<name>）。

    返回 True 表示成功，False 表示跳过或失败。
    """
    repo_path = os.path.abspath(repo_path)
    gitdir = find_gitdir(repo_path)
    if not gitdir:
        print(f'[Wrapper] migrate: {repo_path} 不是 git 仓库，跳过', file=sys.stderr)
        return False

    ok = _migrate_gitdir(gitdir, repo_path)

    if recursive:
        # worktree_root：find_gitdir 可能返回 .git（普通仓库），worktree 就是其父目录
        worktree = str(Path(gitdir).parent) if Path(
            gitdir).name == '.git' else repo_path

        migrated_gitdirs: set = set()

        def _migrate_recursive(cur_worktree: str, depth_label: str) -> bool:
            sub_ok_all = True
            cur_subs = parse_gitmodules(cur_worktree)
            for s in cur_subs:
                s_name = s['name']
                s_worktree = os.path.join(cur_worktree, s['path'])
                if not os.path.isdir(s_worktree):
                    print(
                        f'[Wrapper] migrate: submodule {s_name} worktree 不存在（未 init），跳过',
                        file=sys.stderr)
                    continue
                # 只检查 s_worktree/.git 本身，不向上爬父目录（find_gitdir 会向上爬，
                # 对于未初始化的空 submodule 目录会错误返回父仓的 gitdir）。
                dot_git = os.path.join(s_worktree, '.git')
                if not os.path.exists(dot_git):
                    print(
                        f'[Wrapper] migrate: submodule {s_name} 未初始化，跳过',
                        file=sys.stderr)
                    continue
                s_gitdir = resolve_gitdir(dot_git)
                if not s_gitdir:
                    print(
                        f'[Wrapper] migrate: submodule {s_name} gitdir 解析失败，跳过',
                        file=sys.stderr)
                    continue
                s_gitdir_abs = os.path.abspath(s_gitdir)
                migrated_gitdirs.add(s_gitdir_abs)
                label = f'submodule {s_name}' if not depth_label else f'{depth_label} / submodule {s_name}'
                if not _migrate_gitdir(s_gitdir, label):
                    sub_ok_all = False
                # 嵌套继续
                if not _migrate_recursive(s_worktree, label):
                    sub_ok_all = False
            return sub_ok_all

        if not _migrate_recursive(worktree, ''):
            ok = False

        # 扫描 .git/modules/ 下所有 gitdir，补充 migrate 未被 .gitmodules 路径覆盖的孤立 gitdir
        modules_dir = Path(gitdir) / 'modules'
        if modules_dir.is_dir():
            for candidate in _scan_orphan_module_gitdirs(modules_dir):
                candidate_abs = str(candidate.resolve())
                if candidate_abs in migrated_gitdirs:
                    continue
                migrated_gitdirs.add(candidate_abs)
                label = f'orphan module {candidate.relative_to(modules_dir)}'
                if not _migrate_gitdir(candidate_abs, label):
                    ok = False

    return ok


def do_migrate(global_opts: List[str], args: List[str]):
    """git migrate [--recursive|-r]

    将当前仓库迁移进对象池（在仓库目录内执行，无需传路径）。
    默认递归迁移所有已 init 的 submodule；--no-recursive 禁用递归。

    解析复用 parse_git_option_values 单趟取值；参数错误抛 GitCommandError，由 main 统一
    打印并退出，不在此处裸用 sys.exit。
    """
    values, positionals = parse_git_option_values(args, set())
    recursive = True
    for name, val in values.items():
        if name in ('--recursive', '-r', '--no-recursive') and val is True:
            recursive = name != '--no-recursive'
        else:
            raise GitCommandError(f'migrate: 未知参数 {name!r}', returncode=2)
    if positionals:
        raise GitCommandError(
            'migrate 不接受路径参数，请使用 `git -C <path> migrate`',
            returncode=2)

    migrate_one(effective_cwd(global_opts), recursive=recursive)


# ---------- main ----------
def main():
    if len(sys.argv) < 2:
        os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])

    parsed = parse_global_options(sys.argv)
    # parsed 为 None 表示未识别全局选项 / 无 subcmd，把原始 argv 原样交给原生 git。
    # execvp 的进程替换决策集中在 main，解析函数本身不再产生该副作用。
    if parsed is None:
        os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])
    global_opts, subcmd, subcmd_args = parsed

    try:
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
            do_gc_command(global_opts, subcmd_args)
            return
        if subcmd == 'migrate':
            do_migrate(global_opts, subcmd_args)
            return
    except GitCommandError as e:
        # library 层不再 sys.exit，git 失败 / 参数错误统一在此打印并以 1 退出。
        if e.stderr:
            print(
                e.stderr,
                file=sys.stderr,
                end='' if e.stderr.endswith('\n') else '\n')
        else:
            print(f'[Wrapper] {e}', file=sys.stderr)
        sys.exit(1)

    os.execvp(SYSTEM_GIT, [SYSTEM_GIT] + sys.argv[1:])


if __name__ == '__main__':
    if SYSTEM_GIT is None:
        print('错误：找不到系统 Git 命令', file=sys.stderr)
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        print('\n[git-pool] 操作已取消', file=sys.stderr)
        sys.exit(130)
