# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""romfs takes -d and --baud before or after its subcommand."""

import unittest

from mpftp import cli


class RomfsDeviceOptionTests(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(["romfs", *argv])

    def test_device_before_and_after_deploy(self):
        for argv in (
            ("-d", "COM115", "deploy", "image.romfs"),
            ("deploy", "-d", "COM115", "image.romfs"),
            ("deploy", "image.romfs", "-d", "COM115"),
        ):
            ns = self.parse(*argv)
            self.assertEqual((ns.device, ns.romfs_cmd, ns.path), ("COM115", "deploy", "image.romfs"), argv)

    def test_build_and_query_take_the_device_after(self):
        ns = self.parse("build", "-d", "COM1", "src", "-o", "out.romfs", "--partition", "1")
        self.assertEqual((ns.device, ns.path, ns.output, ns.partition), ("COM1", "src", "out.romfs", 1))
        self.assertEqual(self.parse("query", "-d", "COM1").device, "COM1")
        self.assertIsNone(self.parse("query").device)


if __name__ == "__main__":
    unittest.main()
