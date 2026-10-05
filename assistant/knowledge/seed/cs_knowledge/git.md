# Git Version Control
- Architecture: Working Directory -> Staging Area (Index) -> Local Repository (HEAD) -> Remote Repository.
- Key Operations:
  - `git commit --amend`: Modify recent unpushed commit.
  - `git rebase -i`: Interactive rebase for squashing and cleaning commits.
  - `git stash`: Temporarily shelter uncommitted changes.
  - `git reset --soft HEAD~1`: Move HEAD back while preserving changes staged.
  - `git reset --hard`: Dangerous destructive reset of index and working tree.
  - `git revert`: Creates a new commit that inverts changes safely for public branches.
