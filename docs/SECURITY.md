# Security pass (report roadmap item, Second Revision)

Three items, each covered below: password hashing (already sound, verified
not changed), CSRF protection (new), and login rate limiting (new). See
`gymflow/security.py` for the implementation — it is deliberately small and
hand-written rather than a new dependency (Flask-WTF, Flask-Limiter),
matching the rest of the codebase's style of writing its own algorithms
(WaitlistManager, the resilience.py retry/circuit breaker, the
recommendation engine) so every line is checkable against the code.

## 1. Password hashing — already sound, unchanged

`models.py`'s `User.set_password()` / `check_password()` already use
Werkzeug's `generate_password_hash()` / `check_password_hash()`:

```python
def set_password(self, raw_password: str) -> None:
    self.password_hash = generate_password_hash(raw_password)

def check_password(self, raw_password: str) -> bool:
    return check_password_hash(self.password_hash, raw_password)
```

This is a real, salted, slow (deliberately expensive to brute-force) hash
— `scrypt` by default on this project's Werkzeug version — never plain
text, and never a fast/unsalted hash such as a bare MD5 or SHA-256 (which
would let an attacker who steals the database crack every password in
bulk using a precomputed table). No code change was needed here; this
section exists so the state of this item is explicit rather than assumed.

One related gap, closed as part of this pass: the login route used to look
up the user and only call `check_password()` if an account with that email
existed, which means an unknown email returned slightly faster than a
known one with a wrong password (no hash to check). That timing
difference is a real, if minor, way to enumerate which emails are
registered members. `routes/auth.py` now always runs one password-hash
comparison either way (against a fixed dummy hash when the email is
unknown), so both cases take about the same time.

## 2. CSRF protection — new

**The problem.** Before this pass, every POST route trusted the session
cookie alone. A malicious page on another site can make the member's
browser submit a POST to GymFlow using that member's own valid session
cookie (the browser attaches cookies automatically to any request to a
site the user is logged into, regardless of which page triggered the
request) — e.g. a hidden auto-submitting form pointed at
`/members/<id>/photo/delete` or `/admin/staff/<id>/deregister`. This is
Cross-Site Request Forgery (CSRF): the request looks completely legitimate
to the server because the cookie is real, even though the member never
intended to submit it.

**The fix.** A synchronizer token: the first time a browser loads any
GymFlow page, `csrf_token()` (`gymflow/security.py`) puts a random token in
that browser's own session (`session['_csrf_token']`), inaccessible to any
other website. Every one of the 38 POST forms across the app now carries
that same token as a hidden field:

```html
<form method="post" action="...">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  ...
</form>
```

The two POSTs that are not plain HTML forms (the AI assistant's chat
send/reset buttons, driven by JavaScript `fetch()`) instead read the token
from a `<meta name="csrf-token">` tag in `base.html`'s `<head>` and send it
as an `X-CSRFToken` header.

`gymflow/__init__.py` registers a `before_request` hook
(`security.validate_csrf_request()`) that rejects (HTTP 400) any POST, PUT,
PATCH or DELETE whose submitted token does not match the token in that
visitor's own session. A malicious third-party page has no way to read
GymFlow's session cookie or craft a matching token (it's a random 32-hex
value, and the comparison uses `hmac.compare_digest` to also avoid leaking
the correct value one character at a time via response-timing) — so a
forged cross-site request is rejected even though the cookie itself is
valid.

**Why the existing test suite needed no changes.** CSRF protection is
about defending against a *different site* tricking a *real browser* into
sending a request — something an in-process HTTP test client (which never
runs a browser or loads a page) cannot meaningfully exercise anyway.
`validate_csrf_request()` skips the check when `app.testing` is true
(unless `CSRF_FORCE_ENABLED` is explicitly set) — this is not a special
case invented for this project, it is the same convention Flask-WTF's own
`CSRFProtect` documents and defaults to. That is why none of
`_devtools/test_smoke.py`'s roughly 130 POST calls needed a token added.
The mechanism itself is proven for real with `CSRF_FORCE_ENABLED` in
`_devtools/test_unit.py` (section 9): a POST with no token is rejected, a
POST with the wrong token is rejected, and a POST with the correct
session token succeeds.

**Demoing it live**: open any page's browser dev tools, copy one of the
POST forms' `action` URL, and try replaying it with `curl` (no cookie, no
token) — it now gets a 400 instead of executing. Logged in normally
through the browser, nothing changes; the token is invisible and automatic.

## 3. Login rate limiting — new

**The problem.** The login route had no limit on failed attempts: a script
could try passwords against `admin@gymflow.local` (or any known member
email) as fast as the network allowed — a brute-force attack.

**The fix.** `gymflow/security.py` tracks failed attempts per email in a
sliding 5-minute window: after `MAX_LOGIN_ATTEMPTS` (5) failures for the
same email within `LOGIN_WINDOW_SECONDS` (300), further attempts for that
email are refused (with a "try again in Xm Ys" message) until enough time
has passed for the failure count to drop back under the limit. A
successful login clears that email's failure history immediately, so a
member who mistypes their password twice and then gets it right is not
penalised afterwards.

```python
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 5 * 60
```

This is tracked by email, not by IP address or session, which is the
standard "account lockout" shape (it protects one specific account from
being guessed, regardless of how many different machines the attempts come
from) — noted here as a deliberate choice, not an oversight; a
production deployment defending against a *distributed* guessing attack
across many different emails at once would add IP-based limiting as a
second, complementary layer.

**A known, documented limitation**: the failure counts live in a plain
Python dictionary in process memory. That is fine for this single-process
coursework deployment, but it means (a) a restart clears every lockout, and
(b) in the multi-instance redundancy demo (`docs/FAULT_TOLERANCE_DEMO.md`),
each of the three app instances tracks lockouts independently, so an
attacker load-balanced across all three effectively gets 3x the attempts.
This is the same category of limitation that demo's own notes call out for
`_seed_admin()`'s race condition and for SQLite under concurrent writes: a
real multi-instance production deployment would move this state to the
shared database or a cache such as Redis, not keep pretending
single-process assumptions hold once there is more than one process.

**Demoing it live**: on the login page, submit the wrong password for any
account 5 times in a row; the 6th attempt (even with the *correct*
password) is refused with a "too many failed attempts" message and a
countdown. Wait 5 minutes (or use a different account) and it works again.

**Automated proof**: `_devtools/test_unit.py` section 10 exercises the
sliding window directly — locks out after exactly `MAX_LOGIN_ATTEMPTS`
failures, confirms one fewer attempt does *not* lock out, confirms a
different email is unaffected, and confirms a successful login clears it.

## 4. Not in scope for this pass (documented, not silently skipped)

- **`SECRET_KEY`** (`gymflow/config.py`) falls back to a hardcoded
  development value if `GYMFLOW_SECRET_KEY` is not set in the environment.
  This is standard Flask practice for local development and is already
  configurable for a real deployment (see the Docker Compose / PythonAnywhere
  setup, which does set a real one) — left as-is rather than hardening
  further, since changing the default behavior here was not one of the
  three items asked for.
- **CSRF token rotation.** The token is generated once per session and
  reused for every form in that session, rather than a fresh token per
  form submission. This is the standard "synchronizer token" pattern (the
  same one Django and Flask-WTF use by default) and is sufficient to defeat
  cross-site forgery; a rotating, single-use token is a stricter variant
  used mainly for very high-value actions, not needed here.
- **A pre-existing GET-based side effect, found while checking for exactly
  this during the pass (not introduced by it).** `members.py`'s
  `edit_card` route acquires the card's edit lock (Section 3.6, the
  optimistic time-boxed concurrency lock) on a plain `GET` request to
  `/members/<id>/edit`, not on the form's `POST`. Since CSRF protection by
  design only checks state-changing methods (`GET`/`HEAD`/`OPTIONS` are
  assumed side-effect-free — that assumption is what lets a browser safely
  prefetch or revisit a GET), this route is a narrow exception: a
  third-party page (e.g. an `<img src="...">` pointed at that URL) could
  make a logged-in staff member's browser acquire the lock on a card they
  never intended to edit. The impact is minor and self-healing — no data
  is exposed or changed, and the lock releases itself automatically after
  `CARD_LOCK_TIMEOUT_SECONDS` (5 minutes) — but it is a real, if low
  severity, side effect on a GET. Left unchanged in this pass because
  fixing it properly means changing how the edit page itself is entered
  (the lock is acquired specifically so navigating to the edit page is
  what claims it), which is a larger design change than the three items
  this pass was scoped to; flagged here for a deliberate decision later
  rather than fixed as a drive-by change.
