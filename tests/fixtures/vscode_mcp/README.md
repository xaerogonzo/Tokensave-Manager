# VS Code MCP experiment fixtures

One JSON file per Phase-A experiment observation. Tests consume these rather
than prose or screenshots, so that a client update which silently changes
duplicate-name arbitration or config precedence fails a test instead of
surprising a user.

Schema (`fixture_version: 1`):

| Field | Meaning |
|---|---|
| `experiment` | `A0.1`–`A0.4` |
| `client` / `client_version` | the harness the user drives |
| `host` | what actually owns the MCP session |
| `consulted_sources` | every config path the client reads *at all* |
| `effective_source` | the one that wins |
| `layers` | `configured` / `started` / `connected` / `serving_project`, recorded separately — never collapsed into one status |
| `evidence` | how each claim was established, tagged `config` / `process` / `behavioural` |
| `manager_action` | `managed` \| `detect-only` \| `unsupported` \| `no action required` |

Sanitise before committing: no absolute user paths (`C:\Users\…`,
`/home/<user>/…`), no tokens. Use `<HOME>`, `<APPDATA>`, `<PROJECT>`.
