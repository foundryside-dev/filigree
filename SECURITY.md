# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.1.x   | Yes       |
| < 2.1   | No        |

Only the latest release receives security fixes.

## Reporting a Vulnerability

To report a security vulnerability, use [GitHub's private vulnerability reporting](https://github.com/foundryside-dev/filigree/security/advisories/new).

We aim to acknowledge reports within 7 days and provide a fix or mitigation plan within 30 days.

## Scope

Filigree is a **local-first** tool. There is no network surface unless the optional web dashboard (`filigree-dashboard`) is enabled, in which case it binds to `localhost:8377` by default. The multi-project dashboard can serve multiple local projects from a single instance.

The primary attack surface is:

- **Local file access**: Filigree reads/writes SQLite databases in `.filigree/` directories
- **CLI input handling**: User-supplied arguments are passed to SQLite via parameterized queries
- **MCP server**: Communicates over stdio (no network)
- **Dashboard**: Binds to localhost only; project paths are validated against a local registry

SQL injection is mitigated by parameterized queries throughout. If you find a case where user input reaches SQL without parameterization, please report it.
