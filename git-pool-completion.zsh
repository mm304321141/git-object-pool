# git-pool-completion.zsh
#
# Zsh completion for git-object-pool wrapper commands.
#
# Usage: add the following line to ~/.zshrc:
#   source ~/Work/git-object-pool/git-pool-completion.zsh
#
# This registers a _git-migrate completion function and hooks it into zsh's
# git completion so that:
#   git migrate <Tab>          -> --recursive  -r

# Ensure compinit has been called.
autoload -Uz compinit
if [[ -z "$_comp_dumpfile" ]]; then
    compinit -C
fi

# Hook into zsh git completion: define _git-migrate so that the built-in
# _git dispatcher finds it automatically when the subcommand is "migrate".
_git-migrate () {
    _arguments -C \
        '(-r --recursive)'{-r,--recursive}'[also migrate initialized submodules]' \
        && return 0
}

# Register "migrate" as a known git subcommand for zsh's _git dispatcher.
# The zsh git completion looks up _git-<subcommand> automatically, but we
# also need "migrate" to appear in the subcommand list.
# We achieve this by wrapping __git_builtin_commands / zstyle if the hook
# point exists, otherwise fall back to a minimal compdef.
if (( $+functions[_git] )); then
    # zsh-shipped _git (from git-completion.zsh) uses zstyle user-commands.
    zstyle ':completion:*:git:*' user-commands migrate:'migrate existing repo into git object pool'
fi
