# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT
"""One deliberate type error, which mypy must report: a check that only passes proves nothing."""

from mcuctl import package_search

package_search.bundle_manifest_url({"repo": "PyDevices/omnipython"}, 1)
