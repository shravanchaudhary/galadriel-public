# Credentials map

This file describes which credential sets exist and how to look them up. It is a map,
not a vault: never put passwords, TOTP secrets, API keys, tokens, or cookie values here.

Read this file before authenticated work. Fetch the secret from the configured
credential store only when needed, never echo it, and keep this metadata current when
a credential is added, rotated, renamed, or removed.

| name | service | kind | fields held | notes |
|---|---|---|---|---|
| None configured | — | — | — | — |
