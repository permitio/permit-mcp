# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting: open the
[Security tab](https://github.com/permitio/permit-mcp/security) of this repository and choose
**Report a vulnerability**. Do not open a public issue or pull request for a vulnerability.

Include what you can of:

- the version of permit-mcp, and of mcp and Python;
- how the server runs: the `permit-mcp` command, or embedded in a host, and over which transport;
- the steps to reproduce, and what an attacker gains.

Leave real API keys, tokens and user data out of the report.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x | Yes |
| 0.1 and earlier | No |

Fixes are released in a new 1.x version. To upgrade from 0.1, follow the
[upgrade guide](docs/upgrade-to-1.0.md).
