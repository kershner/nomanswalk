from nms_bot import COMMANDS, MOVEMENT_COMMANDS, NMSState, activate_quickslot, cancel_movement, get_active_quickslot, get_canonical_command_name, is_quickslot_cooldown, get_movement_generation, is_command_allowed, start_state_poller, left_click, is_planet_loading, record_daily_command, get_runtime_game_state
from twitchio.ext.commands.errors import CommandNotFound
from utils import log, get_info_text, get_location_text
from datetime import datetime, timedelta
from dataclasses import dataclass
from twitchio.ext import commands
from typing import Optional
import nms_bluesky
import subprocess
import aiohttp
import requests
import asyncio
import json
import time
import pytz
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
    TELEPORT_VOTING_DURATION = 60

    TOKEN_REFRESH_SKEW_S = 60  # refresh ~1 minute before expiry

    SCHEDULED_COMMANDS = [
        (20 * 60, "_do_help"),    # !help every 20 minutes
        (30 * 60, "_do_info"),    # !info every 30 minutes
        (60 * 60, "_do_location"),  # !location every 60 minutes
    ]

    SHUTDOWN_HOUR = 0             # 0 = midnight; change to e.g. 2 for 2 AM EST
    SHUTDOWN_MINUTE = 0
    SHUTDOWN_TZ = "US/Eastern"
    
    STREAM_TAGS = [
        "Exploration",
        "Automation",
        "Chill",
        "Cozy",
        "Comfy",
        "Interactive",
        "PC",
        "Programming",
        "Casual",
        "Python"
    ]

    ADMIN_ONLY_COMMANDS = {
        "next_planet",
    }

    VOTABLE_COMMANDS = {
        "camera",
        "coords",
        "music",
        "teleport",
    }

    COMMAND_FEEDBACK = {
        "walk": "Autowalk toggled.",
        "w": "Autowalk toggled.",
        "stop": "All movement stopped.",
        "s": "All movement stopped.",
        "cruise": "Cruise toggled.",
        "engage": "Boost toggled.",
        "boost": "Boost toggled.",
        "camera": "Camera toggled.",
        "coords": "Showing planet coordinates for 10 seconds.",
        "music": "Music toggled.",
        "day": "Daytime forced. Use !resume_time to return to the normal planet cycle.",
        "night": "Nighttime forced. Use !resume_time to return to the normal planet cycle.",
        "resume_time": "Normal planet time resumed.",
        "storm": "Storm toggled. Weather should shift shortly.",
        "gravity": "Low gravity toggled.",
        "ship": "Ship placement selected.",
        "anomaly": "Anomaly placement selected.",
        "pet": "Pet placement selected.",
    }

    PARAM_GUARD_CMDS = [
        "up", 
        "down", 
        "left", 
        "right", 
        "forward", 
        "back", 
    ]

    # Edit this list to control when Bluesky posts happen each day.
    BLUESKY_POST_TZ = "US/Eastern"
    BLUESKY_POST_TIMES = [
        "12:00",  # 12pm / noon
        "19:00",  # 7pm
    ]

    USE_COMMAND_QUEUE = False

    FORCED_QUEUE_COMMANDS = {
        "teleport",
        "next_planet",
        "coords",
    }

    LOCKOUT_COMMANDS = {
        "coords",
    }

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
    votes: dict[str, str] = None
    task: Optional[asyncio.Task] = None

    def reset(self):
        self.active = False
        self.cmd_name = ""
        self.args_raw = ""
        self.votes = {}
        self.task = None


# ─────────────────────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────────────────────
class NMSBot(commands.Bot):
    def __init__(self, dev_mode=False):
        self._dev_mode = dev_mode
        self._admin_users = set(Config.get_admin_users())

        self._vote = VoteState()
        self._vote.reset()

        self._teleport_vote = VoteState()
        self._teleport_vote.reset()

        self._cmd_queue: asyncio.Queue[tuple[str, list[str], int | None]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._active_command_tasks: set[asyncio.Task] = set()
        self._lockout_command: Optional[str] = None

        self._tokens = None
        self._access_token = "dev"
        if not self._dev_mode:
            self._tokens = OAuthTokens(Config.get_client_id(), Config.get_client_secret(), Config.TOKENS_FILE)
            tokens = self._tokens.ensure_fresh()
            self._access_token = str(tokens.get("access_token") or "").strip()

        self._bsky = None
        self._bluesky_post_task: Optional[asyncio.Task] = None

        self._teleport_interval_s = 4 * 3600  # 4 hours
        self._next_teleport_time: float = time.time() + self._teleport_interval_s
        self._teleport_loop_task: Optional[asyncio.Task] = None

        self._shutdown_loop_task: Optional[asyncio.Task] = None

        super().__init__(
            token=self._access_token,
            prefix="!",
            initial_channels=[Config.TWITCH_CHANNEL],
        )

        if not self._dev_mode:
            try:
                self._bsky = nms_bluesky.login()
                log("Bluesky logged in.")
            except Exception as e:
                log(f"Bluesky login failed: {e}")

    def _parse_command(self, content: str) -> tuple[str, list[str]]:
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

    def _should_queue_command(self, name: str) -> bool:
        return Config.USE_COMMAND_QUEUE or name in Config.FORCED_QUEUE_COMMANDS

    async def _start_runtime(self, channel, run_startup=True):
        self._chat_context = channel
        start_state_poller()

        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._command_worker())
            log("Command worker started.")

        if not self._dev_mode:
            asyncio.create_task(self._refresh_loop())
            asyncio.create_task(self._start_schedulers())

            if self._teleport_loop_task is None:
                self._teleport_loop_task = asyncio.create_task(self._teleport_loop())
                log(f"Teleport loop started — first teleport in {self._teleport_interval_s // 3600}h.")

            if self._shutdown_loop_task is None:
                self._shutdown_loop_task = asyncio.create_task(self._nightly_shutdown_loop())
                log("Nightly shutdown loop started.")

            if self._bsky and self._bluesky_post_task is None:
                self._bluesky_post_task = asyncio.create_task(self._fixed_bluesky_post_loop())
                log("Bluesky scheduler: fixed-time post loop started.")

        if run_startup:
            log("Startup sequence: beginning...")
            await self._say(channel, "No Man's Walk is online!")
            await self._do_help(channel)
            await self._do_info(channel)
            await self._do_location(channel)

            await self._start_vote(channel, "teleport", [], starter=Config.TWITCH_CHANNEL)
            vote_task = self._teleport_vote.task
            if vote_task:
                await vote_task
            await self._cmd_queue.join()

            await asyncio.to_thread(left_click)
            await asyncio.sleep(0.3)
            await self._do_walk(channel, announce=False)
            log("Startup sequence: complete.")

    async def event_ready(self):
        log(f"Connected to Twitch as {self.nick}")
        channel = self.get_channel(Config.TWITCH_CHANNEL)
        if channel:
            while not get_runtime_game_state().get("startup_ready", False):
                await asyncio.sleep(0.5)
            await self._start_runtime(channel)

    async def process_chat_message(self, ctx, content: str, message=None):
        content = (content or "").strip()
        if not content.startswith("!"):
            return

        name, args = self._parse_command(content)

        runtime_game = get_runtime_game_state()
        teleport_vote_response = (
            name in {"yes", "no"}
            and args
            and args[0].lower().lstrip("!") in {"teleport", "planet"}
            and self._teleport_vote.active
        )

        if not teleport_vote_response:
            if not runtime_game.get("startup_ready", False):
                await self._say(ctx, "Game loading — please wait.")
                return

            if runtime_game.get("planet_loading", False):
                await self._say(ctx, "Planet loading — please wait.")
                return

        if self._lockout_command:
            await self._say(ctx, f"!{self._lockout_command} is running — please wait.")
            return

        canonical_name = get_canonical_command_name(name)
        active_quickslot = get_active_quickslot()
        if active_quickslot and canonical_name not in MOVEMENT_COMMANDS | {"left_click", "right_click", "stop"}:
            await self._say(ctx, f"Finish or cancel !{active_quickslot} first.")
            return

        if is_quickslot_cooldown() and canonical_name in {"camera", "coords", "ship", "anomaly", "pet", "dance"}:
            await self._say(ctx, "Quick menu closing — please wait.")
            return

        if name in {"yes", "no", "help", "more", "info", "location", "loc"} or name in COMMANDS:
            record_daily_command(getattr(getattr(ctx, "author", None), "name", ""))

        if name == "yes":
            await self._cast_vote(ctx, message, "yes", args[0] if args else "")
        elif name == "no":
            await self._cast_vote(ctx, message, "no", args[0] if args else "")
        elif name == "help":
            await self._do_help(ctx, args)
        elif name == "more":
            await self._do_more(ctx)
        elif name == "info":
            await self._do_info(ctx)
        elif name in {"location", "loc"}:
            await self._do_location(ctx)
        elif name:
            if name in Config.PARAM_GUARD_CMDS:
                await self._param_guard_cmd(ctx, name, args)
            else:
                await self._dispatch_nms_command(ctx, name, args)

    async def event_message(self, message):
        if message.echo:
            return

        content = (message.content or "").strip()
        if content.startswith("!"):
            ctx = await self.get_context(message)
            await self.process_chat_message(ctx, content, message)
            return

        await self.handle_commands(message)

    def _is_admin(self, username: str) -> bool:
        return (username or "").lower() in self._admin_users

    async def _say(self, ctx, text: str):
        if not text:
            return
        await ctx.send(text)
        await asyncio.sleep(Config.CHAT_DELAY)

    async def _run_command(self, name: str, args: list[str], movement_generation: int | None = None):
        func = COMMANDS.get(name)
        if not func:
            log(f"Command worker: no func found for !{name}")
            return

        lockout = name in Config.LOCKOUT_COMMANDS
        if lockout:
            self._lockout_command = name

        try:
            log(f"Command worker: executing !{name} {args}")
            canonical_name = get_canonical_command_name(name)
            if canonical_name in MOVEMENT_COMMANDS:
                await asyncio.to_thread(func.func, args, movement_generation)
            else:
                await asyncio.to_thread(func.func, args)
            log(f"Command worker: !{name} complete.")
        except Exception as e:
            log(f"Command failed: !{name} {args} ({e})")
        finally:
            if lockout:
                self._lockout_command = None

    async def _start_immediate_command(self, name: str, args: list[str], movement_generation: int | None = None):
        task = asyncio.create_task(self._run_command(name, args, movement_generation))
        self._active_command_tasks.add(task)
        task.add_done_callback(self._active_command_tasks.discard)

    async def _command_worker(self):
        while True:
            name, args, movement_generation = await self._cmd_queue.get()
            try:
                await self._run_command(name, args, movement_generation)
            finally:
                self._cmd_queue.task_done()

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

    async def _param_guard_cmd(self, ctx: commands.Context, name: str, args: list[str]):
        if not args:
            cmd = COMMANDS.get(name)
            help_text = f"{cmd.help}" if cmd and cmd.help else ""
            await self._say(ctx, help_text)
            return
        
        await self._dispatch_nms_command(ctx, name, args)
    
    
    async def _enqueue_command(self, name: str, args: list[str], movement_generation: int | None = None):
        await self._cmd_queue.put((name, args, movement_generation))

    async def _submit_command(self, name: str, args: list[str]):
        canonical_name = get_canonical_command_name(name)

        if canonical_name in {"ship", "anomaly", "pet"}:
            activate_quickslot(canonical_name)

        if canonical_name == "stop":
            cancel_movement()
            await self._start_immediate_command(name, args)
            return

        movement_generation = (
            get_movement_generation() if canonical_name in MOVEMENT_COMMANDS else None
        )

        if self._should_queue_command(name):
            await self._enqueue_command(name, args, movement_generation)
        else:
            await self._start_immediate_command(name, args, movement_generation)

    async def _refresh_loop(self):
        while True:
            try:
                tokens = self._tokens.ensure_fresh()
                new_access = str(tokens.get("access_token") or "").strip()
                if new_access and new_access != self._access_token:
                    self._access_token = new_access
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
        channel = getattr(self, "_chat_context", None) or self.get_channel(Config.TWITCH_CHANNEL)
        if not channel:
            return
        for interval_s, handler_name in Config.SCHEDULED_COMMANDS:
            asyncio.create_task(self._scheduler_loop(channel, interval_s, handler_name))

    async def _scheduler_loop(self, channel, interval_s: int, handler_name: str):
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
        """Every _teleport_interval_s (from startup) start a vote to teleport to a new planet."""
        while True:
            sleep_s = max(0.0, self._next_teleport_time - time.time())
            log(f"Teleport loop: sleeping {sleep_s:.0f}s until next teleport vote.")
            await asyncio.sleep(sleep_s)

            channel = getattr(self, "_chat_context", None) or self.get_channel(Config.TWITCH_CHANNEL)
            log("Teleport loop: starting scheduled teleport vote.")
            try:
                if channel:
                    await self._start_vote(channel, "teleport", [], starter=Config.TWITCH_CHANNEL)
            except Exception as e:
                log(f"Teleport loop: failed to start teleport vote: {e}")

            self._next_teleport_time += self._teleport_interval_s

    async def _nightly_shutdown_loop(self):
        """
        Runs every 60 seconds. Shuts down at the configured time each night by
        comparing minutes until the *next* occurrence of that time.
        """
        log(
            f"Nightly shutdown loop: targeting {Config.SHUTDOWN_HOUR:02d}:{Config.SHUTDOWN_MINUTE:02d} "
            f"{Config.SHUTDOWN_TZ} each night."
        )
        last_warning_date = None
        last_shutdown_date = None

        while True:
            try:
                tz = pytz.timezone(Config.SHUTDOWN_TZ)
                now = datetime.now(tz)

                shutdown_next = (now + timedelta(days=1)).replace(
                    hour=Config.SHUTDOWN_HOUR,
                    minute=Config.SHUTDOWN_MINUTE,
                    second=0,
                    microsecond=0,
                )
                minutes_until = int((shutdown_next - now).total_seconds() // 60)

                channel = self.get_channel(Config.TWITCH_CHANNEL)

                # ── 10-minute warning ──────────────────────────────────────
                if 0 < minutes_until <= 10 and last_warning_date != now.date():
                    last_warning_date = now.date()
                    log("Nightly shutdown: sending 10-minute warning to chat.")
                    if channel:
                        await self._say(
                            channel,
                            f"🌙 No Man's Walk will be shutting down in "
                            f"{minutes_until} minutes. "
                            "The Walker will be back in the morning, see you then!"
                        )

                # ── Shutdown ───────────────────────────────────────────────
                elif minutes_until > 1430 and last_shutdown_date != now.date():
                    last_shutdown_date = now.date()
                    log("Nightly shutdown: shutting down now.")
                    subprocess.run(["shutdown", "/s", "/t", "30"], check=False)

            except Exception as e:
                log(f"Nightly shutdown loop error: {e}")

            await asyncio.sleep(60)

    def _next_bluesky_post_time(self):
        """Return the next configured Bluesky post time."""
        tz = pytz.timezone(Config.BLUESKY_POST_TZ)
        now = datetime.now(tz)
        next_times = []

        for post_time in Config.BLUESKY_POST_TIMES:
            hour, minute = map(int, str(post_time).split(":", 1))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            next_times.append(candidate)

        return min(next_times)

    async def _fixed_bluesky_post_loop(self):
        if not Config.BLUESKY_POST_TIMES:
            log("Bluesky scheduler: no post times configured.")
            return

        log(
            "Bluesky scheduler: fixed post times are "
            f"{', '.join(Config.BLUESKY_POST_TIMES)} {Config.BLUESKY_POST_TZ}."
        )

        while True:
            next_post = self._next_bluesky_post_time()
            sleep_s = max(0.0, (next_post - datetime.now(next_post.tzinfo)).total_seconds())
            log(f"Bluesky scheduler: next post at {next_post:%Y-%m-%d %H:%M %Z}.")
            await asyncio.sleep(sleep_s)

            if not self._bsky:
                log("Bluesky scheduler: no Bluesky client.")
                continue

            try:
                await asyncio.to_thread(nms_bluesky.post_clip, self._bsky, countdown=self._format_countdown())
                log("Bluesky scheduler: post_clip() complete.")
            except Exception as e:
                log(f"Bluesky scheduler failed: {e}")

    async def _start_vote(self, ctx: commands.Context, name: str, args: list[str], starter=None):
        is_teleport = name == "teleport"
        vote = self._teleport_vote if is_teleport else self._vote
        duration = Config.TELEPORT_VOTING_DURATION if is_teleport else Config.VOTING_DURATION

        if vote.active:
            await self._say(ctx, "Teleport vote already in progress." if is_teleport else "Vote already in progress.")
            return

        vote.active = True
        vote.cmd_name = name
        vote.args_raw = " ".join(args)
        vote.votes = {}

        starter = (starter or ctx.author.name or "").lower()
        if starter:
            vote.votes[starter] = "yes"

        cmd = COMMANDS.get(name)
        help_text = f"{cmd.help}" if cmd and cmd.help else ""

        if is_teleport:
            if args:
                galaxy = args[1] if len(args) > 1 else "current"
                teleport_text = f"address {args[0]} • Galaxy: {galaxy}"
            else:
                teleport_text = "random planet"

            await self._say(
                ctx,
                f"Teleport vote started! Destination: {teleport_text} • "
                f"Vote with !yes teleport or !no teleport • "
                f"{duration} seconds • {self._tally(vote)}"
            )
        else:
            await self._say(
                ctx,
                f"Vote started! {help_text} • Type !yes or !no • "
                f"{duration} seconds • {self._tally(vote)}"
            )

        async def _finish():
            if is_teleport:
                await asyncio.sleep(duration / 2)
                if vote.active:
                    await self._say(
                        ctx,
                        f"Teleport vote halfway! Destination: {teleport_text} • "
                        f"Vote with !yes teleport or !no teleport • "
                        f"{duration // 2} seconds remaining • {self._tally(vote)}"
                    )
                await asyncio.sleep(duration / 2)
            else:
                await asyncio.sleep(duration)

            try:
                yes = sum(1 for value in vote.votes.values() if value == "yes")
                no = sum(1 for value in vote.votes.values() if value == "no")
                passed = yes > no

                cmd = COMMANDS.get(name)
                help_text = f"{cmd.help}" if cmd and cmd.help else ""

                if passed:
                    passed_text = f"Destination: {teleport_text}" if is_teleport else help_text
                    await self._say(ctx, f"Vote passed! ({yes}-{no}) • {passed_text}")
                    await self._submit_command(name, args)
                    feedback = Config.COMMAND_FEEDBACK.get(name)
                    if feedback:
                        await self._say(ctx, feedback)
                else:
                    await self._say(ctx, f"Vote failed! ({yes}-{no}) • {help_text}")
            finally:
                vote.reset()

        vote.task = asyncio.create_task(_finish())

    def _tally(self, vote: VoteState = None) -> str:
        vote = vote or self._vote
        yes = sum(1 for value in vote.votes.values() if value == "yes")
        no = sum(1 for value in vote.votes.values() if value == "no")
        return f"(Yes: {yes} | No: {no})"

    async def _cast_vote(self, ctx, message, side: str, target: str = ""):
        target = (target or "").lower().lstrip("!")

        if target in {"teleport", "planet"}:
            vote = self._teleport_vote
            label = "Teleport "
        elif self._vote.active:
            vote = self._vote
            label = ""
        else:
            return

        if not vote.active:
            return

        user = ""

        try:
            user = (ctx.author.name or "").lower()
        except Exception:
            pass

        if not user:
            try:
                user = (message.author.name or "").lower()
            except Exception:
                pass

        if not user:
            try:
                user = ((message.tags or {}).get("display-name") or "").lower()
            except Exception:
                pass

        if not user:
            await self._say(ctx, f"Could not count your !{side} vote.")
            return

        side = side.lower()
        vote.votes[user] = side

        if side == "yes":
            await self._say(ctx, f"{user} voted YES • {label}{self._tally(vote)}")
        else:
            await self._say(ctx, f"{user} voted NO • {label}{self._tally(vote)}")

    @commands.command(name="yes")
    async def cmd_yes(self, ctx: commands.Context, target: str = ""):
        await self._cast_vote(ctx, ctx.message, "yes", target)

    @commands.command(name="no")
    async def cmd_no(self, ctx: commands.Context, target: str = ""):
        await self._cast_vote(ctx, ctx.message, "no", target)

    @commands.command(name="info")
    async def cmd_info(self, ctx: commands.Context):
        await self._do_info(ctx)

    async def _do_info(self, ctx):
        info = get_info_text(countdown=self._format_countdown())
        title = info.split(" • Today:", 1)[0]
        await self._say(ctx, f"🪐{info}")
        await self._update_stream_info(title=title)
        if self._bsky:
            nms_bluesky.ensure_live(self._bsky, title)

    @commands.command(name="location", aliases=("loc",))
    async def cmd_location(self, ctx: commands.Context):
        await self._do_location(ctx)

    async def _do_location(self, ctx):
        await self._say(ctx, f"🌎{get_location_text()}")

    def _format_countdown(self) -> str:
        """Return a human-readable countdown to the next auto-teleport, e.g. '3h24m'."""
        remaining = max(0.0, self._next_teleport_time - time.time())
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        return f"{h}h{m:02d}m"

    async def _update_stream_info(self, title: str = ""):
        """Update the Twitch stream title and tags via the Helix API."""
        if self._dev_mode:
            return
        try:
            client_id = Config.get_client_id()
            oauth_token = self._access_token
            helix_api_url = "https://api.twitch.tv/helix/"

            async with aiohttp.ClientSession() as session:
                headers = {
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {oauth_token}",
                    "Content-Type": "application/json",
                }

                async with session.get(
                    f"{helix_api_url}users?login={Config.TWITCH_CHANNEL}",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        log(f"Stream update: failed to get user ID ({resp.status})")
                        return
                    data = await resp.json()
                    broadcaster_id = data["data"][0]["id"]

                async with session.patch(
                    f"{helix_api_url}channels?broadcaster_id={broadcaster_id}",
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

    def _help_commands(self):
        all_aliases = {a for c in COMMANDS.values() for a in c.aliases}
        return [n for n in COMMANDS if n not in all_aliases and not COMMANDS[n].hidden]

    async def _do_help(self, ctx, args=None):
        if args:
            name = args[0].lower().lstrip("!")
            meta_help = {
                "info": "Show current activity and today's stats.",
                "location": "Show current planet or space details.",
                "loc": "Alias for !location.",
                "help": "Show command list page 1.",
                "more": "Show command list page 2.",
            }
            if name in meta_help:
                await self._say(ctx, f"!{name}: {meta_help[name]}")
                return
            cmd = COMMANDS.get(name)
            if cmd:
                alias_str = (f" (aliases: {', '.join('!' + a for a in cmd.aliases)})" if cmd.aliases else "")
                await self._say(ctx, f"!{name}: {cmd.help}{alias_str}" if cmd.help else f"!{name}: no description available.{alias_str}")
            else:
                await self._say(ctx, f"Unknown command: !{name}")
            return

        commands = self._help_commands()
        split = (len(commands) + 1) // 2
        await self._say(ctx, f"🛸Commands: {' • '.join('!' + n for n in commands[:split])} • Type !more for more commands.")

    @commands.command(name="more")
    async def cmd_more(self, ctx: commands.Context):
        await self._do_more(ctx)

    async def _do_more(self, ctx):
        commands = self._help_commands()
        split = (len(commands) + 1) // 2
        commands = commands[split:] + ["help", "info", "location"]
        await self._say(ctx, f"🛸More: {' • '.join('!' + n for n in commands)} • Type !help <cmd> for details.")

    @commands.command(name="walk")
    async def cmd_walk(self, ctx: commands.Context):
        await self._do_walk(ctx)

    async def _do_walk(self, ctx, announce=True):
        await self._submit_command("walk", [])
        if announce:
            await self._say(ctx, Config.COMMAND_FEEDBACK["walk"])

    async def _dispatch_nms_command(self, ctx: commands.Context, name: str, args: list[str]):
        if name not in COMMANDS:
            return

        if name in Config.ADMIN_ONLY_COMMANDS and not self._is_admin(ctx.author.name):
            return

        state = NMSState.get()
        if not is_command_allowed(name, state):
            await self._say(ctx, f"!{name} is not available while {state.lower().replace('_', ' ')}.")
            return

        if name == "teleport" and args:
            if len(args) not in (1, 2):
                await self._say(ctx, "Usage: !teleport <12-character planet address> [galaxy 1-255]")
                return

            address = args[0].strip().upper()
            if (
                len(address) != 12
                or address[0] not in "123456"
                or any(c not in "0123456789ABCDEF" for c in address)
            ):
                await self._say(ctx, "Invalid planet address. It must be 12 hexadecimal characters and start with 1-6.")
                return

            if len(args) == 2:
                try:
                    galaxy = int(args[1])
                except ValueError:
                    galaxy = 0
                if not 1 <= galaxy <= 255:
                    await self._say(ctx, "Invalid galaxy. It must be a number from 1 to 255.")
                    return
                args = [address, str(galaxy)]
            else:
                args = [address]

        if is_planet_loading():
            await self._say(ctx, "Planet loading — please wait before sending commands.")
            return

        if name in Config.VOTABLE_COMMANDS:
            await self._start_vote(ctx, name, args)
            return

        await self._submit_command(name, args)

        feedback = Config.COMMAND_FEEDBACK.get(name)
        if feedback:
            await self._say(ctx, feedback)

    async def event_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, CommandNotFound):
            return
        log(f"Command error: {error}")

    async def event_error(self, error: Exception, data=None):
        log(f"Event error: {error}")

    async def event_raw_data(self, data: str):
        return


def main():
    bot = NMSBot()
    bot.run()


if __name__ == "__main__":
    main()