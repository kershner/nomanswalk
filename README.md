# No Man's Walk

![No Man's Walk](https://djfdm802jwooz.cloudfront.net/static/img/twitch_background_optimized.png)

No Man's Walk is an interactive Twitch stream that autonomously documents the infinite universe of No Man's Sky.

[https://twitch.tv/nomanswalk](https://twitch.tv/nomanswalk)

---

The code is written entirely in Python and consists of several modules:

| Module | Description |
|---|---|
| [`nms_bot.py`](https://github.com/kershner/nomanswalk/blob/master/nms_bot.py) | Provides a direct interfacing to the running No Man's Sky process. Sends keyboard/mouse input, logs game state, checks if the Walker is stuck. |
| [`nms_twitch_bot.py`](https://github.com/kershner/nomanswalk/blob/master/nms_twitch_bot.py) | Twitch interface. Passes command input from chat to nms_bot.py and manages timers for loading random planets, Bluesky posts, daily shutdown. |
| [`nms_bluesky.py`](https://github.com/kershner/nomanswalk/blob/master/nms_bluesky.py) | Bluesky interface for posting Twitch clips and maintaining "Live" status. |
| [`dev_server.py`](https://github.com/kershner/nomanswalk/blob/master/dev_server.py) | Basic Flask server for local nms_bot.py development. |
| [`start_no_mans_walk.py`](https://github.com/kershner/nomanswalk/blob/master/start_no_mans_walk.py) | Handles the boot sequence - opening Steam/modded NMS, starting the OBS stream, launching the Twitch bot. |

---

### [`nmspy_mods/`](https://github.com/kershner/nomanswalk/tree/master/nmspy_mods)
These are direct No Man's Sky game mods I've written with [nmspy.py](https://github.com/monkeyman192/NMS.py).

| Mod | Description |
|---|---|
| [`hud_toggle.py`](https://github.com/kershner/nomanswalk/blob/master/nmspy_mods/hud_toggle.py) | Toggle HUD with a keypress. Uses a clumsy "settings fingerprint" to locate the right HUD toggle in memory. |
| [`music_toggle.py`](https://github.com/kershner/nomanswalk/blob/master/nmspy_mods/music_toggle.py) | Allows muting/unmuting the music track with a keypress. |
| [`state_logger.py`](https://github.com/kershner/nomanswalk/blob/master/nmspy_mods/state_logger.py) | Dumps info about current planet/player state to a local JSON file. |
| [`teleporter.py`](https://github.com/kershner/nomanswalk/blob/master/nmspy_mods/teleporter.py) | Randomizes in-memory planet coordinates and then forces a local reload, like going through a portal. |
