# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""Sphinx configuration for the mcuctl documentation.

Everything shared by the OmniPython documentation projects (theme, top bar,
Markdown setup, the reference map) comes from omnipython-docs. mcuctl is a tool,
not a library, so there is no API reference to generate.
"""

from omnipython_docs import configure

globals().update(configure(
    "mcuctl", __file__,
    repo="PyDevices/mcuctl", edit_path="docs",
    version_file="../VERSION",
))


# index.md is plain Markdown so the docs read on GitHub too; the site
# navigation lives in contents.md.
master_doc = "contents"
