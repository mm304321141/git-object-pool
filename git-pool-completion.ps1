# git-pool-completion.ps1
# PowerShell argument completion for git-object-pool wrapper.
# Usage: add ". $env:USERPROFILE\git-object-pool\git-pool-completion.ps1" to $PROFILE

Register-ArgumentCompleter -Native -CommandName git -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $tokens = $commandAst.CommandElements
    $subcommand = $tokens | Select-Object -Skip 1 -First 1 | ForEach-Object { $_.ToString() }
    if ($subcommand -eq 'migrate') {
        @('--recursive', '-r') | Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
    }
    # For all other subcommands, fall through (PowerShell uses built-in git completion if posh-git is installed)
}
