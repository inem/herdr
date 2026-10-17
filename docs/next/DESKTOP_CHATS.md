# Continue desktop chats in Herdr (macOS fork)

Open **desktop chats** from Herdr's global menu on the Local machine. A new
workspace shows existing chats, newest first. Type to search by title, provider,
folder or ID; use the arrow keys and Enter, or click a row. Escape closes the
picker. Opening a chat preserves its original identity and history.

```sh
herdr desktop                     # picker in the current terminal
herdr desktop pick claude         # filter to one app
herdr desktop open                # picker in a new workspace
herdr desktop list                # JSON catalog, with per-app warnings
herdr desktop resume work <id>     # resume an exact existing ID
```

Providers are `work`, `claude`, and `antigravity`. Python 3 is required; the
adapter is embedded in the Herdr binary and uses only the standard library.
Herdr prefers Homebrew Python when present: Apple's older SQLite library can
reject Antigravity's live WAL database. Use `HERDR_DESKTOP_PYTHON` to select a
compatible Python executable explicitly.
These commands operate on the local Mac, including when Herdr has SSH machines.
They do not import transcripts, create replacement chats, or modify history files.

| App | Supported history | Continuation |
| --- | --- | --- |
| ChatGPT Work | Local Work threads tagged `codex_work_desktop` in Codex's history database | A current Codex CLI resumes the original thread in a dedicated `--no-daemon` process. |
| Claude Desktop | Local Code sessions with a `cliSessionId` and an existing Claude transcript | Claude Code runs `--resume` with that exact ID and the original folder. |
| Antigravity App | Root desktop conversations in the App's own history database | Herdr sends text to the running App's local service with the original cascade ID. |

Ordinary ChatGPT cloud chats, Claude Chat/Cowork, Antigravity IDE, and importing
Antigravity desktop history into `agy` are outside this feature.

## Work and Claude

The original working folder and the corresponding signed-in CLI must exist.
Herdr prefers Codex bundled with ChatGPT, then the managed Codex package, then
`codex` on PATH; every candidate must support `--no-daemon`. Set
`HERDR_DESKTOP_CODEX` to select a particular executable. The native CLI keeps
its trust, login and approval prompts. Claude uses Desktop's `~/.claude` profile.

An existing owner may block a chat. Close that chat's CLI or desktop session and
retry. Herdr does not override ownership or fork the chat on failure. Exit the
CLI with its normal exit command when returning to Desktop; detaching from Herdr
leaves the CLI running. The dedicated Codex process avoids the shared daemon's
retention of the chat after a CLI disconnect.

## Antigravity App

Keep Antigravity App running. The adapter discovers the App's existing local
endpoint through its `DevToolsActivePort` file. If unavailable, open the App and
retry; Herdr does not relaunch it or enable remote debugging. Requests are limited
to literal `127.0.0.1`, with redirects and proxy use disabled. Its local token stays
in memory. This is an experimental adapter for the App's internal protocol and
may require updates after an App upgrade.

The terminal shows recent messages and accepts one message per line. It reads
the existing model and agent configuration before sending. Busy conversations
are rejected. Replies are polled while running; `/refresh` reads newer desktop
messages and `/exit` leaves the bridge. Tool approvals, attachments and model
selection remain in Desktop. Ctrl+C stops waiting for output; it does not cancel
the desktop task. Ambiguous sends are never retried automatically.

Discovery runs only when opening/listing/resuming chats, outside Herdr's rendering
and agent detection loops. A broken provider produces a warning while other
providers remain available. Run adapter contract tests with `just desktop-test`.
