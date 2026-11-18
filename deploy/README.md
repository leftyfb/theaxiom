# Standard Nginx deployment (Ubuntu)

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
6. Verify unauthenticated GET / returns the public homepage; /admin and
   /api/storage-settings return 401 without credentials; authenticated admin
   opens and settings work; acquisition POST returns 503; service units enabled.
   Verify password rotation using the admin form, including old credentials
   failing, new credentials working, and persistence across a backend restart.
   Do not put real credentials in test transcripts.

Password changes require the current password and confirmation and take effect
without Nginx or backend reload. For recovery stop the backend, preserve the
private existing hash under an operator-controlled backup name, initialize a
new credential as the service account, then restart. Never silently reset a
credential. Browser Basic authentication may cache credentials until the browser
session is closed. Basic authentication over HTTP is **not encrypted**; use this
HTTP example only on a trusted isolated development LAN. Configure Nginx HTTPS
with a trusted certificate before use on untrusted networks. The backend uses
the scheme forwarded by the configured local Nginx proxy when checking Origin.

The example IP and host names are deployment examples, not application defaults.
Ansible may consume these files from the application repository. Keep actual
host inventory, custom firewall rules and secrets outside Git.
