# Standard Nginx deployment (Ubuntu)

**CURRENT DEPLOYMENT HOLD:** Vancouver has an active Ready.gov download. These instructions are for future operator-approved work only. Do not run them, restart services, or touch storage until the download has completed and a separate deployment authorization is given. See [the deployment acceptance register](../docs/deployment-acceptance-register.md).


Nginx owns public HTTP delivery. The Python backend runs as an unprivileged,
loopback-only process; it owns admin authentication and password rotation.
The public homepage is static and requires no login. No Kiwix or acquisition
worker is configured in this initial deployment.

## Operator setup

Use a dedicated `theaxiom` service account and a root-owned application checkout
at `/opt/theaxiom/app` with a Python virtual environment installed from the
repository. The service account needs read access to the application and write
access only to its private application state (and eventually separately
authorized content storage). Do not change ownership of an existing NFS export
or create content directories during deployment.

1. Install Nginx and Python prerequisites using the host package manager. Install
   the application into `/opt/theaxiom/app/.venv` using `pip install -e .`.
2. Create `/var/lib/theaxiom` owned by the service account, mode 0700. From a
   trusted local terminal initialize its credential file (interactive, never
   put the password in a command line, Git, issue or log):
   `sudo -u theaxiom /opt/theaxiom/app/.venv/bin/python -m theaxiom.admin_security init /var/lib/theaxiom/admin-credential.json`.
   The command refuses to overwrite an existing credential.
3. Copy `server.env.example` to `/etc/theaxiom/server.env`, root-owned, and
   replace the sample host names with the exact LAN DNS name/IP. No passwords
   are kept in the environment. The credential file is a private salted hash.
4. Install `theaxiom.service` in `/etc/systemd/system/`, run
   `systemctl daemon-reload` and `systemctl enable --now theaxiom.service`.
   Verify only 127.0.0.1:8080 is listening.
5. Install the two Nginx files from `nginx/` as indicated by their comments.
   Replace the example LAN address and server names in the site with the actual
   values. Enable that site, resolve conflicting default/listening sites only
   with operator approval, run `nginx -t`, then enable/start or reload Nginx.
   Bind only the intended interface and restrict access with existing firewall
   policy; do not expose publicly or forward ports.
6. Verify unauthenticated GET / returns the public homepage, /admin redirects
   to the branded /admin/login page without a browser-native Basic prompt, and
   /api/storage-settings denies anonymous requests. Test the HTML sign-in form,
   HttpOnly/SameSite browser session cookie, logout, bounded session expiry,
   CSRF rejection and password-rotation session invalidation. Confirm explicit
   Basic Authorization still works for API clients. Never put real credentials
   or session cookies in test transcripts. Verify the existing content and
   acquisition safeguards after any auth change.

Password changes require the current password and confirmation and take effect
without Nginx or backend reload. For recovery stop the backend, preserve the
private existing hash under an operator-controlled backup name, initialize a
new credential as the service account, then restart. Never silently reset a
credential. Browser sessions use random, in-memory server-side tokens with a
30-minute idle limit and an absolute eight-hour limit; cookie values do not
contain passwords. The backend checks strict same-origin requests and session
CSRF tokens for mutations; Nginx forwards only its dedicated theaxiom_session
cookie to the trusted backend, never to Kiwix. Existing clients may continue
to send HTTP Basic Authorization explicitly; their browser-native credential
dialogs are no longer needed for ordinary interactive administration.
HTTP **does not encrypt passwords, Basic credentials or session cookies**; use
this HTTP example only on a trusted isolated development LAN. Configure Nginx
HTTPS with a trusted certificate before use on untrusted networks. The backend
uses the trusted proxy scheme for Origin checks and sets Secure on session
cookies when accessed over HTTPS. Live Chrome/1Password save-and-fill behavior
and naming still require owner acceptance.

The example IP and host names are deployment examples, not application defaults.
Ansible may consume these files from the application repository. Keep actual
host inventory, custom firewall rules and secrets outside Git.

## External acquisition monitoring

The admin page polls GET /api/acquisitions every two seconds. A single
background sampler queries the allowlisted acquire-wikipedia-zim.service
and file metadata, never reads archive content or controls the service.
The initial adapter is specific to the existing Wikipedia service at
/media/offline; other configurations report unavailable or unknown.
Completion requires successful current-invocation journal evidence, not
file size alone. After a reboot or journal rotation, verification evidence
may be unavailable; the UI must not claim verification. Rate samples are
in memory and rebuild after backend restart. A hard NFS hang may stall
the sampler; the endpoint still returns promptly with stale status.
The independent storage-readiness endpoint is unchanged.
Acquisition POST remains disabled.

Preserve any existing theaxiom UID, primary group, supplementary groups,
and local state ownership during updates. For NFS AUTH_SYS, configure
a dedicated server-side identity with appropriate share permissions and
match its numeric UID on the application host; check group requirements
and test access as the service account. Do not recursively chmod/chown
the share or map all clients to an administrator.
