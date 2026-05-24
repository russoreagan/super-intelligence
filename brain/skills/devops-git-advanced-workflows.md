# Git Workflows (Motor Reference)

Concise git command reference for constructing `run_command` tool calls.

## Status and Inspection
```bash
git status                          # working tree state
git log --oneline -20               # recent commits
git diff                            # unstaged changes
git diff --staged                   # staged changes
git diff main..HEAD                 # branch vs main
git show <sha>                      # inspect a commit
git blame <file>                    # who changed what
```

## Branching
```bash
git branch -a                       # list all branches
git checkout -b <name>              # create and switch
git checkout <name>                 # switch branch
git branch -d <name>                # delete merged branch
```

## Staging and Committing
```bash
git add <file>                      # stage specific file
git add -p                          # stage hunks interactively
git commit -m "message"             # commit with message
git commit --amend --no-edit        # amend last commit (keep message)
```

## Sync
```bash
git fetch origin                    # fetch without merging
git pull --rebase origin <branch>   # rebase on remote
git push origin <branch>            # push branch
git push -u origin <branch>         # push and track
```

## Stash
```bash
git stash                           # stash dirty working tree
git stash pop                       # restore latest stash
git stash list                      # list stashes
```

## History Repair
```bash
git rebase -i HEAD~<n>              # interactive rebase last n commits
git cherry-pick <sha>               # apply single commit
git revert <sha>                    # revert without rewriting history
git reset --soft HEAD~1             # undo last commit, keep changes staged
```

## Finding Bugs
```bash
git bisect start
git bisect bad                      # current commit is broken
git bisect good <sha>               # last known good commit
# test, then: git bisect good/bad
git bisect reset                    # when done
```

## Key Rules
- Prefer `reset --soft` over `reset --hard` (keeps work)
- Never force-push main/master
- Always `git status` before committing to avoid surprises
- Use `git diff --staged` to review what's about to be committed
