from nms_bot import COMMANDS, start_state_poller
from twitchio.ext.commands.errors import CommandNotFound
from utils import log, get_status_text
from dataclasses import dataclass
from twitchio.ext import commands
from typing import Optional
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


# Mark commands as admin-only or votable here.
ADMIN_ONLY_COMMANDS = {
    # "camera",
}

VOTABLE_COMMANDS = {
    # "dig",
    "camera",
}


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

        super().__init__(
            token=self._access_token,
            prefix="!",
            initial_channels=[Config.TWITCH_CHANNEL],
        )

    
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

        asyncio.create_task(self._refresh_loop())

        channel = self.get_channel(Config.TWITCH_CHANNEL)
        if channel:
            # Startup sequence
            await self._say(channel, "No Man's Walk is online!")
            await self._do_help(channel)
            await self._do_status(channel)
            await self._do_walk(channel)

    async def event_message(self, message):
        if message.echo:
            return

        content = (message.content or "").strip()
        if content.startswith("!"):
            name, args = self._parse_command(content)
            if name in {"yes", "no", "help", "status"}:
                await self.handle_commands(message)
                return
            if name:
                ctx = await self.get_context(message)
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
            self._executing = True
            try:
                func = COMMANDS.get(name)
                if func:
                    await asyncio.to_thread(func, args)
            except Exception as e:
                log(f"Command failed: !{name} {args} ({e})")
            finally:
                self._executing = False
                self._cmd_queue.task_done()

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

        await self._say(ctx, f"Vote started: !{name} {self._vote.args_raw}".strip())

        async def _finish():
            await asyncio.sleep(Config.VOTING_DURATION)
            try:
                passed = len(self._vote.yes) > len(self._vote.no)
                yes = len(self._vote.yes)
                no = len(self._vote.no)

                if passed:
                    await self._say(ctx, f"Vote passed ({yes}-{no}). Executing.")
                    await self._enqueue_command(ctx, name, args)
                else:
                    await self._say(ctx, f"Vote failed ({yes}-{no}).")
            finally:
                self._vote.reset()

        self._vote.task = asyncio.create_task(_finish())

    @commands.command(name="yes")
    async def cmd_yes(self, ctx: commands.Context):
        if not self._vote.active:
            return
        user = (ctx.author.name or "").lower()
        if not user:
            return
        if user in self._vote.yes or user in self._vote.no:
            return
        self._vote.yes.add(user)
        await self._say(ctx, f"{user} voted YES")

    @commands.command(name="no")
    async def cmd_no(self, ctx: commands.Context):
        if not self._vote.active:
            return
        user = (ctx.author.name or "").lower()
        if not user:
            return
        if user in self._vote.yes or user in self._vote.no:
            return
        self._vote.no.add(user)
        await self._say(ctx, f"{user} voted NO")

    @commands.command(name="status")
    async def cmd_status(self, ctx: commands.Context):
        await self._do_status(ctx)
    
    async def _do_status(self, ctx):
        try:
            status = get_status_text()
            status_text = f'{status.get("main", "")} {status.get("details", "")}'.strip()
            await self._say(ctx, status_text)
        except Exception as e:
            log(f"!status failed: {e}")
            await self._say(ctx, "Could not read game state.")

    @commands.command(name="help")
    async def cmd_help(self, ctx: commands.Context):
        await self._do_help(ctx)

    async def _do_help(self, ctx):
        names = sorted(COMMANDS.keys())
        await self._say(ctx, "Commands: " + " • ".join(f"!{n}" for n in names))

    @commands.command(name="walk")
    async def cmd_walk(self, ctx: commands.Context):
        await self._do_walk(ctx)

    async def _do_walk(self, ctx):
        await self._enqueue_command(ctx, "walk", [])

    async def _dispatch_nms_command(self, ctx: commands.Context, name: str, args: list[str]):
        if name not in COMMANDS:
            return

        if name in ADMIN_ONLY_COMMANDS and not self._is_admin(ctx.author.name):
            return

        if name in VOTABLE_COMMANDS:
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