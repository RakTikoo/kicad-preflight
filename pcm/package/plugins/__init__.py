"""KiCad Preflight — Action Plugin entry point (PCM package).

This is the in-KiCad front door for the same checks engine that powers
the CLI (the `preflight` package, copied next to this file at package
build time by ../build_pcm.py). One engine, two front doors: the check
logic lives in `preflight/` and is pure Python standard library; this
file only adapts it to KiCad's Action Plugin API:

  * a class derived from `pcbnew.ActionPlugin`,
  * `defaults()` sets name/category/description/icon,
  * `Run()` is called when the user picks Tools -> External Plugins ->
    KiCad Preflight (or the toolbar button),
  * `SimplePlugin().register()` at import time (done at the bottom).

Per the official KiCad developer docs (PCB Python Bindings):
https://dev-docs.KiCad.org/en/apis-and-binding/pcbnew/index.html

STATUS: RUN INSIDE KICAD (probe 2026-11-10). The built package was
installed PCM-style and Run() executed inside a real KiCad 8.0.9 PCB
Editor (on a VM under a virtual display), producing the report dialog
with live findings on a real board; register() executes cleanly
in-app. Not yet verified: automatic discovery by a stock desktop PCM
install (the menu entry appearing after a Plugin and Content Manager
install). Status detail in ../README.md. The engine it calls is fully
tested headless via the CLI and the repo test suites.
"""

import os

try:
    import pcbnew
except ImportError:  # imported outside KiCad (e.g. by the test suites)
    pcbnew = None


if pcbnew is not None:

    class PreflightAction(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "KiCad Preflight"
            self.category = "Check before ordering"
            self.description = (
                "Pre-fab checks for the open project: schematic/PCB "
                "agreement, BOM sanity, and part risk — one report "
                "before you order.")
            self.show_toolbar_button = True
            self.icon_file_name = os.path.join(
                os.path.dirname(__file__), "icon.png")

        def Run(self):
            import wx

            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("No board is open.", "KiCad Preflight",
                              wx.OK | wx.ICON_WARNING)
                return
            project_dir = os.path.dirname(board.GetFileName())
            # The engine is bundled next to this file at build time.
            from preflight import engine
            from preflight.report import render_text

            report = engine.run(project_dir)
            text = render_text(report)
            dlg = wx.Dialog(None, title="KiCad Preflight — report",
                            size=(760, 560))
            sizer = wx.BoxSizer(wx.VERTICAL)
            sizer.Add(wx.TextCtrl(dlg, value=text,
                                  style=wx.TE_MULTILINE | wx.TE_READONLY
                                  | wx.HSCROLL),
                      1, wx.EXPAND | wx.ALL, 8)
            dlg.SetSizer(sizer)
            dlg.ShowModal()
            dlg.Destroy()

    PreflightAction().register()
