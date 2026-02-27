---
description: Create a pull request for the current branch following project conventions
alwaysApply: false
---

# Create Pull Request

When asked to create a PR, follow these steps:

## Pre-flight Checks

1. Verify there are no uncommitted changes (`git status`)
2. Ensure the branch is pushed to remote (`git push -u origin HEAD`)
3. Identify the base branch (default: `main`)

## PR Structure

### Title
Use the format: `<type>: <short description>`

Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`

Example: `feat: add Ollama GLM LLM configuration support`

### Body Template

```
## Summary
- <bullet point summary of key changes>

## Changes
- <specific files or components changed>

## Test plan
- [ ] <how to verify this works>
- [ ] Run existing tests: `pytest tests/`
```

## Command

```bash
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
- 

## Changes
- 

## Test plan
- [ ] 
EOF
)"
```

## Notes
- Target branch is `main` unless otherwise specified
- Link related issues with `Closes #<issue>` in the body if applicable
- Keep PRs focused; one concern per PR
