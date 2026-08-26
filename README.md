# No Man's Walk

![No Man's Walk](https://djfdm802jwooz.cloudfront.net/static/img/twitch_background_optimized.png)

No Man's Walk is an autonomous journey through *No Man's Sky*. A persistent character called the Walker explores planets on foot while Twitch viewers influence the trip.

Watch live at [twitch.tv/nomanswalk](https://twitch.tv/nomanswalk).

## What it does

The project combines game automation, live state, chat interaction, and stream operations.

- Keeps the Walker moving and recovers from obstacles.
- Turns chat commands and votes into in-game actions.
- Reads the current planet, galaxy, environment, and player state.
- Teleports to chosen or random planets across galaxies.
- Automates selfies, social posts, startup, recovery, and shutdown.

## How it works

No Man's Walk has four main layers:

| Layer | Role |
|---|---|
| Game instrumentation | [`nmspy_mods/`](nmspy_mods/) reads and changes live game state through NMSpy and pyMHF. |
| Walker control | [`nms_bot.py`](nms_bot.py) handles input, movement, recovery, cameras, and teleportation. |
| Audience interaction | [`nms_twitch_bot.py`](nms_twitch_bot.py) manages commands, votes, selfies, and scheduling. |
| Operations | [`start_no_mans_walk.py`](start_no_mans_walk.py) coordinates startup and supporting services. |

[`dev_server.py`](dev_server.py) exposes the same commands through a local browser interface.

## How to run locally

This project requires Windows, Steam, *No Man's Sky*, Git, and Python 3.10. Game updates may require matching NMSpy or mod updates.

### 1. Install dependencies

From PowerShell in the repository root:

```powershell
py -3.10 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Configure the project

Create an ignored `parameters.json` file:

```json
{
  "AUTHORIZED_USERS": []
}
```

Then configure pyMHF:

```powershell
pymhf config nmspy
```

Use Steam game ID `275850` and the absolute path to this repository's `nmspy_mods` directory.

### 3. Run the dev server

```powershell
python start_no_mans_walk.py --mode dev
```

This launches the dev server and modded game. Open [localhost:5050](http://localhost:5050) to send commands and view feedback.

If the modded game is already running, start only the server with:

```powershell
python dev_server.py
```

## Development notes

- Runtime files, credentials, screenshots, caches, and logs are ignored by Git.
- Game controls require a running, focused *No Man's Sky* window.
- Review memory hooks after major game or NMSpy updates.
