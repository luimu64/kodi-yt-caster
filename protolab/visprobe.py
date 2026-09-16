"""One-shot probe: which window / condvisibility forms work on this Kodi build.

Run with:  kodi-send --action="RunScript(/tmp/visprobe.py)"
Results land in kodi.log as VISPROBE lines.
"""
import xbmc

CONDS = [
    "Window.IsActive(visualisation)",
    "Window.IsActive(12006)",
    "Window.IsActive(fullscreenvideo)",
    "Window.IsActive(12005)",
    "Window.IsActive(home)",
    "Window.IsActive(10000)",
    "Player.IsPlayingVideo",
    "Player.IsPlayingAudio",
    "Player.HasVideo",
    "Player.HasAudio",
    "Window.IsVisible(12005)",
    "Window.IsVisible(12006)",
]


def main():
    for c in CONDS:
        try:
            v = xbmc.getCondVisibility(c)
        except Exception as e:
            v = "ERR:%s" % e
        xbmc.log("VISPROBE cond %-34s = %s" % (c, v), level=1)
    for label in ("System.CurrentWindow", "Player.FilenameAndPath", "Player.FileNameAndPath"):
        try:
            xbmc.log("VISPROBE label %-24s = %s" % (label, xbmc.getInfoLabel(label)), level=1)
        except Exception as e:
            xbmc.log("VISPROBE label %s ERR %s" % (label, e), level=1)


main()
