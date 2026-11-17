# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT
"""What a user writes against the installed package: mypy must accept it."""

from mcuctl import config, package_search

url: str = package_search.bundle_manifest_url({"repo": "PyDevices/omnipython"}, "v1.0.0")
settings: dict[str, object] = config.load()
