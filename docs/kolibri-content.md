# Kolibri content evaluation (#34)

Status: metadata scope-planning foundation only. The existing catalog placeholder
is deliberately not enabled for downloads or publication. No production content
or service has been acquired.

## Upstream and format

Khan Academy explicitly recommends Learning Equality's Kolibri for disconnected
use: https://support.khanacademy.org/hc/en-us/articles/202487370

The public Kolibri Studio channel listing is
https://studio.learningequality.org/api/public/v1/channels .
Learning Equality announced refreshed Khan Academy channels on 2026-02-13
(14 additions and 12 updates), including US, India and Philippines English
curricula and Spanish, French and other languages:
https://community.learningequality.org/t/khan-academy-channels-in-kolibri-just-got-a-major-refresh/3960 .
The English US channel ID is c9d7f950ab6b5a1199e3d6c10d7f0103, also documented
by Learning Equality support:
https://community.learningequality.org/t/importing-khan-academy-from-kolibri-studio/2137 .

The channel export uses SQLite metadata plus content-addressed files under
content/storage/<first hex digit>/<second hex digit>/<md5>.<extension>.
See https://kolibri-dev.readthedocs.io/en/develop/backend_architecture/content/implementation.html
and the upstream export models:
https://github.com/learningequality/kolibri/blob/develop/kolibri/core/content/base_models.py .
Kolibri's official importer supports explicit node IDs, local disk import,
and channel-version updates:
https://kolibri.readthedocs.io/en/latest/manage/command_line.html .
Current channel metadata size, edition, total bytes, and per-topic rights must
be verified using a real current export before enabling acquisition.

The existing March 2023 English Kiwix ZIM is roughly 168 GiB. OpenZIM's
2026-10-20 maintainer discussion reports no known subject subsets and a
currently unreliable converter:
https://github.com/openzim/zim-requests/issues/1954 .
Do not substitute that entire archive for a scoped download.

## Rights

Khan Academy's video/exercise licensing generally uses CC BY-NC-SA, with
exceptions and required attribution and free-availability notice. Review
per-resource rights and brand use before publication; software licensing
does not grant rights to content or logos.
https://support.khanacademy.org/hc/en-us/articles/202262954
https://support.khanacademy.org/hc/en-us/articles/202263034

## Implementation foundation

The generic `theaxiom.kolibri_scope` module inspects a local exported channel
database read-only, validates the requested channel and explicit selected
nodes, browses the hierarchy, computes deduplicated media byte estimates, and
reports known/missing license metadata. Full channel selection requires explicit
confirmation. The estimates include alternative encodings and supplemental
files, and exclude database and filesystem overhead. This does not yet
authorize downloads. MD5 is a publisher file identifier and integrity check,
not a digital signature; an acquisition implementation must independently
verify origin, size and hashes and record a local SHA-256.

## Remaining integration

Prefer native Kolibri import and rendering for exercises and packaged interactive
content; do not build an untrusted HTML execution environment in Axiom. A
standalone reader for ordinary media can be evaluated against representative
real exports, but a native service may be necessary for full exercise support.
Whichever approach is adopted, integrate with Axiom's verified acquisition
lifecycle, scoped selection and progress, publication gate, storage safeguards,
local search, navigation, update/rollback, and locally licensed artwork.
Keep the operator's selected scope across updates. Exclude unpublished and
unavailable lessons from all reader and search routes. Verify representative
videos and captions offline and do not claim unsupported exercise/transcript
capabilities. Do not install a service or download a full channel on Vancouver
until the integration has passed CI and the guarded deployment process.
