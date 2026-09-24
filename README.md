# Apartment Notifier

Apartment Notifier watches one or more StreetEasy rental searches, applies
local filters, remembers listings in SQLite, and sends new-listing or optional
price-change alerts through iMessage, email, or Twilio SMS.

It is designed for a personal Mac. Live StreetEasy requests are disabled by
default, notification channels are disabled until configured, and all tests use
saved HTML or mocks.

## StreetEasy permission is required

Do not enable live requests merely because another user received permission.
Every user must independently confirm that their own automated access is
authorized and must keep a durable reference to that permission.

The program requires all three of these before it will make a live request:

- `allow_live_requests = true`
- A non-empty `permission_reference`
- A contactable `user_agent` containing `mailto:`

It also restricts requests to `allowed_url_prefix`, spaces request starts by at
least 30 seconds, stops at `max_pages`, and backs off after HTTP 403 responses.
There is no CAPTCHA bypass, proxy rotation, browser impersonation, or
rate-limit evasion.

## Fresh Mac checklist

1. Install Python 3.11 or newer from
   [python.org](https://www.python.org/downloads/macos/) or Homebrew.
2. Copy or clone this project and open Terminal in its directory.
3. Run `./setup-macos.sh`.
4. Create searches on StreetEasy in a browser and copy their complete URLs.
5. Run `./apartment-notifier configure`.
6. Run `./apartment-notifier validate`.
7. Test each enabled notification channel.
8. Independently confirm StreetEasy permission before enabling live access.
9. Run `./apartment-notifier baseline`.
10. Run in Terminal or install the launch agent.

## Prerequisites

- macOS on Apple Silicon or Intel
- Python 3.11 or newer
- A signed-in Messages app for iMessage delivery
- A StreetEasy rental-search URL
- Independent authorization before making automated StreetEasy requests

The application has no third-party runtime dependencies.

## Installation

From a copied release directory:

```bash
cd apartment-notifier
./setup-macos.sh
./apartment-notifier configure
./apartment-notifier validate
```

Or after cloning:

```bash
git clone <repository-url> apartment-notifier
cd apartment-notifier
./setup-macos.sh
./apartment-notifier configure
./apartment-notifier validate
```

`setup-macos.sh` selects Python 3.11+, creates `.venv`, and installs the local
project. It never replaces `config.toml`. To check prerequisites without
installing anything:

```bash
./setup-macos.sh --check
```

If more than one Python is installed, choose one explicitly:

```bash
APARTMENT_NOTIFIER_PYTHON="$(command -v python3.12)" ./setup-macos.sh
```

Normal use goes through `./apartment-notifier`; knowledge of virtual
environments or Python packaging is not required.

## Create a StreetEasy search URL

1. Open StreetEasy in a normal browser.
2. Configure the rental location and filters you want StreetEasy to apply.
3. Prefer newest-first ordering so page one contains recently listed rentals.
4. Copy the complete results-page URL from the address bar.
5. Paste it when `./apartment-notifier configure` asks for the URL.

The configuration wizard asks about common local filters and notifications.
Advanced settings remain editable in `config.toml`. Run the wizard again with
`--force` only when you intentionally want to replace an existing config; it
never deletes the SQLite database.

## Configuration

`config.example.toml` is a sanitized, fully annotated starting point. The
guided command produces the same format. Any number of `[[searches]]` entries
is supported:

```toml
[app]
database = "data/notifier.sqlite3"
poll_interval_seconds = 40
full_scan_interval_seconds = 3600
notify_existing_on_first_run = false
notify_price_changes = true

[streeteasy]
allow_live_requests = false
permission_reference = ""
allowed_url_prefix = "https://streeteasy.com/for-rent/nyc"
minimum_request_interval_seconds = 40
max_pages = 25
rate_limit_state_file = "data/streeteasy-rate-limit"
user_agent = "ApartmentNotifier/0.2 (+mailto:you@example.com)"
timeout_seconds = 20

[[searches]]
name = "downtown"
url = "https://streeteasy.com/for-rent/nyc?sort_by=listed_desc"

[searches.filters]
neighborhoods = ["Soho", "Tribeca"]
min_price = 3000
max_price = 5000
min_bedrooms = 1
max_bedrooms = 2
min_bathrooms = 1
available_on_or_before = 2026-11-01
required_keywords = []
excluded_keywords = ["short term"]
required_amenities = ["Dishwasher"]
```

Relative paths are resolved from the config file’s directory. `~`, `$NAME`,
and `${NAME}` are expanded without invoking a shell. An unset variable causes a
specific validation error instead of silently creating an unexpected path.

Unknown numeric or neighborhood data fails a configured constraint to avoid
false-positive alerts. An unknown availability date is allowed because the
listing is not known to violate the cutoff.

### Geographic exclusions

Local coordinate polygons require no extra listing-page requests. Coordinates
are `[latitude, longitude]`; the last point connects to the first:

```toml
[[searches.filters.excluded_areas]]
name = "excluded pocket"
points = [
  [40.720, -73.980],
  [40.730, -73.980],
  [40.730, -73.970],
  [40.720, -73.970],
]
```

A listing inside or on the polygon is rejected. A listing without coordinates
is also rejected when a geographic exclusion is configured.

## Validate before running

```bash
./apartment-notifier validate
```

Validation does not contact StreetEasy or send notifications. An offline parser
check is also available:

```bash
./apartment-notifier --config config.example.toml check \
  --fixture example-search=tests/fixtures/search_results.html \
  --dry-run
```

The fixture name must match a configured search. The example above is entirely
offline and uses the locked example configuration.

## iMessage setup

iMessage delivery controls the signed-in macOS Messages app using AppleScript.
For separate messages to multiple people:

```toml
[notifications.imessage]
enabled = true
recipients = ["+12125550101", "person@example.com"]
chat_id = ""
recipient_env = "IMESSAGE_RECIPIENT"
```

Each recipient receives a separate message. The program does not claim to
create reliable group chats. To address a group, first verify an existing
Messages chat ID and configure only that `chat_id`.

The first test may prompt for permission. Allow Terminal—or the Python process
used by the launch agent—to control Messages under **System Settings → Privacy
& Security → Automation**.

Send an actual test message without contacting StreetEasy:

```bash
./apartment-notifier test-notifications --channel imessage
```

## Optional Gmail email setup

This SMTP integration uses a Gmail App Password. Google requires 2-Step
Verification for App Passwords and notes that some managed or Advanced
Protection accounts may not offer them; see
[Google Account Help](https://support.google.com/accounts/answer/185833?hl=en).
The guided configuration can save that App Password in the current user’s
macOS Keychain; the password is never written to `config.toml`.

The generated email section resembles:

```toml
[notifications.email]
enabled = true
host = "smtp.gmail.com"
port = 587
starttls = true
use_ssl = false
username_env = "SMTP_USERNAME"
password_env = "SMTP_PASSWORD"
password_keychain_service = "Apartment Notifier Gmail"
password_keychain_account = "sender@example.com"
from_address = "sender@example.com"
to_addresses = ["recipient@example.com"]
```

Environment variables override Keychain values and preserve compatibility with
older configurations:

```bash
export SMTP_USERNAME="sender@example.com"
export SMTP_PASSWORD="your-app-password"
./apartment-notifier test-notifications --channel email
```

Shell environment variables are not reliably inherited by launch agents, so
Keychain storage is recommended for unattended email delivery.

## Optional Twilio SMS

Existing Twilio SMS support remains available for advanced users. Put the
account SID and token in environment variables, not TOML:

```bash
export TWILIO_ACCOUNT_SID="..."
export TWILIO_AUTH_TOKEN="..."
```

Configure only the sender and recipient numbers in `[notifications.sms]`.

## Enable an authorized live source

Only after receiving your own written authorization:

```toml
[streeteasy]
allow_live_requests = true
permission_reference = "your-ticket-or-agreement-reference"
allowed_url_prefix = "https://streeteasy.com/for-rent/nyc"
minimum_request_interval_seconds = 40
user_agent = "ApartmentNotifier/0.2 (+mailto:your-real-address@example.com)"
```

Keep the interval and scope within the terms of that authorization. A full scan
fetches all result pages up to `max_pages`. Routine checks fetch only the newest
page. Every request, including pagination, passes through the same cross-process
rate limiter.

HTTP 403 responses cause exponential cooldowns of 2, 4, 8, 16, and up to 30
minutes, or longer if `Retry-After` requires it. Diagnostics include only an
allowlist of response headers and never cookies.

## Baseline behavior

After validation and notification testing:

```bash
./apartment-notifier baseline
```

`baseline` records all currently matching listings and never sends listing or
price-change notifications. It preserves existing state and can safely refresh
an earlier partial baseline. The first normal full scan also baselines by
default when `notify_existing_on_first_run = false`.

Do not delete the database to repeat a baseline; deleting it also removes
notification history.

## Foreground operation

Run continuously in Terminal:

```bash
./apartment-notifier run
```

To prevent idle system sleep while keeping a foreground Terminal session:

```bash
caffeinate -i ./apartment-notifier run
```

Press Control-C to stop.

## Run automatically with launchd

Install and start a per-user launch agent after setup, validation, notification
testing, and baseline:

```bash
./apartment-notifier install-launch-agent
```

The generated plist uses the actual project directory, active virtual
environment Python, config path, home directory, and log paths. It starts after
login and restarts after failures.

Manage it with:

```bash
./apartment-notifier launch-agent-status
./apartment-notifier stop-launch-agent
./apartment-notifier start-launch-agent
./apartment-notifier uninstall-launch-agent
```

Inspect logs:

```bash
tail -f "$HOME/Library/Logs/Apartment Notifier/notifier.log"
tail -f "$HOME/Library/Logs/Apartment Notifier/notifier-error.log"
```

Uninstalling removes the generated plist but leaves the project, configuration,
Keychain entry, database, and logs intact.

## Updating

For a Git checkout:

```bash
./apartment-notifier stop-launch-agent
git pull --ff-only
./setup-macos.sh
./apartment-notifier validate
./apartment-notifier start-launch-agent
```

For a copied release, stop the launch agent, replace only program and
documentation files, retain `config.toml` and `data/`, rerun
`./setup-macos.sh`, validate, and restart.

## Back up and restore SQLite state

Stop the foreground process or launch agent before copying the database so all
writes are closed:

```bash
./apartment-notifier stop-launch-agent
mkdir -p backups
cp data/notifier.sqlite3 "backups/notifier-$(date +%Y%m%d-%H%M%S).sqlite3"
```

Restore while the notifier is stopped:

```bash
cp backups/notifier-YYYYMMDD-HHMMSS.sqlite3 data/notifier.sqlite3
./apartment-notifier validate
./apartment-notifier start-launch-agent
```

The database contains listing observations and per-channel delivery history.
Protect backups accordingly.

## Troubleshooting

### Python is too old or `tomllib` is missing

Run `./setup-macos.sh --check`. The setup script deliberately ignores an older
default `python3` when a compatible `python3.11`, `python3.12`, or `python3.13`
is available. Otherwise install a current Python and rerun setup.

### Invalid configuration

Run `./apartment-notifier validate` and address the named field. Common causes
are quoted booleans such as `"false"`, an unset variable in a path, a duplicate
search name, an invalid date, or a live URL outside `allowed_url_prefix`.

### HTTP 403 or an anti-automation challenge

The notifier stops the check and backs off. Do not add evasion techniques.
Confirm authorization, URL scope, request interval, and any instructions from
StreetEasy. Disable live requests while resolving the issue.

### Messages permission or delivery failure

Confirm Messages is signed in, the handle is reachable through iMessage, and
Automation permission is enabled for the invoking Terminal or Python process.
For group delivery, verify that the configured chat ID already exists.

### Gmail authentication failure

Create a Gmail App Password, rerun guided configuration with `--force` only if
you intend to replace the config, or update the matching Keychain item. For a
foreground test, environment variables can override Keychain credentials.

### Launch agent is not running

Run `launch-agent-status`, inspect both log files, and run
`start-launch-agent`. Reinstall the agent after moving the project or changing
the Python environment so its generated absolute paths are refreshed.

## Development and offline tests

The full test suite is offline: network and notification operations are mocked,
and StreetEasy parsing uses saved HTML.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Never add real email addresses, phone numbers, chat IDs, permission references,
search URLs, database files, rate-limit files, logs, or credentials to tracked
files.
