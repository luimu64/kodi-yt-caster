# Task List: Emulator Bug Fixes & Performance Enhancements

Identified and reproduced via the Kodi emulator test rig (`emulator/`).

---

## Bugs (Confirmed with Emulator Scenarios)

- [ ] **1. Fix `stop()` while resolving starting playback anyway**
  - **Issue:** If phone sends `stop` while video resolution/startup is in flight, `self._kodi_player.isPlaying()` is `False`, so `stop()` hits the `else` branch without bumping `_play_gen` or clearing `_requested_id`. Once resolved, `gen == self._play_gen` still matches, so Kodi starts playback despite explicit stop. In addition, `_requested_id` remains stale, blocking future casts of the same video.
  - **Location:** `resources/lib/player_bridge.py:609-617` (`PlayerBridge.stop`)
  - **Fix:** Inside `stop()`, acquire `self._lock`, increment `self._play_gen += 1`, and clear `self._requested_id = None`.
  - **Regression test:** Add `test_stop_while_resolving` in `emulator/scenarios/test_remote_control.py`.

- [ ] **2. Fix seek-while-paused dropping state updates to Lounge**
  - **Issue:** In Kodi, `isPlaying()` returns `True` when paused. `seek_to()` branches on `self._kodi_player.isPlaying()` and only calls `seekTime(seconds)`, omitting `report_state_change()`. While playing, `_position_loop` normally sends updates, but `_position_loop` only runs when `state == PLAYING`. Seeking while paused therefore never sends an update to Lounge, leaving the phone seekbar frozen or desynced.
  - **Location:** `resources/lib/player_bridge.py:619-629` (`PlayerBridge.seek_to`)
  - **Fix:** Always report state and position to `self.sessions` in `seek_to()`, regardless of `isPlaying()`.
  - **Regression test:** Add `test_seek_while_paused_reports` in `emulator/scenarios/test_remote_control.py`.

- [ ] **3. Fix chapter / replay seek on same video dropped by `setPlaylist` dedup**
  - **Issue:** `setPlaylist` deduplication checks `if target_id == self._requested_id: return` before inspecting `current_time`. When a user taps a chapter marker, timestamp link, or replay on the currently playing video, `target_id == self._requested_id` evaluates to `True` and exits immediately, silently ignoring the seek request.
  - **Location:** `resources/lib/player_bridge.py:159-166` (`PlayerBridge.set_playlist`)
  - **Fix:** Before early returning on duplicate `target_id`, check `if current_time > 0: self.seek_to(current_time); return`.
  - **Regression test:** Add `test_chapter_seek_same_video` in `emulator/scenarios/test_remote_control.py`.

- [ ] **4. Fix `SeekRetry` background thread corrupting subsequent track playback**
  - **Issue:** When a video starts with a seek offset, `_apply_seek` retries `seekTime(seek_target)` up to 5 times over 2 seconds in a background thread. It has no generation check. If the user rapidly skips to the next track, `SeekRetry` continues running and applies the previous track's seek offset to the newly started video.
  - **Location:** `resources/lib/player_bridge.py:694-712` (`PlayerBridge._on_playback_started`)
  - **Fix:** Capture `gen = self._play_gen` at spawn; exit `_apply_seek` immediately if `self._play_gen != gen`.
  - **Regression test:** Add `test_rapid_skip_ignores_stale_seek_retry` in `emulator/scenarios/test_remote_control.py`.

- [ ] **5. Fix reversed arguments in `PlayerBridge._notify` calls**
  - **Issue:** `_notify` signature is `(message, title="YouTube Cast", error=False)` and delegates to `Dialog().notification(title, message, ...)`. Call sites pass `"YouTube Cast"` as the first positional argument, causing notification headers and body text to be swapped in Kodi UI.
  - **Location:** `resources/lib/player_bridge.py:195, 228, 253, 275`
  - **Fix:** Align method signature to `(title="YouTube Cast", message="", error=False)` or fix call sites.

- [ ] **6. Guard UI dialog calls against holding `self._lock` (deadlock prevention)**
  - **Issue:** `_play_video` invokes `xbmcgui.Dialog().notification()` while holding `self._lock`. If Kodi main thread is concurrently dispatching a player callback waiting for `_lock`, an AB/BA deadlock can freeze Kodi.
  - **Location:** `resources/lib/player_bridge.py:235-241`
  - **Fix:** Perform notification calls outside `with self._lock:`.

---

## Performance Enhancements

- [ ] **7. Persistent HTTP keep-alive connection in `LoungeSession._do_post`**
  - **Impact:** High (eliminates ~3,600 TLS 1.3 handshakes/hr on low-power devices).
  - **Hot path:** `_position_loop` calls `report_now_playing` every 2s across both YouTube and YouTube Music sessions (~1 request/sec).
  - **Location:** `resources/lib/lounge/session.py:231-236`
  - **Enhancement:** Maintain a persistent `http.client.HTTPSConnection("www.youtube.com")` per `LoungeSession` (reconnecting on socket error / `RemoteDisconnected`) instead of `urllib.request.urlopen`.

- [ ] **8. Decouple background prefetch lock from interactive user playback**
  - **Impact:** High (eliminates 1–3s interactive playback latency spikes on manual skip/pick).
  - **Hot path:** `_kick_prefetch` resolves next queue track in background while current track plays; holds `_INSTANCE_LOCK` in `ytdlp_inproc.py:88-100`.
  - **Location:** `resources/lib/ytdlp_inproc.py:88-100` & `resources/lib/player_bridge.py:220-228`
  - **Enhancement:** Separate locks/instances for foreground interactive resolves vs speculative prefetch, or check if foreground resolve is waiting and abort speculative prefetch.

- [ ] **9. Bound memory and cache growth across long 24/7 uptime**
  - **Impact:** Medium-High (prevents unbounded memory growth on long-running Kodi boxes).
  - **Hot path:** Video resolve caches and HLS/DASH manifest stores accumulate keys indefinitely.
  - **Locations:**
    - `resources/lib/manifest_server.py:18` (`_MANIFESTS`)
    - `resources/lib/resolver.py:16-31` (`VideoResolver._cache`)
    - `resources/lib/player_bridge.py:397` (`_queue_titles` and `_queue_titles_inflight`)
  - **Enhancement:** Cap dictionaries with LRU or max size (e.g. 50 entries) and purge expired items on insert.

- [ ] **10. Eliminate redundant addon settings IPC and JSON decoding in service loop**
  - **Impact:** Medium (reduces CPU cycles in 2-second idle service tick).
  - **Hot path:** Main wait loop checks `store.load()` up to 3 times per 2-second tick.
  - **Location:** `service.py:448-454`
  - **Enhancement:** Read `store.load()` once per tick into a local variable.
