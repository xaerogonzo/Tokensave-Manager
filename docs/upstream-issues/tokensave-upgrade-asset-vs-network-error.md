<!--
STATUS: FILED 2026-09-08 as issue #513 — awaiting maintainer response.
  https://github.com/aovestdipaperino/tokensave/issues/513

  SCOPE NARROWED BEFORE FILING. The draft originally reported two defects:
  the missing Windows assets AND the misleading error. The duplicate search
  found the first already filed, thoroughly, with a root cause this report
  did not have — so only the second was filed.

DUPLICATE SEARCH (gh search issues, all states):
  #512 Windows asset missing from v7.11.1 release — OPEN, filed 2026-09-07
       by @godzail. Complete duplicate of the packaging half, and better:
       traces it to the SignPath signing legs timing out in release run
       #170 (3600s approval timeout), so the signed ZIPs never uploaded and
       the dependent Scoop job then failed validating them. Also notes that
       workflow_dispatch cannot fix it, since sign-windows and update-scoop
       are gated on github.event_name == 'release'.
       Commented on it with the user-facing symptom + the state.toml
       timestamps, rather than re-filing:
       https://github.com/aovestdipaperino/tokensave/issues/512#issuecomment-5580235708
  #136 v6.4.0 doesn't have Windows assets — CLOSED. Same packaging failure,
       previous occurrence. This is the evidence that the diagnosis bug in
       #513 will fire again, which is the argument for filing it separately
       from #512 rather than as a comment on it.

  Searched: "could not reach GitHub" (0 hits), "windows asset" (the two
  above), "upgrade windows" (0), "release artifact" (0). NB `gh search
  issues --state all` is rejected; omit --state to search all states.

Found against tokensave 7.11.0 (stable) on Windows 11.

SANITISED BEFORE FILING: no paths, project names, usernames or usage
figures. Every fact is drawn from the public releases API or the tool's own
stdout. The published body is byte-identical to what follows this comment
block; the block itself is local-only and was stripped before publishing.
-->

# `upgrade`: a release with no asset for the platform is reported as "could not reach GitHub"

## Summary

When the latest release has no asset for the running platform, `tokensave upgrade` fails with:

```
Error: config error: failed to check for updates — could not reach GitHub
```

GitHub is reachable. Nothing is wrong with the user's configuration. The message names the two things that are fine and not the one thing that is wrong, so it sends people to debug a working network.

**This is deliberately not a re-report of #512.** #512 is that v7.11.1's Windows assets are missing, and why — the SignPath signing legs timed out in release run #170, so the signed ZIPs never uploaded. That is a release-workflow problem with a known fix. This issue is that `upgrade` cannot tell a missing asset from an unreachable host, which survives the v7.11.1 re-sign and will misdiagnose the next occurrence. #136 ("v6.4.0 doesn't have Windows assets") shows there is a next occurrence.

## Reproduction

Windows 11, tokensave 7.11.0 (stable), while v7.11.1 is latest and carries no Windows asset:

```
$ tokensave upgrade
Current version: v7.11.0 (stable channel)
Checking for updates...
Error: config error: failed to check for updates — could not reach GitHub
```

Exit code 1, deterministic across repeated invocations.

## Why this is a diagnosis bug and not a network fault

Reachability verified three ways from the same shell, minutes either side of a failing `upgrade`:

- `curl` to `api.github.com/repos/aovestdipaperino/tokensave/releases/latest` — **200**
- PowerShell `Invoke-WebRequest` (schannel, a different TLS stack from the one curl uses here) to both `api.github.com/.../releases/latest` and `github.com/.../releases/latest` — **200** each
- `gh release list` — returns the full release list, including v7.11.1

Explicitly ruled out:

| Suspect | Observation |
|---|---|
| Rate limiting | `api.github.com/rate_limit` reported `core` 55/60 remaining during a failing run |
| Proxy | No `*_PROXY` variables in the environment; `ProxyEnable = 0` in Internet Settings |
| Firewall | No `Get-NetFirewallApplicationFilter` rule matches the binary, in either direction |
| IPv6 | No AAAA record for `api.github.com` here, so not a v6 route timing out before fallback |
| Authentication | Re-running with a valid `GITHUB_TOKEN` and `GH_TOKEN` in the environment changes nothing |

The tool's own state agrees that its checks are failing rather than that it is current. `~/.tokensave/state.toml`:

```toml
cached_latest_version = "7.11.0"
last_version_check_at = 1788720641   # 2026-09-06 18:50:41 UTC
```

v7.11.1 was published at 2026-09-06 22:03:09 UTC — **3h13m after** the last check that succeeded. So the cache is not stale by neglect: every check since the release has failed, and the last successful one predates it.

## Consequence

The message is wrong in the direction that costs the most time. It asserts a network fault, which is the one hypothesis a user cannot cheaply disprove without already knowing to go and diff release asset lists. About an hour went that way here before the v7.11.1 asset names were compared against v7.11.0's.

It compounds for anyone wrapping the CLI: a GUI that shows the string verbatim gives the user no way to tell the two conditions apart, and the honest reading of it is the wrong one.

Classifying it as a `config error` adds a second wrong pointer — it implicates the user's configuration, which is not involved.

## Suggested fix

Distinguish the two conditions. They have different remedies and should not share a message:

```
Error: v7.11.1 has no asset for x86_64-windows.
       Published assets: aarch64-linux, aarch64-macos, x86_64-linux
       (+2 Homebrew bottles). Staying on v7.11.0.
```

A genuine reachability failure would keep the current wording. As a smaller variant of the same point: whatever error the HTTP layer actually returned is worth surfacing rather than collapsing to a single sentence — a 404 on an asset URL and a connection timeout are both currently spelled "could not reach GitHub".

Exiting non-zero is correct in both cases; only the diagnosis needs to differ.

## Environment

- tokensave 7.11.0 (stable channel), Windows 11
- Deterministic across repeated invocations
- Reachability to `api.github.com` and `github.com` verified from the same shell, at the same time, via three independent HTTP clients
