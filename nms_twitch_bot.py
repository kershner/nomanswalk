from nms_bot import COMMANDS, start_state_poller, left_click, is_planet_loading
from twitchio.ext.commands.errors import CommandNotFound
from utils import log, get_status_text
from dataclasses import dataclass
from twitchio.ext import commands
from typing import Optional
import nms_bluesky
import aiohttp
import requests
import asyncio
import json
import time
import os


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
class Config:
    PARAMS_FILE = "parameters.json"
    TOKENS_FILE = "oauth_tokens.json"

    TWITCH_CHANNEL = "nomanswalk"

    CHAT_DELAY = 1.5
    VOTING_DURATION = 20

    TOKEN_REFRESH_SKEW_S = 60  # refresh ~1 minute before expiry

    SCHEDULED_COMMANDS = [
        (20 * 60, "_do_help"),    # !help  every 20 minutes
        (30 * 60, "_do_status"),  # !status every 30 minutes
    ]
    
    STREAM_TAGS = [
        "Exploration",
        "Automation",
        "Chill",
        "Cozy",
        "Interactive",
        "PC",
        "Programming",
        "Casual",
        "Relaxing"
    ]

    ADMIN_ONLY_COMMANDS = {
        "teleport",
    }

    VOTABLE_COMMANDS = {
        "camera",
        "coords"
    }

    CLIP_POST_DELAY_MINUTES = 120

    _params: Optional[dict] = None

    @classmethod
    def load_params(cls) -> dict:
        if cls._params is None:
            if not os.path.exists(cls.PARAMS_FILE):
                log(f"Missing {cls.PARAMS_FILE}")
                raise SystemExit(1)
            with open(cls.PARAMS_FILE, "r", encoding="utf-8") as f:
                cls._params = json.load(f)
        return cls._params

    @classmethod
    def get_client_id(cls) -> str:
        return str(cls.load_params().get("CLIENT_ID", "")).strip()

    @classmethod
    def get_client_secret(cls) -> str:
        return str(cls.load_params().get("CLIENT_SECRET", "")).strip()

    @classmethod
    def get_admin_users(cls):
        params = cls.load_params()
        users = params.get("AUTHORIZED_USERS") or params.get("ADMIN_USERS") or []
        users = [str(u).lower() for u in users if u]
        defaults = {cls.TWITCH_CHANNEL}
        return sorted(set(users) | defaults)

# ─────────────────────────────────────────────────────────────
# OAUTH TOKENS
# ─────────────────────────────────────────────────────────────
class OAuthTokens:
    def __init__(self, client_id: str, client_secret: str, tokens_file: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens_file = tokens_file

        if not self.client_id or not self.client_secret:
            raise SystemExit("parameters.json must include CLIENT_ID and CLIENT_SECRET")

    def load(self) -> dict:
        if not os.path.exists(self.tokens_file):
            raise SystemExit(f"Missing {self.tokens_file} (run twitch_oauth_helper.py once)")
        with open(self.tokens_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, tokens: dict) -> None:
        with open(self.tokens_file, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, sort_keys=True)

    def _refresh(self, refresh_token: str) -> dict:
        url = "https://id.twitch.tv/oauth2/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        r = requests.post(url, data=data, timeout=20)
        r.raise_for_status()
        payload = r.json()

        access_token = payload.get("access_token", "")
        new_refresh = payload.get("refresh_token") or refresh_token
        expires_in = int(payload.get("expires_in") or 0)

        if not access_token or not expires_in:
            raise RuntimeError(f"Unexpected refresh payload: {payload}")

        return {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "expires_at": int(time.time()) + expires_in,
            "scopes": payload.get("scope") or payload.get("scopes") or [],
            "token_type": payload.get("token_type") or "bearer",
        }

    def ensure_fresh(self) -> dict:
        tokens = self.load()
        expires_at = int(tokens.get("expires_at") or 0)
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            raise SystemExit(f"{self.tokens_file} missing refresh_token (re-run twitch_oauth_helper.py)")

        if expires_at and time.time() < (expires_at - Config.TOKEN_REFRESH_SKEW_S):
            return tokens

        log("Refreshing Twitch access token...")
        new_tokens = self._refresh(refresh_token)
        self.save(new_tokens)
        return new_tokens


# ─────────────────────────────────────────────────────────────
# VOTING
# ─────────────────────────────────────────────────────────────
@dataclass
class VoteState:
    active: bool = False
    cmd_name: str = ""
    args_raw: str = ""
    yes: set[str] = None
    no: set[str] = None
    task: Optional[asyncio.Task] = None

    def reset(self):
        self.active = False
        self.cmd_name = ""
        self.args_raw = ""
        self.yes = set()
        self.no = set()
        self.task = None


# ─────────────────────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────────────────────
class NMSBot(commands.Bot):
    def __init__(self):
        self._admin_users = set(Config.get_admin_users())

        self._vote = VoteState()
        self._vote.reset()

        self._cmd_queue: asyncio.Queue[tuple[str, list[str]]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._executing = False

        self._tokens = OAuthTokens(Config.get_client_id(), Config.get_client_secret(), Config.TOKENS_FILE)
        tokens = self._tokens.ensure_fresh()
        self._access_token = str(tokens.get("access_token") or "").strip()

        self._bsky = None
        self._clip_task: Optional[asyncio.Task] = None

        self._teleport_interval_s = 6 * 3600  # 6 hours
        self._next_teleport_time: float = time.time() + self._teleport_interval_s
        self._teleport_loop_task: Optional[asyncio.Task] = None

        super().__init__(
            token=self._access_token,
            prefix="!",
            initial_channels=[Config.TWITCH_CHANNEL],
        )

        try:
            self._bsky = nms_bluesky.login()
            log("Bluesky logged in.")
        except Exception as e:
            log(f"Bluesky login failed: {e}")
    
    def _parse_command(self, content: str) -> tuple[str, list[str]]:
        # content like: "!walk" or "!forward 3"
        if not content:
            return "", []
        text = content.strip()
        if not text.startswith("!"):
            return "", []
        text = text[1:].strip()
        if not text:
            return "", []

        parts = text.split()
        name = parts[0].lower()
        args = parts[1:]
        return name, args
    
    
    async def event_ready(self):
        log(f"Connected to Twitch as {self.nick}")
        start_state_poller()

        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._command_worker())
            log("Command worker started.")

        asyncio.create_task(self._refresh_loop())
        asyncio.create_task(self._start_schedulers())

        if self._teleport_loop_task is None:
            self._teleport_loop_task = asyncio.create_task(self._teleport_loop())
            log(f"Teleport loop started — first teleport in {self._teleport_interval_s // 3600}h.")

        if self._bsky and self._clip_task is None:
            self._clip_task = asyncio.create_task(self._delayed_clip_post())

        channel = self.get_channel(Config.TWITCH_CHANNEL)
        if channel:
            log("Startup sequence: beginning...")
            await self._say(channel, "No Man's Walk is online!")
            await self._do_help(channel)
            await self._do_status(channel)

            await asyncio.to_thread(left_click)
            await asyncio.sleep(0.3)

            await self._do_walk(channel)
            log("Startup sequence: complete.")

    async def event_message(self, message):
        if message.echo:
            return

        content = (message.content or "").strip()
        if content.startswith("!"):
            name, args = self._parse_command(content)
            ctx = await self.get_context(message)
            if name == "yes":
                await self._cast_vote(ctx, message, "yes")
            elif name == "no":
                await self._cast_vote(ctx, message, "no")
            elif name == "help":
                await self._do_help(ctx, args)
            elif name == "status":
                await self._do_status(ctx)
            elif name:
                await self._dispatch_nms_command(ctx, name, args)
            return

        await self.handle_commands(message)

    def _is_admin(self, username: str) -> bool:
        return (username or "").lower() in self._admin_users

    async def _say(self, ctx, text: str):
        if not text:
            return
        await ctx.send(text)
        await asyncio.sleep(Config.CHAT_DELAY)

    async def _command_worker(self):
        while True:
            name, args = await self._cmd_queue.get()
            log(f"Command worker: executing !{name} {args}")
            self._executing = True
            try:
                func = COMMANDS.get(name)
                if func:
                    await asyncio.to_thread(func.func, args)
                    log(f"Command worker: !{name} complete.")
                else:
                    log(f"Command worker: no func found for !{name}")
            except Exception as e:
                log(f"Command failed: !{name} {args} ({e})")
            finally:
                self._executing = False
                self._cmd_queue.task_done()

            # After teleport, drain anything that snuck into the queue during
            # the tiny window before the loading flag was raised.
            if name == "teleport":
                drained = 0
                while not self._cmd_queue.empty():
                    try:
                        self._cmd_queue.get_nowait()
                        self._cmd_queue.task_done()
                        drained += 1
                    except asyncio.QueueEmpty:
                        break
                if drained:
                    log(f"Teleport: drained {drained} stale command(s) from queue.")

    async def _enqueue_command(self, ctx: commands.Context, name: str, args: list[str]):
        was_busy = self._executing or (self._cmd_queue.qsize() > 0)
        await self._cmd_queue.put((name, args))
        if was_busy:
            pass

    async def _refresh_loop(self):
        while True:
            try:
                tokens = self._tokens.ensure_fresh()
                new_access = str(tokens.get("access_token") or "").strip()
                if new_access and new_access != self._access_token:
                    self._access_token = new_access
                    # Best-effort: update token for internal clients if present.
                    try:
                        if hasattr(self, "_connection") and hasattr(self._connection, "_token"):
                            self._connection._token = self._access_token
                    except Exception:
                        pass
                    try:
                        if hasattr(self, "http") and hasattr(self.http, "_token"):
                            self.http._token = self._access_token
                    except Exception:
                        pass
            except Exception:
                log("Token refresh loop failed")

            # Sleep until near expiry (or a short backoff if expires_at missing).
            try:
                tokens = self._tokens.load()
                expires_at = int(tokens.get("expires_at") or 0)
                sleep_s = 300
                if expires_at:
                    sleep_s = max(30, int(expires_at - time.time() - Config.TOKEN_REFRESH_SKEW_S))
                await asyncio.sleep(sleep_s)
            except Exception:
                await asyncio.sleep(300)

    async def _start_schedulers(self):
        """Spawn one independent loop per entry in Config.SCHEDULED_COMMANDS."""
        channel = self.get_channel(Config.TWITCH_CHANNEL)
        if not channel:
            return
        for interval_s, handler_name in Config.SCHEDULED_COMMANDS:
            asyncio.create_task(self._scheduler_loop(channel, interval_s, handler_name))

    async def _scheduler_loop(self, channel, interval_s: int, handler_name: str):
        """Wait `interval_s` seconds, then call `self.<handler_name>(channel)`, repeat."""
        handler = getattr(self, handler_name, None)
        if handler is None:
            log(f"Scheduler: unknown handler '{handler_name}', skipping.")
            return
        log(f"Scheduler: '{handler_name}' will run every {interval_s}s.")
        while True:
            await asyncio.sleep(interval_s)
            try:
                await handler(channel)
            except Exception as e:
                log(f"Scheduler: '{handler_name}' failed: {e}")

    
    async def _teleport_loop(self):
        """Every _teleport_interval_s (from startup) automatically teleport to a new planet."""
        while True:
            sleep_s = max(0.0, self._next_teleport_time - time.time())
            log(f"Teleport loop: sleeping {sleep_s:.0f}s until next auto-teleport.")
            await asyncio.sleep(sleep_s)

            channel = self.get_channel(Config.TWITCH_CHANNEL)
            log("Teleport loop: firing scheduled teleport.")
            try:
                if channel:
                    await self._say(channel, "Warping to a new planet...")
                await self._cmd_queue.put(("teleport", []))
            except Exception as e:
                log(f"Teleport loop: failed to queue teleport: {e}")

            # Advance the clock by exactly one interval (stays in phase with startup)
            self._next_teleport_time += self._teleport_interval_s


    async def _delayed_clip_post(self):
        delay_s = Config.CLIP_POST_DELAY_MINUTES * 60
        log(f"Clip scheduler: posting in {Config.CLIP_POST_DELAY_MINUTES} minutes.")
        await asyncio.sleep(delay_s)

        if not self._bsky:
            log("Clip scheduler: no Bluesky client.")
            return

        try:
            await asyncio.to_thread(nms_bluesky.post_clip, self._bsky)
            log("Clip scheduler: post_clip() complete.")
        except Exception as e:
            log(f"Clip scheduler failed: {e}")
    
    
    async def _start_vote(self, ctx: commands.Context, name: str, args: list[str]):
        if self._vote.active:
            await self._say(ctx, "Vote already in progress.")
            return

        self._vote.active = True
        self._vote.cmd_name = name
        self._vote.args_raw = " ".join(args)

        starter = (ctx.author.name or "").lower()
        if starter:
            self._vote.yes.add(starter)

        cmd = COMMANDS.get(name)
        help_text = f"{cmd.help}" if cmd and cmd.help else ""
        await self._say(ctx, f"Vote started! {help_text} • Type !yes or !no • {Config.VOTING_DURATION} seconds • {self._tally()}")

        async def _finish():
            await asyncio.sleep(Config.VOTING_DURATION)
            try:
                passed = len(self._vote.yes) > len(self._vote.no)
                yes = len(self._vote.yes)
                no = len(self._vote.no)
                cmd = COMMANDS.get(name)
                help_text = f"{cmd.help}" if cmd and cmd.help else ""

                if passed:
                    await self._say(ctx, f"Vote passed! ({yes}-{no}) • {help_text}")
                    await self._enqueue_command(ctx, name, args)
                else:
                    await self._say(ctx, f"Vote failed! ({yes}-{no}) • {help_text}")
            finally:
                self._vote.reset()

        self._vote.task = asyncio.create_task(_finish())

    def _tally(self) -> str:
        return f"(Yes: {len(self._vote.yes)} | No: {len(self._vote.no)})"

    async def _cast_vote(self, ctx, message, side: str):
        if not self._vote.active:
            return
        # Read username directly from message tags — more reliable than ctx.author
        user = ""
        try:
            user = (message.author.name or "").lower()
        except Exception:
            pass
        if not user:
            try:
                user = (message.tags or {}).get("display-name", "").lower()
            except Exception:
                pass
        if not user:
            return
        if user in self._vote.yes or user in self._vote.no:
            return
        if side == "yes":
            self._vote.yes.add(user)
            await self._say(ctx, f"{user} voted YES • {self._tally()}")
        else:
            self._vote.no.add(user)
            await self._say(ctx, f"{user} voted NO • {self._tally()}")

    @commands.command(name="yes")
    async def cmd_yes(self, ctx: commands.Context):
        await self._cast_vote(ctx, ctx.message, "yes")

    @commands.command(name="no")
    async def cmd_no(self, ctx: commands.Context):
        await self._cast_vote(ctx, ctx.message, "no")

    @commands.command(name="status")
    async def cmd_status(self, ctx: commands.Context):
        await self._do_status(ctx)
    
    async def _do_status(self, ctx):
        try:
            status = get_status_text(countdown=self._format_countdown())
            main = status.get("main", "").strip()
            details = status.get("details", "").strip()
            status_text = " • ".join(filter(None, [main, details]))
            await self._say(ctx, status_text)
            await self._update_stream_info(title=main)
        except Exception as e:
            log(f"!status failed: {e}")
            await self._say(ctx, "Could not read game state.")
            return

        if self._bsky:
            nms_bluesky.ensure_live(self._bsky, main)

    def _format_countdown(self) -> str:
        """Return a human-readable countdown to the next auto-teleport, e.g. '3h24m'."""
        remaining = max(0.0, self._next_teleport_time - time.time())
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        return f"{h}h{m:02d}m"

    async def _update_stream_info(self, title: str = ""):
        """Update the Twitch stream title and tags via the Helix API."""
        try:
            client_id = Config.get_client_id()
            oauth_token = self._access_token

            async with aiohttp.ClientSession() as session:
                headers = {
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {oauth_token}",
                    "Content-Type": "application/json",
                }

                # Get broadcaster ID
                async with session.get(
                    f"https://api.twitch.tv/helix/users?login={Config.TWITCH_CHANNEL}",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        log(f"Stream update: failed to get user ID ({resp.status})")
                        return
                    data = await resp.json()
                    broadcaster_id = data["data"][0]["id"]

                # Patch title and tags in one request
                async with session.patch(
                    f"https://api.twitch.tv/helix/channels?broadcaster_id={broadcaster_id}",
                    headers=headers,
                    json={
                        "title": title,
                        "tags": Config.STREAM_TAGS,
                    },
                ) as resp:
                    if resp.status == 204:
                        log(f"Stream info updated: title='{title}' tags={Config.STREAM_TAGS}")
                    else:
                        text = await resp.text()
                        log(f"Stream update failed: {resp.status} - {text}")

        except Exception as e:
            log(f"_update_stream_info error: {e}")

    @commands.command(name="help")
    async def cmd_help(self, ctx: commands.Context):
        await self._do_help(ctx)

    async def _do_help(self, ctx, args=None):
        if args:
            name = args[0].lower().lstrip("!")
            cmd = COMMANDS.get(name)
            if cmd:
                alias_str = (f" (aliases: {', '.join('!' + a for a in cmd.aliases)})" if cmd.aliases else "")
                await self._say(ctx, f"!{name}: {cmd.help}{alias_str}" if cmd.help else f"!{name}: no description available.{alias_str}")
            else:
                await self._say(ctx, f"Unknown command: !{name}")
            return
        # Only show canonical names, not aliases or hidden commands, in the command list
        all_aliases = {a for c in COMMANDS.values() for a in c.aliases}
        primary_names = [n for n in COMMANDS if n not in all_aliases and not COMMANDS[n].hidden]
        cmds_text = "Commands: " + " • ".join(f"!{n}" for n in primary_names)
        cmds_text = f"{cmds_text} • Type !help <cmd> for details."
        await self._say(ctx, cmds_text)

    @commands.command(name="walk")
    async def cmd_walk(self, ctx: commands.Context):
        await self._do_walk(ctx)

    async def _do_walk(self, ctx):
        await self._enqueue_command(ctx, "walk", [])

    async def _dispatch_nms_command(self, ctx: commands.Context, name: str, args: list[str]):
        if name not in COMMANDS:
            return

        if name in Config.ADMIN_ONLY_COMMANDS and not self._is_admin(ctx.author.name):
            return

        if is_planet_loading():
            await self._say(ctx, "Planet loading — please wait before sending commands.")
            return

        if name in Config.VOTABLE_COMMANDS:
            await self._start_vote(ctx, name, args)
            return

        await self._enqueue_command(ctx, name, args)

    async def event_command_error(self, ctx: commands.Context, error: Exception):
        # We dispatch most !commands ourselves; ignore TwitchIO's CommandNotFound noise.
        if isinstance(error, CommandNotFound):
            return
        log(f"Command error: {error}")

    async def event_error(self, error: Exception, data=None):
        log(f"Event error: {error}")

    async def event_raw_data(self, data: str):
        # keep quiet
        return

def main():
    bot = NMSBot()
    bot.run()


if __name__ == "__main__":
    main()