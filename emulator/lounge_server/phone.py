"""Phone: a fake YouTube app (sender) driving the receiver through the mock Lounge."""
import threading
import time


class Phone:
    """Queues Lounge commands the way the real app (via Google's relay) does.

    A real sender talks to ONE lounge session (the one its pairing code
    registered); the cl and m listeners are separate lounges. Commands go to
    the first bound session unless retargeted.
    """

    def __init__(self, server, session_index=0):
        self.server = server
        self.name = "Pixel 8"
        self._target = None  # explicit sid list override
        # The queue identity the real app establishes on first cast and
        # re-sends verbatim on every subsequent command. Stable per phone.
        self.list_id = "PLemulator0000000000000000000001"

    def target(self, *sids):
        self._target = list(sids)
        return self

    def _sids(self):
        if self._target is not None:
            return self._target
        return None  # server picks the actively-polled session

    def _send(self, cmd, data=None):
        item = [cmd, data] if data is not None else [cmd]
        self.server.queue_command(item, self._sids())

    def connect(self, name=None):
        if name:
            self.name = name
        self._send("remoteConnected", {"id": 1, "name": self.name, "type": "phone"})

    def disconnect(self):
        self._send("remoteDisconnected", {"id": 1, "name": self.name, "type": "phone"})

    def set_playlist(self, video_id, video_ids, current_time=0, context="playlist", list_id=None):
        # Real traffic ALWAYS carries listId: the phone keys its whole player
        # model on it, and rejects any nowPlaying report whose videoId is not
        # attached to a listId it already knows. Omitting it from the fake
        # sender hid the desync bug (app stuck on the old track after a
        # TV-side advance), so default to a stable one.
        self._send("setPlaylist", {
            "videoId": video_id,
            "videoIds": ",".join(video_ids) if isinstance(video_ids, (list, tuple)) else str(video_ids),
            "currentTime": str(int(current_time)),
            "context": context,
            "listId": list_id or self.list_id,
        })

    def update_playlist(self, video_ids):
        self._send("updatePlaylist", {
            "videoIds": ",".join(video_ids) if isinstance(video_ids, (list, tuple)) else str(video_ids),
        })

    def play(self):
        self._send("playVideo")

    def pause(self):
        self._send("pause")

    def stop(self):
        self._send("stopVideo")

    def seek(self, new_time):
        self._send("seekTo", {"newTime": str(int(new_time))})

    def set_volume(self, volume):
        self._send("setVolume", {"volume": int(volume)})

    def get_volume(self):
        self._send("getVolume")

    def get_now_playing(self):
        self._send("getNowPlaying")

    def burst_set_playlist(self, video_id, video_ids, current_time=0, n=3, gap=0.03):
        """Relay-duplicate simulation: the real backend redelivers the same
        setPlaylist 2-5x within ~100ms. Commands carry the SAME code so the
        receiver's last_code dedup applies only if the app reuses it — the
        real relay bumps the code per delivery, so we do too."""
        for _ in range(n):
            self.set_playlist(video_id, video_ids, current_time)
            if gap:
                time.sleep(gap)

    # --- receiver-visible state (assertion surface) --------------------------
    def now_playing_reports(self):
        return self.server.reports("nowPlaying")

    def now_playing_playlist_reports(self):
        return self.server.reports("nowPlayingPlaylist")

    def state_reports(self):
        return self.server.reports("onStateChange")

    def volume_reports(self):
        return self.server.reports("onVolumeChanged")
