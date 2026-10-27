# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org).

The Windows installer for the desktop app, and the app inside it, will be
signed. SignPath signs Windows only, so the Linux `.deb` and the other
downloads (the Python package and the VS Code extension) are not signed by it.
Each signed file is built from this repository by GitHub Actions on
GitHub-hosted runners, and signed only after a maintainer approves that
release. Nothing built on a personal machine is signed.

## Team roles

- Committers and reviewers: [@bdbarnett](https://github.com/bdbarnett)
- Approvers: [@bdbarnett](https://github.com/bdbarnett)

Committers may change the code without further review. Every pull request
from anyone else is reviewed by a committer before it merges. Approvers
decide whether a release is signed, one release at a time.

Everyone in these roles uses multi-factor authentication on GitHub and on
SignPath.

## Privacy

This program will not transfer any information to other networked systems
unless specifically requested by the user or the person installing or
operating it.

When you ask it to, it contacts: micropython.org and GitHub to download
firmware, the package indexes you search or install from (micropython-lib,
the PyDevices index, the CircuitPython bundles, PyPI), and boards on your own
network over WebREPL. Those services' own privacy policies apply to those
requests. It sends no telemetry or usage data.
