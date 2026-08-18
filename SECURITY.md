# Security and Privacy

This repository is designed to be safe for public portfolio use, but local runtime data can contain sensitive information.

## Never commit

- `.env`
- API keys, access tokens, OAuth credentials, or cookies
- SQLite databases and WAL/SHM files
- real webhook URLs belonging to a client or employer
- client requirement documents
- real phone numbers, email addresses, home addresses, or calendar links
- generated reports containing production conversations
- uploaded project files under `data/uploads/`

## Before pushing

Run:

```bat
python scripts\public_repo_check.py
```

Also inspect:

```bat
git status
git diff --cached
```

If a secret was ever committed, deleting it from the latest file is not enough. Rotate the secret and remove it from Git history before making the repository public.

## Public examples

Only synthetic/demo project data should be committed under `examples/`.
