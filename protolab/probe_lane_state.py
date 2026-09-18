import xbmc


def L(k, v):
    xbmc.log("YTCAST-PROBE3 %s=%s" % (k, v), xbmc.LOGINFO)


p = xbmc.Player()
for name, fn in (("isPlaying", p.isPlaying), ("isPlayingAudio", p.isPlayingAudio), ("isPlayingVideo", p.isPlayingVideo)):
    try:
        L(name, fn())
    except Exception as e:
        L(name, "ERR %s" % e)
for w in ("fullscreenvideo", "visualisation", "home"):
    try:
        L("win." + w, xbmc.getCondVisibility("Window.IsActive(%s)" % w))
    except Exception as e:
        L("win." + w, "ERR %s" % e)
for lab in ("Player.FileNameAndPath", "Player.Title"):
    try:
        L("label." + lab, xbmc.getInfoLabel(lab))
    except Exception as e:
        L("label." + lab, "ERR %s" % e)
L("done", "1")
