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

### 快速安装（一行命令）

复制对应行在终端执行一次即可完成 clone + 配置 + 生效：

**Bash**：

```bash
git clone git@github.com:mm304321141/git-object-pool.git ~/git-object-pool && printf '\n# git object pool wrapper\nfunction git() { python3 ~/git-object-pool/git-pool-wrapper.py "$@"; }\nsource ~/git-object-pool/git-pool-completion.bash\n' >> ~/.bash_profile && source ~/.bash_profile
```

**Zsh**：

```zsh
git clone git@github.com:mm304321141/git-object-pool.git ~/git-object-pool && printf '\n# git object pool wrapper\nfunction git() { python3 ~/git-object-pool/git-pool-wrapper.py "$@"; }\nsource ~/git-object-pool/git-pool-completion.zsh\n' >> ~/.zshrc && source ~/.zshrc
```

### 卸载

**Bash**：

```bash
sed -i '' '/# git object pool wrapper/d; /git-pool-wrapper\.py/d; /git-pool-completion\.bash/d' ~/.bash_profile && source ~/.bash_profile
```

**Zsh**：

```zsh
sed -i '' '/# git object pool wrapper/d; /git-pool-wrapper\.py/d; /git-pool-completion\.zsh/d' ~/.zshrc && source ~/.zshrc
```

> 以上仅移除 profile 中的配置行，不删除 `~/git-object-pool` 目录。
> 若要连目录一起删除：`rm -rf ~/git-object-pool`。
> ⚠️ 删除目录前请确认本机没有仓库通过 `alternates` 依赖对象池（`~/.git-pool`），否则相关仓库会因缺失对象损坏。

---

### 方式一：可执行文件（推荐）

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

### 方式二：Shell Function

不需要修改 `PATH`，直接在 shell profile 中定义一个名为 `git` 的函数覆盖系统命令：

**Bash**（在 `~/.bash_profile` 或 `~/.bashrc` 中添加）：

```bash
# git object pool wrapper
function git() {
    python3 ~/Work/git-object-pool/git-pool-wrapper.py "$@"
}
```

**Zsh**（在 `~/.zshrc` 中添加）：

```zsh
# git object pool wrapper
function git() {
    python3 ~/Work/git-object-pool/git-pool-wrapper.py "$@"
}
```

添加后执行 `source ~/.bash_profile` 或 `source ~/.zshrc` 生效。

### Shell 补全（可选）

仓库提供了 bash 和 zsh 两套补全脚本，安装后可以在 `git migrate` 时通过 Tab 键补全选项和 remote 名。

**Bash**（在 `~/.bash_profile` 或 `~/.bashrc` 中添加）：

```bash
source ~/Work/git-object-pool/git-pool-completion.bash
```

**Zsh**（在 `~/.zshrc` 中添加）：

```zsh
source ~/Work/git-object-pool/git-pool-completion.zsh
```

添加后重新打开终端或执行 `source ~/.bash_profile` / `source ~/.zshrc` 即可生效。

> **注意（Shell Function 安装方式）**：如果使用方式二（shell function），bash 有时不会自动将补全函数绑定到同名 function，需要在补全脚本 source 之后手动绑定：
> 
> ```bash
> source ~/Work/git-object-pool/git-pool-completion.bash
> complete -F _git git
> ```
> 
> Zsh 通常不需要额外绑定。

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

### Migrate

将一个已存在的普通仓库迁移为对象池化仓库，把历史对象从本地 `.git` 移到共享池（默认 `~/.git-pool`）：

```bash
# 在仓库目录内执行（无需传路径），使用 remote.origin.url 作为池来源
git migrate

# 指定 remote 名（仓库没有 origin 或想用其他 remote 时）
git migrate --remote gitlab

# 同时迁移所有已 init 的 submodule
git migrate -r
git migrate --recursive

# 指定 remote + 递归
git migrate -r --remote gitlab
```

迁移流程概览：

1. 根据指定 remote（默认 `origin`）的 URL 在对象池中创建或更新对应 bare repository。
2. 将本仓库本地所有对象（含通过其他 remote fetch 来的）fetch 进池，最大化后续压缩空间。
3. 通过 alternates 让本仓库引用池对象，再执行 `git repack --local` 收缩本地存储。
4. `--recursive` 时会对已 init 的 submodule（含嵌套 submodule）重复以上步骤。

注意事项：

- 迁移后仓库依赖 `~/.git-pool`（或 `GIT_POOL` 指向的路径），删除或损坏对象池会让仓库无法读取历史对象。
- 仓库无 `origin` 且未指定 `--remote` 时，命令会报错退出，需显式指定 remote 名。
- 仅对已经存在的本地仓库生效；对未 clone 的仓库请使用 `git clone`，wrapper 会在 clone 阶段自动池化。
- 迁移操作会修改本仓库的 `objects/info/alternates`，建议在工作区干净时执行。

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
- `git-pool-completion.bash`：Bash 补全脚本，source 到 `~/.bash_profile` 后生效。
- `git-pool-completion.zsh`：Zsh 补全脚本，source 到 `~/.zshrc` 后生效。
