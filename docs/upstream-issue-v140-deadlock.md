# v1.4.0 daemon hangs: all /v1/* requests block indefinitely while /version and /healthz stay responsive (recovered by restart)

## Summary

The native daemon (wx v1.4.0, linux-x64-gnu, official release asset, sha256
`ace3ecc786951eb385572a267e9c3c35eb4a29475b16ba91fb9091bbd53f9879`) twice entered a
state where **every authenticated `/v1/*` request blocks forever**, while
`GET /version` and `GET /healthz` (no auth) continue to answer with 401 in
microseconds. The event loop is alive but a global lock appears to be held
indefinitely. A daemon restart (cold recovery) clears it. Both occurrences
happened with **no user message traffic** at all.

## Environment

- agent-wechat Native daemon v1.4.0 (official `wx-v1.4.0-linux-x64-gnu`, checksums.json-verified)
- Linux x86_64, kernel 6.x, glibc 2.39
- Running inside a private network namespace (slirp), systemd transient units, `--data-dir` on ext4
- One WeChat account logged in, storage mode `managed`
- Bridge client: HTTP calls to `127.0.0.1:26174`, SSE event stream subscription

## Timeline of the two occurrences

### Occurrence 1 — 2026-11-04

| UTC time | observation |
|---|---|
| 19:03 | Bridge reported `ready:true`, SSE connected, all green |
| 19:20–20:06 | SSE silently disconnected somewhere in this window (bridge pump kept retrying) |
| 20:06 | Diagnosed: `/version` → 401 in 0.3 ms; `GET /v1/accounts/wechat-primary` → no response at all (curl `-m 6` times out, request never served) |
| 20:16 | Thread dump: 4 threads total — main thread blocked in `futex_wait_bitset` (NULL timeout, FUTEX_WAIT_BITSET_PRIVATE), two tokio runtime workers (one in `ep_poll`, one in `futex`), one in `tcp_recvmsg`. No outbound TCP connections from the daemon netns. `media-maintenance.timer` on host unaffected. |
| 20:4x | strace confirmed `FUTEX_WAIT_BITSET_PRIVATE` with no timeout on the blocked threads. |
| Next day 07:15 | Host machine rebooted (unrelated), daemon restarted |

Traffic context: **`received:0, forwarded:0`** during the whole window — no
incoming or outgoing messages were flowing when the hang began.

### Occurrence 2 — 2026-11-06

| UTC time | observation |
|---|---|
| 13:40 | Full acceptance suite green (`ready:true`, 17/17 checks), one group text message flowed end-to-end earlier |
| 13:57:03 | Daemon verified healthy by hand: `GET /v1/accounts/wechat-primary` → HTTP 200 in 0.093 s, `GET /v1/accounts/…/events?after=23485` streamed ~423 KB of events normally |
| 13:59 | Bridge health probe flipped to `native_connection:"unreachable"` (probe = `GET /v1/accounts/...` with 8 s timeout) |
| 14:16 | Forensics: same signature as occurrence 1 — `/version` 401 in 0.3 ms; all `/v1/*` requests (with or without bearer token, any path incl. `/v1/accounts/wechat-primary`) hang indefinitely; connections accepted (kernel queue) but never read; 4 threads, blocked in futex / get_signal; ~100 CLOSE-WAIT + ESTAB sockets on 26174 with non-empty Recv-Q |
| 14:04 | Restart via cold recovery |
| 14:11 | Ready again, fully green |

Traffic context: essentially idle between 13:57 and 13:59 (no forwarded
messages in that window; the only recent activity was a single group text at
~13:1x and my own health-check GETs).

## Observed signature (both times)

- `GET /version` and `GET /healthz` (unauthenticated, no account lock) → 401 unauthorized in < 1 ms — the HTTP accept loop and routing are alive
- Every authenticated `/v1/*` request → connection accepted, request bytes never consumed, no response, no timeout — hangs even **without** a valid bearer token (i.e. blocks *before* auth completes)
- Requests to different resources (`/v1/accounts/wechat-primary`, `/v1/accounts/.../events`, unknown paths) all hang identically → the block is **global, not per-resource** — consistent with a process-wide lock (e.g. account state / registry lock) acquired on any `/v1` request and never released
- Blocked threads: `futex_wait_bitset` (FUTEX_WAIT_BITSET_PRIVATE, no timeout), one tokio worker in `ep_poll`, one in `tcp_recvmsg`
- No outbound TCP activity from the daemon netns (not waiting on WeChat CDN)
- No OOM, no disk pressure, CPU ~0
- Bridge-side SSE pump reconnect loop kept cycling (20 s timeouts) — server accepted each connection but never answered the subscription request

## Recovery

`kill` + cold start of the daemon and a bridge cold-recovery run restored full
service in ~3 minutes with no data loss (no new QR code, session survived).
Both times the state was not self-healing — it persisted for hours until restart.

## Frequency

Two occurrences in ~48 h of runtime with very light traffic (a handful of
text messages, a couple of images, one group chat active). Both began while
the system was effectively idle, so a specific triggering message type could
not be isolated.

## What would help

- Any known global lock / mutex held across await points on the `/v1` request path in v1.4.0
- Whether the `#382/#374` lock-scope reductions (message sync account-lock time) touched a lock that can be left held by a cancelled/panicked task
- Debug symbols or guidance for capturing a tokio-console / minidump from a hung instance (`/opt/wx` ships stripped; we can reproduce and collect on demand)
- We can provide `strace`/`gdb` captures from the next occurrence

## Non-issues we ruled out

- Host resource exhaustion (no OOM kills, 28 GB free RAM, disk 284 GB free)
- CDN / upstream network (no outbound sockets pending)
- Our bridge client (a plain `curl` with no auth hangs the same way)
- `media-maintenance.timer` contention (runs fine during the hang)
