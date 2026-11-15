# Synthetic legacy EasyEDA-style BOMs

These hand-authored CSV fixtures use the Components and Name aliases scoped
by the maintainer in issue #6 and the discussion of issue #4. They are not real
project exports. Parse them only; do not fabricate a board or order parts.
The supplier identifiers are fictional.

- good.csv: PASS; a missing manufacturer part is identified by an explicitly
  LCSC supplier, using the same supplier boundary as Pro exports.
- broken.csv: CAUTION; a non-LCSC supplier identifier cannot fill the LCSC field.

Components supplies references; Name supplies the value. Package already
maps to footprint. Existing explicit LCSC columns retain precedence, and
the Pro-only TSV recognition remains unchanged. These fixtures are CSV,
not a claim to support every historical export encoding or delimiter.

The separate legacy scope and mappings were agreed in:
https://github.com/RakTikoo/kicad-preflight/issues/4#issuecomment-6804098459
