# Git Object Pool Wrapper

一个用于复用 Git 对象存储的 Python wrapper。它通过拦截常见 Git 操作，在本机维护全局裸仓对象池，让多个 worktree、clone 结果和 submodule 共享同一批 Git objects，降低重复下载与磁盘占用。

## 背景

在频繁 clone 大型仓库、递归初始化 submodule、或维护多个相近工作区时，普通 Git 工作流会反复下载和存放相同对象。`git-pool-wrapper.py` 的目标是在不改变日常 Git 使用习惯的前提下，自动把可共享的对象集中到全局池中，并让各工作区通过 `alternates` 或 `--reference` 复用这些对象。

## 核心能力

- 拦截 `git clone`，自动先创建或更新池中的 bare repository，再使用 `--reference=<pool repo>` clone 工作区。
- 拦截 `git fetch`，基于当前仓库 `remote.origin.url` 预热对象池。
- 拦截 `git submodule update --init`，自驱初始化 submodule，并支持递归 submodule。
- 支持 submodule 相对 URL 解析，统一映射到对象池路径。
- 支持同层 submodule 并发处理，兼容 `--jobs`。
- 支持 `--depth`、`--filter` 等常见 submodule update 参数。
- 维护已注册 worktree / gitdir 列表，用于后续池 GC 依赖分析。
- 拦截 `git gc`，在执行原生 GC 前先维护对象池。
- 对本地路径、`file://`、已有 `--reference`、`--shared`、`--bare`、`--mirror` 等场景自动透传原生 Git。

## 主流系统适用性

| 系统 | 适用性 | 说明 |
|---|---|---|
| Linux | 推荐使用 | 脚本依赖 Python 3、系统 Git、POSIX 路径语义和 `fcntl.flock` 文件锁。主流 Linux 发行版通常都满足这些条件。 |
| macOS | 推荐使用 | macOS 自带 `/usr/bin/git` 与 Python 3 环境可满足基本运行需求；如通过 Homebrew 安装 Git，可能需要把脚本中的 `SYSTEM_GIT` 调整为实际路径。 |
| Windows 原生环境 | 不直接支持 | 脚本使用 `fcntl`、Unix 风格路径、`os.execve`/`os.execvp` 以及 Git alternates 路径写法，不能直接在 CMD / PowerShell 原生 Python 环境中稳定运行。 |
| Windows WSL | 可使用 | 在 WSL 内部按 Linux 环境使用即可。建议对象池和工作区都放在 WSL 文件系统内，避免跨 `/mnt/c` 带来的路径、权限和性能问题。 |
| Git Bash / MSYS2 / Cygwin | 不建议作为正式环境 | 这类环境提供部分 POSIX 兼容层，但文件锁、路径转换、系统 Git 路径和 alternates 行为可能存在差异，需要额外验证。 |
| 容器 / CI | 可使用 | 适合在 Linux 容器内使用。需要确保对象池目录挂载为持久卷，否则容器销毁后对象池也会丢失。 |

### 依赖条件

- Python 3。
- Git 命令行工具。
- 类 Unix 文件系统与 POSIX 路径语义。
- `fcntl.flock` 文件锁支持。
- Git alternates 支持。
- 脚本中的 `SYSTEM_GIT` 指向真实 Git 可执行文件。

### 平台注意事项

- macOS / Linux 是主要目标环境。
- Windows 建议通过 WSL 使用，而不是直接在 Windows 原生 shell 中使用。
- 如果系统 Git 不在 `/usr/bin/git`，需要修改脚本中的 `SYSTEM_GIT`。
- 如果多个终端或 CI job 共享对象池，应确保 `GIT_POOL` 指向同一个本地文件系统路径。
- 不建议把对象池放在网络文件系统上，文件锁、rename、权限和 Git 对象访问性能都可能受影响。

## 工作原理

默认对象池目录为：

```bash
~/.git-pool
```

也可以通过环境变量覆盖：

```bash
export GIT_POOL=/path/to/git-pool
```

wrapper 会将远端 URL 归一化为池内路径，例如：

```text
https://host/storage/foo.git    -> ~/.git-pool/host/storage/foo
git@host:storage/foo.git        -> ~/.git-pool/host/storage/foo
ssh://git@host/storage/foo.git  -> ~/.git-pool/host/storage/foo
```

首次遇到某个远端仓库时，wrapper 会执行 bare clone 到池中；后续 clone / fetch / submodule 初始化会复用并更新这个 bare repository。

## 安装

将脚本放到任意固定路径，例如：

```bash
mkdir -p ~/bin
cp git-pool-wrapper.py ~/bin/git
chmod +x ~/bin/git
```

确保 `~/bin` 位于 `PATH` 中，并且优先级高于系统 Git：

```bash
export PATH="$HOME/bin:$PATH"
```

验证当前命中的 Git：

```bash
which git
```

脚本内部默认调用系统 Git：

```text
/usr/bin/git
```

如你的系统 Git 不在该路径，需要修改脚本中的 `SYSTEM_GIT`。

## 使用方式

安装后继续按普通 Git 命令使用即可。

### Clone

```bash
git clone git@github.com:owner/repo.git
```

首次 clone 会先将 bare repository 放入对象池，再通过 `--reference` 创建工作区。

### Fetch

```bash
git fetch
```

在普通仓库中执行 fetch 时，wrapper 会根据 `remote.origin.url` 更新对应对象池。

### Submodule

```bash
git submodule update --init --recursive --jobs 8
```

wrapper 会解析 `.gitmodules`，为每个 submodule 预热对象池，并让 submodule gitdir 通过 alternates 引用池对象。

### GC

```bash
git gc
```

wrapper 会先执行对象池维护，再透传执行原生 `git gc`。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GIT_POOL` | `~/.git-pool` | 全局 Git 对象池目录 |

## 对象池结构

对象池中主要包含：

| 路径 | 说明 |
|---|---|
| `.lock` | 进程间文件锁，避免并发更新池仓库时冲突 |
| `.registered_shells` | 已注册 worktree / gitdir 列表，用于 GC 依赖分析 |
| `<host>/<group>/<repo>` | 归一化 URL 对应的 bare repository |

## 透传规则

以下场景不会进入对象池逻辑，会直接调用原生 Git：

- 本地路径 clone，例如 `/path/to/repo`、`./repo`、`../repo`。
- `file://` URL。
- `git clone` 已显式使用 `--reference` 或 `--reference-if-able`。
- `git clone --shared`。
- `git clone --bare`。
- `git clone --mirror`。
- 非 `submodule update --init` 的其他 submodule 子命令。
- 未被 wrapper 识别的其他 Git 子命令。

## 注意事项

- wrapper 依赖 Unix 文件锁，适用于 macOS / Linux 等类 Unix 环境。
- wrapper 使用 Git alternates 共享对象；不要手动删除对象池中仍被工作区引用的 bare repository。
- 如果对象池损坏，脚本会在部分路径中尝试删除并重新 clone 对应池仓库。
- `git gc` 的对象池维护依赖 `.registered_shells`，如果工作区被手动移动或删除，registry 会在清理时过滤失效路径。
- 该脚本当前定位为个人本机 Git 加速与节省磁盘空间工具，不建议直接作为多人共享服务端组件使用。

## 常见问题

### 如何禁用对象池？

临时绕过 wrapper，直接调用系统 Git：

```bash
/usr/bin/git clone git@github.com:owner/repo.git
```

或调整 `PATH`，让系统 Git 优先于 wrapper。

### 如何换一个对象池目录？

```bash
export GIT_POOL=/data/git-pool
```

建议在 shell profile 中固定该变量，避免不同终端使用不同对象池。

### 对象池能否删除？

可以删除，但所有依赖该池 objects 的工作区可能需要重新 fetch 或重新 clone。删除前建议确认没有重要工作区仍依赖该对象池。

## 文件

- `git-pool-wrapper.py`：主脚本，作为 `git` wrapper 使用。
