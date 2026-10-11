# Roadmap

What's planned for mpftp. Work here also follows bug reports and requests, so
if something is broken or missing for you, open an
[issue](https://github.com/PyDevices/mpftp/issues).

## Planned

- **Package search.** Find a package without knowing its exact name: one
  search across micropython-lib (`mip`), the CircuitPython bundles (`circup`)
  and the PyDevices index, with install from the results.
- **One-click installers.** Download mpftp for Windows, macOS or Linux with
  its own Python inside, and start it from the Start menu, Dock or launcher,
  with nothing to `pip install` first.
- **Translations.** The browser interface and the extension in your
  language, with English as the fallback for anything not yet translated.

## Smaller things

- **Set the micropython-pydevices checkout from VS Code Settings.** Today the
  Firmware panel finds it from the workspace or `~/.mpftp/config.json`'s
  `buildSystemPath`; an `mpftp.buildSystemPath` setting would sit beside the others.
