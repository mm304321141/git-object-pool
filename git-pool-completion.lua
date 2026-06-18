-- git-pool-completion.lua
-- Clink completion for git-object-pool wrapper (Windows CMD via Clink).
-- Usage: copy this file to %LocalAppData%\clink\
-- See: https://github.com/chrisant996/clink

local migrate_parser = clink.argmatcher()
migrate_parser:addflags({ "-r", "--recursive" })

local git_parser = clink.argmatcher("git")
git_parser:addarg({ "migrate" }):chaincommand(migrate_parser)

-- Note: Clink's built-in git completion handles all other git subcommands.
-- This script only registers the extra "migrate" subcommand.
