# Synthetic EasyEDA Pro-style BOMs

These are hand-authored CSV fixtures, not exports from a real project. They are
only parsed; do not fabricate a board or order parts from them. The `C99000000x`
supplier identifiers are fictional. The two MPNs in `good.csv` refer to the
project's existing illustrative part-risk dataset, not current distributor data.

- `good.csv`: PASS, exit 0, only the existing `BOM_ONLY_MODE` note. R1/R2 are a
  grouped row; C1 has no MPN but has a supplier identifier explicitly assigned
  to LCSC. D1 has a different supplier, so its Supplier Part is not an LCSC ID.
- `broken.csv`: CAUTION, exit 1, `BOM_MISSING_MPN` plus `BOM_ONLY_MODE`. C1 has
  no MPN, and its supplier is not LCSC. Even a C-shaped supplier identifier
  must not make that row look identified by LCSC.

Run either fixture as a standalone BOM:

```console
python3 -m preflight samples/easyeda-pro/good.csv --format json
python3 -m preflight samples/easyeda-pro/broken.csv --format json
```

## Header and mapping decisions

The first-party [EasyEDA Pro BOM example](https://prodocs.easyeda.com/en/api/guide/cli.html#ix-exporting-the-bom)
shows the ten-column Pro layout used here. Most headers already match existing
aliases. This change records Supplier and Supplier Part separately: only an
exact `LCSC` supplier name, ignoring case and surrounding whitespace, supplies
the LCSC field. Missing, other or uncertain supplier names do not. An existing
explicit LCSC column keeps precedence, including an empty value, preserving
the old column selection behavior.

The vendor example's BOM text uses tab separators. Tab-separated input is
recognized only when its header contains the complete documented Pro set;
comma-separated input keeps its existing parsing. Generic TSV auto-detection
and legacy Components/Name aliases are outside this change, as agreed in
[issue #4](https://github.com/RakTikoo/kicad-preflight/issues/4#issuecomment-6804098459).

This is a BOM header/supplier mapping change. No KiCad file-format code, text,
electrical rule or numerical geometry is copied or changed.
