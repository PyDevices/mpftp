"""Sphinx configuration for the mpftp documentation.

Everything shared by the PyDevices documentation projects (theme, top bar,
Markdown setup, the reference map) comes from pydevices-docs. mpftp is a tool,
not a library, so there is no API reference to generate.
"""

from pydevices_docs import configure

globals().update(configure(
    "mpftp", __file__,
    repo="PyDevices/mpftp", edit_path="docs",
    version_file="../VERSION",
))


# index.md is plain Markdown so the docs read on GitHub too; the site
# navigation lives in contents.md.
master_doc = "contents"
