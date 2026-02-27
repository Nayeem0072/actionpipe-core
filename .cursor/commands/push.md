---
description: Commit all changes and push the current branch to its remote
alwaysApply: false
---

# Commit and Push Branch

When asked to commit and push, follow these steps in order:

## Steps

1. **Check status** — run `git status` to see what files are changed or untracked.

2. **Stage changes** — stage all relevant changed files:
   ```bash
   git add <files>
   ```
   Never stage files that likely contain secrets (e.g. `.env`, credentials). Warn the user if they try to.

3. **Write the commit message** — analyse the staged diff (`git diff --staged`) and draft a concise message:
   - Format: `<type>: <short description>`
   - Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`
   - One sentence max; focus on *why*, not *what*
   - Example: `docs: add LLM provider setup guide to README`

4. **Commit**:
   ```bash
   git commit -m "$(cat <<'EOF'
   <type>: <short description>
   EOF
   )"
   ```

5. **Push to remote**:
   ```bash
   git push -u origin HEAD
   ```

6. **Confirm** — run `git status` after pushing and report the remote branch URL to the user.

## Notes

- If there is nothing to commit (clean working tree), say so and skip to the push step in case the branch just needs pushing.
- Never amend commits that have already been pushed to remote.
- Never force-push unless the user explicitly requests it.
