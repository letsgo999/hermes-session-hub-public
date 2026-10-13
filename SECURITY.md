# Security and Privacy

Session Hub runs only on your own PC and reads Hermes data read-only.

- Hermes `state.db` and Kanban databases are opened with SQLite `mode=ro`, `PRAGMA query_only=ON`
  and an authorizer that allows only SELECT/READ and a few read-only PRAGMAs.
- Branch snapshots are copied to a local cache and opened only after their sha256 matches the manifest.
- The Codex/Claude collector stores only a title, the first request line, the last three answer
  lines, status, folder and timestamps per session; obvious secrets and e-mail addresses are masked.
- `GET /config` never returns filesystem paths. Skills/AI-session refresh endpoints answer only to a
  loopback Desktop request.

Never commit or attach to issues: session transcripts, local SQLite files, logs, your
`session-hub.config.json` / `ai-sessions.config.json`, API keys, tokens, cookies or `.env` files.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) instead of a
public issue. Include the plugin version, Hermes version and reproduction steps without personal data.
