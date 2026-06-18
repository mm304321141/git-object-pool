# git-pool-completion.bash
#
# Bash completion for git-object-pool wrapper commands.
#
# Usage: add the following line to ~/.bash_profile or ~/.bashrc:
#   source ~/Work/git-object-pool/git-pool-completion.bash
#
# This file sources Apple Git's built-in git-completion.bash (if not already
# loaded) and wraps Git's main completion dispatcher so that:
#   git <Tab>                  -> all normal git subcommands + migrate
#   git migrate <Tab>          -> --recursive  -r

# Source Apple Git's completion if Git completion is not yet defined.
_git_pool_completion_bash=/Library/Developer/CommandLineTools/usr/share/git-core/git-completion.bash
if [[ -f "$_git_pool_completion_bash" ]] && \
    ! declare -f __git_wrap__git_main >/dev/null 2>&1 && \
    ! declare -f __git_main >/dev/null 2>&1; then
    # shellcheck source=/dev/null
    source "$_git_pool_completion_bash"
fi
unset _git_pool_completion_bash

_git_migrate ()
{
    local cur
    cur="${COMP_WORDS[COMP_CWORD]}"

    case "$cur" in
        --*)
            COMPREPLY=( $(compgen -W "--recursive" -- "$cur") )
            return
            ;;
        -*)
            COMPREPLY=( $(compgen -W "-r --recursive" -- "$cur") )
            return
            ;;
        *)
            # No file-name fallback for migrate; show nothing when no prefix.
            COMPREPLY=()
            return
            ;;
    esac
}

_git_pool_wrap ()
{
    local subcommand=""
    local i
    # Find the first non-option, non-empty word after position 0 that has
    # already been fully typed (i.e., not the word currently being completed).
    for (( i=1; i < COMP_CWORD; i++ )); do
        if [[ "${COMP_WORDS[$i]}" != -* && -n "${COMP_WORDS[$i]}" ]]; then
            subcommand="${COMP_WORDS[$i]}"
            break
        fi
    done

    if [[ "$subcommand" == "migrate" ]]; then
        # Delegate entirely to _git_migrate
        _git_migrate
        return
    fi

    # For all other subcommands (including empty = still choosing subcommand),
    # call the real git completion first, then append "migrate" if we are at
    # the subcommand position so it appears in the list.
    if declare -f __git_wrap__git_main >/dev/null 2>&1; then
        __git_wrap__git_main
    elif declare -f __git_main >/dev/null 2>&1; then
        __git_main
    fi

    # If we are completing the subcommand word itself, inject "migrate".
    if [[ -z "$subcommand" ]]; then
        local cur="${COMP_WORDS[COMP_CWORD]}"
        COMPREPLY+=( $(compgen -W "migrate" -- "$cur") )
    fi
}

complete -o bashdefault -o default -o nospace -F _git_pool_wrap git
