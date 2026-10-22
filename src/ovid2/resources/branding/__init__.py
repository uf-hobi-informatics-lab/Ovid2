"""Branding assets bundled with the Ovid2 wheel.

Assets:

- ``ovid_logo.png`` — 1254 × 1254 master brand mark. Used by the
  architecture PDF generator, email digests, and any consumer that
  wants the raw high-resolution PNG.
- ``ovid_logo.svg`` — resolution-independent version of the mark
  (embedded raster inside an ``<svg viewBox="0 0 256 256">`` wrapper).
  This is the file the web control panel serves as its topbar logo
  and favicon so the browser can never render it at native size.
- ``ovid_logo_32.png`` / ``ovid_logo_64.png`` / ``ovid_logo_128.png`` —
  pre-rendered small variants for legacy consumers that cannot render
  SVG (email clients, some old kiosks).

Downstream subsystems should locate assets via
``importlib.resources.files`` rather than depending on the source
checkout layout.

Example
-------

>>> from importlib.resources import files
>>> logo_bytes = files("ovid2.resources.branding").joinpath("ovid_logo.svg").read_bytes()
"""
