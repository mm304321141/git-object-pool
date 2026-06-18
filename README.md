# Git Object Pool Wrapper

> ⚠️ **免责声明**：本工具按现状（AS IS）提供，不作任何明示或暗示的担保。使用本工具的风险由用户自行承担。
> 如遇问题或有改进建议，欢迎提 [Issue](https://github.com/mm304321141/git-object-pool/issues) 或 PR。
>
> **Windows 原生环境**（CMD / PowerShell）目前**未经作者实际测试**，如遇 Bug 欢迎反馈。

一个用于复用 Git 对象存储的 Python wrapper。它通过拦截常见 Git 操作，在本机维护一个全局唯一的裸仓对象池（`~/.git-pool/pool.git`），让多个 worktree、clone 结果和 submodule 共享同一批 Git objects，降低重复下载与磁盘占用。

## 背景

你在使用 Git 时是否经常遇到这样的问题：

- 需要克隆多份仓库以并行开发或维护不同特性分支，相同对象被反复下载并重复占用磁盘空间。
- 仓库依赖树复杂，存在菱形依赖；多个上游仓库共享同一底层依赖仓库作为 submodule，导致相同对象在本地重复存储。
- 克隆了同一项目的不同 fork 仓库，这些仓库来自不同团队或个人，对象大量重叠却各自独立存放。
- 从不同源克隆了同一项目的仓库，虽然 remote URL 不同但内容高度重叠，仍会耗费大量磁盘空间与下载时间。

`git-pool-wrapper.py` 或许可以解决你的困扰：它通过维护全局唯一对象池，并结合 Git alternates 与 `--reference` 机制，让多个工作区复用同一批对象，从而降低重复下载与磁盘占用。

## 核心能力

- 拦截 `git clone`，自动先创建或更新全局唯一对象池，再使用 `--reference=<pool repo>` clone 工作区。
- 拦截 `git fetch`，先执行原生 fetch；若当前仓库已迁移进池（alternates 指向全局池）则把新对象搬入池并 `repack`，否则按当前分支 tracking remote（回退 `origin`）的 URL 预热全局池。
- 拦截 `git submodule update --init`，自驱初始化 submodule，并支持递归 submodule。
- 支持 submodule 相对 URL 解析，统一映射到对象池路径。
- 支持同层 submodule 并发处理，兼容 `--jobs`。
- 支持 `--depth`、`--filter` 等常见 submodule update 参数。
- 维护已注册 worktree / gitdir 列表，用于后续池 GC 依赖分析。
- 拦截 `git gc`，在执行原生 GC 前先维护对象池：先汇总所有依赖 worktree 的可达 commit 集合，并在池内为这些 commit 建立 `refs/object-pool/<sha>` 临时保护 ref，然后对对象池执行 `prune --expire=now` 与 `repack -Ad`，prune 完成后再清理这些临时 ref，避免误删 worktree 仍依赖的对象；设有 1 小时频率限制，避免重复触发全池维护。
- 对本地路径、`file://`、已有 `--reference`、`--shared`、`--bare`、`--mirror` 等场景自动透传原生 Git。

## 主流系统适用性

| 系统 | 适用性 | 说明 |
|---|---|---|
| Linux | 推荐使用 | 脚本依赖 Python 3、系统 Git、POSIX 路径语义和 `fcntl.flock` 文件锁。主流 Linux 发行版通常都满足这些条件。 |
| macOS | 推荐使用 | macOS 自带 `/usr/bin/git` 与 Python 3 环境可满足基本运行需求；如通过 Homebrew 安装 Git，可能需要把脚本中的 `SYSTEM_GIT` 调整为实际路径。 |
| Windows 原生环境 | 可使用（有限制） | 脚本已适配 `fcntl` → `msvcrt` 文件锁和 `os.execvp` → `subprocess` 进程替换，在 Python 3 + Git for Windows（提供 `git.exe`）环境下可运行。路径分隔符由 Python `pathlib` 处理，alternates 文件路径会使用 Windows 风格；需要通过 `SYSTEM_GIT` 环境变量或系统 PATH 指向 `git.exe`。CMD / PowerShell 下 shell function 注入方式与 Unix 不同，需手动配置调用方式（参见下方说明）。 |
| Windows WSL | 可使用 | 在 WSL 内部按 Linux 环境使用即可。建议对象池和工作区都放在 WSL 文件系统内，避免跨 `/mnt/c` 带来的路径、权限和性能问题。 |
| Git Bash / MSYS2 / Cygwin | 可使用 | 这类环境提供 POSIX 兼容层，Python 的 `fcntl` 通常可用（Git Bash 随 MSYS2 的 Python 提供）。建议通过 shell function 方式注入，路径使用 Unix 风格。 |
| 容器 / CI | 可使用 | 适合在 Linux 容器内使用。需要确保对象池目录挂载为持久卷，否则容器销毁后对象池也会丢失。 |

### 依赖条件

- Python 3。
- Git 命令行工具。
- 文件系统路径支持（Unix/macOS 使用 POSIX 路径语义；Windows 使用 `pathlib` 跨平台路径处理）。
- 文件锁支持（Unix/macOS 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`）。
- Git alternates 支持。

### 平台注意事项

- macOS / Linux 是主要目标环境。
- 脚本优先从 `SYSTEM_GIT` 环境变量读取系统 Git 路径；若未设置，自动通过 `PATH` 查找；如均不可用，回退到 `/usr/bin/git`。Windows 用户建议通过 `SYSTEM_GIT` 环境变量明确指定 `git.exe` 路径（如 `C:\Program Files\Git\bin\git.exe`）。
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

wrapper 维护一个全局唯一的对象池：

```text
~/.git-pool/pool.git
```

不同远端 URL 不再各自分池，而是作为该唯一池的不同 remote 共存（remote 名由 URL 归一化生成）。首次遇到某个远端仓库时，wrapper 会初始化该池（`git init --bare`）并把远端登记为 remote 后只 fetch 该 remote；后续 clone / fetch / submodule 初始化会复用并更新这个唯一池。

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

**Windows CMD**：

```bat
mkdir "%USERPROFILE%\git-object-pool" 2>nul & curl -L -o "%USERPROFILE%\git-object-pool\git-pool-wrapper.py" "https://raw.githubusercontent.com/mm304321141/git-object-pool/main/git-pool-wrapper.py" & curl -L -o "%USERPROFILE%\git-object-pool\git-pool-completion.lua" "https://raw.githubusercontent.com/mm304321141/git-object-pool/main/git-pool-completion.lua" & (echo @python "%USERPROFILE%\git-object-pool\git-pool-wrapper.py" %%*) > "%USERPROFILE%\git-object-pool\git.cmd" & for /f "delims=" %%G in ('where /r "C:\Program Files\Git" git.exe 2^>nul') do (setx SYSTEM_GIT "%%G" & goto :sysgit_done) & echo Could not detect git.exe automatically. Please run: setx SYSTEM_GIT "C:\Program Files\Git\bin\git.exe" & :sysgit_done & if exist "%LocalAppData%\clink\" (copy /y "%USERPROFILE%\git-object-pool\git-pool-completion.lua" "%LocalAppData%\clink\" & echo Clink completion installed.) else (echo Clink not found. For tab completion in CMD, install Clink ^(https://github.com/chrisant996/clink^) then copy "%USERPROFILE%\git-object-pool\git-pool-completion.lua" to "%LocalAppData%\clink\") & echo Add "%USERPROFILE%\git-object-pool" to the front of PATH (before Git install dir).
```

**PowerShell**：

```powershell
$dir="$env:USERPROFILE\git-object-pool"; New-Item -ItemType Directory -Force $dir | Out-Null; iwr -UseBasicParsing "https://raw.githubusercontent.com/mm304321141/git-object-pool/main/git-pool-wrapper.py" -OutFile "$dir\git-pool-wrapper.py"; iwr -UseBasicParsing "https://raw.githubusercontent.com/mm304321141/git-object-pool/main/git-pool-completion.ps1" -OutFile "$dir\git-pool-completion.ps1"; Set-Content -Encoding ASCII "$dir\git.ps1" "& python `"$dir\git-pool-wrapper.py`" @args; exit `$LASTEXITCODE"; $realGit = (Get-Command git -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*git-object-pool*" } | Select-Object -First 1).Source; if ($realGit) { [Environment]::SetEnvironmentVariable('SYSTEM_GIT', $realGit, 'User'); Write-Host "SYSTEM_GIT set to $realGit" } else { Write-Host "Could not detect git.exe automatically. Please run: [Environment]::SetEnvironmentVariable('SYSTEM_GIT','C:\Program Files\Git\bin\git.exe','User')" }; if (-not (Test-Path $PROFILE)) { New-Item -Path $PROFILE -ItemType File -Force | Out-Null }; $completionLine = ". `"$dir\git-pool-completion.ps1`""; if (-not (Select-String -Path $PROFILE -Pattern ([regex]::Escape($completionLine)) -Quiet)) { Add-Content -Path $PROFILE -Value "`n$completionLine" }; Write-Host "Completion added to `$PROFILE. Run: . `$PROFILE to activate."; Write-Host "Add $dir to the front of PATH."
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

**Windows CMD**：

```bat
del "%USERPROFILE%\git-object-pool\git.cmd" & del "%USERPROFILE%\git-object-pool\git-pool-wrapper.py" & echo Done. Remember to remove "%USERPROFILE%\git-object-pool" from PATH and unset SYSTEM_GIT if set.
```

**Windows PowerShell**：

```powershell
Remove-Item "$env:USERPROFILE\git-object-pool\git.ps1","$env:USERPROFILE\git-object-pool\git-pool-wrapper.py" -ErrorAction SilentlyContinue; Write-Host "Done. Remember to remove $env:USERPROFILE\git-object-pool from PATH and unset SYSTEM_GIT if set."
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

**Windows CMD**：在 PATH 中某目录（如 `%USERPROFILE%\bin`）下放置 `git.cmd`：

```bat
@python "%USERPROFILE%\bin\git-pool-wrapper.py" %*
```

将 `git-pool-wrapper.py` 复制到 `%USERPROFILE%\bin\git-pool-wrapper.py`，确保 `%USERPROFILE%\bin` 在 PATH 中且优先级高于 Git 安装目录（如 `C:\Program Files\Git\cmd`）。

**Windows PowerShell**：在 PATH 中某目录下放置 `git.ps1`：

```powershell
& python "$env:USERPROFILE\bin\git-pool-wrapper.py" @args; exit $LASTEXITCODE
```

同样确保该目录在 PATH 中优先级最高，并设置 `SYSTEM_GIT`：

```powershell
[Environment]::SetEnvironmentVariable("SYSTEM_GIT", "C:\Program Files\Git\bin\git.exe", "User")
```

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

**Windows CMD**：CMD 不支持 shell function，使用方式同方式一，通过 `git.cmd` shim 实现。

**Windows PowerShell**：在 PowerShell profile（`$PROFILE`）中添加：

```powershell
function git { & python "$env:USERPROFILE\git-object-pool\git-pool-wrapper.py" @args; $global:LASTEXITCODE = $LASTEXITCODE }
```

添加后执行 `. $PROFILE` 生效。

### Shell 补全（可选）

仓库提供了 bash 和 zsh 两套补全脚本，安装后可以在 `git migrate` 时通过 Tab 键补全选项。

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

**Windows CMD（通过 Clink）**：

CMD 原生不支持可编程补全，建议安装 [Clink](https://github.com/chrisant996/clink)。安装 Clink 后，将 `git-pool-completion.lua`（见仓库）放入 Clink scripts 目录：

```bat
copy git-pool-completion.lua "%LocalAppData%\clink\"
```

**Windows PowerShell**：

在 PowerShell profile（`$PROFILE`）中添加：

```powershell
. "$env:USERPROFILE\git-object-pool\git-pool-completion.ps1"
```

添加后执行 `. $PROFILE` 生效。

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

将一个已存在的普通仓库迁移为对象池化仓库，把历史对象从本地 `.git` 移到全局唯一池（默认 `~/.git-pool/pool.git`）：

```bash
# 在仓库目录内执行（无需传路径，也无需指定 remote）
git migrate

# 同时迁移所有已 init 的 submodule
git migrate -r
git migrate --recursive
```

迁移流程概览：

1. 池不存在时初始化全局唯一裸仓（`git init --bare ~/.git-pool/pool.git`）。
2. 将本仓库所有本地分支（`refs/heads/*`）和 tag（`refs/tags/*`）指向的对象 fetch 进池。fetch 过程中会在池内使用临时 `refs/object-pool/` 命名空间中转引用，fetch 完成后自动清理，不会留下永久 ref。
3. 通过 alternates 让本仓库引用池对象，先执行 `git reflog expire --expire=now --all` 清理指向不存在对象的 reflog 条目，再执行 `git repack --local` 收缩本地存储。
4. `--recursive` 时会对已 init 的 submodule（含嵌套 submodule）重复以上步骤；默认递归迁移还会扫描 `.git/modules/` 目录下历史上初始化过、但当前 worktree 已不存在的 orphan gitdir，一并迁移进池以回收磁盘空间。若 orphan gitdir 中的 `core.worktree` 指向不存在路径，脚本会临时屏蔽该配置后执行迁移，完成后再还原。

注意事项：

- 迁移后仓库依赖 `~/.git-pool/pool.git`（或 `GIT_POOL` 指向的路径），删除或损坏对象池会让仓库无法读取历史对象。
- 仅对已经存在的本地仓库生效；对未 clone 的仓库请使用 `git clone`，wrapper 会在 clone 阶段自动池化。
- 迁移操作会修改本仓库的 `objects/info/alternates`，建议在工作区干净时执行。

### GC

```bash
git gc
```

wrapper 会先执行对象池维护，再透传执行原生 `git gc`。

池维护时会枚举所有依赖 worktree 的可达 commit 集合，并在池内为这些 commit 建立 `refs/object-pool/<sha>` 临时保护 ref；随后对对象池依次执行 `prune --expire=now` 与 `repack -Ad`，prune 完成后再清理这些临时 ref，避免 worktree 引用的本地独有对象被误删。

对象池维护设有 1 小时频率限制（记录于 `~/.git-pool/.gc_last_run`）：连续 `git gc` 只触发一次池维护。带显式参数的 `git gc`（如 `git gc --prune=now`）不触发池维护，直接透传。

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
| `pool.git` | 全局唯一对象池（所有远端作为不同 remote 共存于此） |

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
- 如果对象池损坏，脚本会立即将损坏的池归档备份（重命名为 `.bak` 目录），然后重新初始化。
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
- `git-pool-completion.lua`：Clink（Windows CMD）补全脚本，放入 `%LocalAppData%\clink\` 后生效。
- `git-pool-completion.ps1`：PowerShell 补全脚本，source 到 `$PROFILE` 后生效。
