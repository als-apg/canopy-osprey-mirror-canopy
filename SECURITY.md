# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately. Don't open a public issue,
pull request or discussion for them.

Use GitHub's private vulnerability reporting: open the repository's
[Security tab](https://github.com/slaclab/canopy/security) and choose
**Report a vulnerability**. Only the maintainers can see the report.

A useful report says:

- which version you ran (image tag, or the commit if you built from source);
- which modules and API-key scopes are involved;
- the steps to reproduce it and what you expected to happen;
- the impact as you understand it.

The maintainers will acknowledge the report, work with you on a fix and agree a
disclosure date with you. Tell us if you want to be credited.

## Supported versions

Canopy is pre-1.0 and under active development. Security fixes are made on
`main` and published as a new `ghcr.io/slaclab/canopy` image; they are not
backported to older tags. Run a current image.

## Notes for operators

- Authentication is by API key; see
  [API keys](docs/deployment/api-keys.md) for scopes and how to mint, rotate
  and revoke them.
- The `dev` key (`CANOPY_API_KEYS="dev:read+write@*"`) in the README, the
  example `.env` files and `docker/docker-compose.yml` is for local development
  only. It grants read and write on every module. Don't run that configuration
  anywhere other people can reach.
- Hare can write to PVs. Give keys the narrowest scope and module list that
  works, and keep Canopy off networks you don't control.
- Report a vulnerability in a dependency to that project. Tell us too if
  Canopy's usage of it makes the impact worse.
