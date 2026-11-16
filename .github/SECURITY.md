# Security policy

## Supported versions

Only the latest minor release published on [PyPI](https://pypi.org/project/wsbuilder/)
receives security fixes. `wsbuilder` is still *Alpha*: upgrade to the newest
release before reporting, in case the problem is already fixed.

| Version | Supported |
| --- | --- |
| Latest minor (`X.Y.*`) | Yes |
| Older releases | No |

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a vulnerability.**

Report it privately through GitHub:
<https://github.com/jorgelsc-dev/wsbuilder/security/advisories/new>
(*Security* tab → *Report a vulnerability*).

Please include:

- the affected version (`python -c "import wsbuilder; print(wsbuilder.__version__)"`);
- the component (HTTP/1.1, HTTP/2, HTTP/3/QUIC, WebSocket, DNS, ProxyI, ORM, PKI, ...);
- a minimal reproduction or proof of concept, and the impact you observed;
- any configuration needed to trigger it (TLS, limits, enabled features).

Keep the report minimal until it has been triaged, and do not test against
deployments you do not own.

## What to expect

- Acknowledgement within **7 days**.
- An assessment (accepted, needs more information, or not a vulnerability)
  within **14 days**.
- For accepted reports, a fix released on PyPI as soon as practical, usually
  within **30 days**, followed by a published GitHub Security Advisory (with a
  CVE when one applies) that credits the reporter unless they prefer otherwise.
