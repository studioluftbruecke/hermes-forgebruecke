"""The mention channel's Hermes half: a gateway platform adapter named `forgebruecke`.

One Hermes session per issue or pull request thread (chat id `owner/repo#n`). On a mention: 👀 on the
comment, a turn with the framed text, the final answer as one comment (split at paragraph boundaries
past the limit). The platform registers `display_tier: minimal`, so Hermes sends no tool progress,
no interim messages and no streaming edits: one answer, one comment.

Safety, each measured or read from Hermes 0.23:
- It goes dormant on its own when the plugin is disabled in its profile, because a plain
  `plugins disable` leaves a running adapter polling (measured), and resumes when it is enabled again,
  because `plugins enable` builds no new adapter (measured). A lease file makes
  "one poller" structural: the newest adapter to connect owns it, any older one stops for good.
- It refuses to connect until `platform_toolsets.forgebruecke` is set: a plugin platform's default
  toolset is Hermes' core set, shell included, and a mention turn starts on a forge user's text.
- Setup notices (Hermes' one-time "no home channel" message) are dropped instead of posted into a
  public thread (`notice_delivery: private` plus a silent `send_private_notice`).
- Mention turns run on this platform, which the plugin's write gate treats as unattended, so forge
  writes through tools are refused there; the answer itself is the only write.
- With `dry_run` on it records each mention as an event and sends nothing: no reaction, no turn, no comment.
- A mention is marked answered in the ledger after Hermes took it, and the poll cursor moves after that, so
  a failed or interrupted poll answers the mention on a later poll instead of losing it.
- Hermes gets each mention at least once, and one gateway process hands over a mention Hermes took only
  once. A gateway that dies between the hand-off and the ledger save hands the mention over again after its
  restart: the ledger is the only memory that outlives the process, and a mention handed over twice is better
  than one lost.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)
PKG = __package__.rpartition(".")[0]
DELIVERY_TRIES = 3  # a mention Hermes refuses this often is marked and left alone
UPDATE_PENDING = "channel-update-pending.json"  # in the data folder; the settings page reads it
PLUGIN_YAML = Path(__file__).resolve().parents[1] / "plugin.yaml"


def _mod(name: str):
    return importlib.import_module(f".{name}", PKG)


class ForgejoMentionAdapter(BasePlatformAdapter):
    serial_receive_loop = True

    def __init__(self, config: PlatformConfig, *, home: Path, data_dir: Optional[Path],
                 get_secret: Callable[[str], Optional[str]]) -> None:
        profile, poller = _mod("profile"), _mod("channel.poller")
        super().__init__(config=config, platform=Platform(profile.PLATFORM))
        self.MAX_MESSAGE_LENGTH = poller.REPLY_LIMIT
        self.iid = uuid.uuid4().hex[:6]
        self.home = Path(home)
        self.data_dir = Path(data_dir) if data_dir else self.home / "plugin-data" / "forgebruecke-channel"
        self._state_dir = Path(data_dir) if data_dir else None  # flows need ctx.state's folder, never a guess
        self.ledger_path = self.data_dir / "channel-ledger.json"
        self.events_path = self.data_dir / "channel-events.jsonl"
        self.lease_path = self.data_dir / "channel-lease.json"
        self._dormant = False
        self._alive = False  # the poll loop's own flag: Hermes' _mark_disconnected clears _running
        # A token with a line break or a character no token has would go into an HTTP header, and urllib's
        # error for it quotes the value, which Hermes logs. Credentials drops it and names the field only.
        creds = _mod("settings").Credentials(get_secret("FORGEBRUECKE_BOT_ACCOUNT_TOKEN"))
        self._token = creds.token or ""
        self._token_problems = creds.problems()
        self._client = None
        self._bot = ""
        self._task: Optional[asyncio.Task] = None
        self._now: Callable[[], float] = lambda: time.time()
        self._failed: dict = {}  # mention key -> deliveries that raised
        self._handed: set = set()  # keys whose hand-off is over while their ledger entry is not on disk yet
        self._dry_seen: set = set()  # keys the last dry-run poll recorded; the next poll finds them again
        self._version = self._loaded_version()  # the code this adapter was built from
        extra = config.extra if isinstance(getattr(config, "extra", None), dict) else {}
        if not isinstance(getattr(config, "extra", None), dict):
            config.extra = extra
        extra.setdefault("notice_delivery", "private")
        extra.setdefault("unauthorized_dm_behavior", "ignore")
        try:
            authors = list(self._settings().mention_authors)
        except Exception:
            authors = []
        # The poller already drops authors without write access to the repo, authors outside mention_authors and
        # repos where mentions are off; this is Hermes' own allowlist on top. Empty mention_authors: anyone with
        # write access.
        extra.setdefault("allow_from", authors or ["*"])
        self._event("init", home=str(self.home))

    # -- evidence and settings -------------------------------------------------------------------

    def _event(self, event: str, **data: Any) -> None:
        line = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "iid": self.iid, "event": event, **data}
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass
        logger.info("forgebruecke channel %s %s %s", self.iid, event, data)

    def _config(self) -> dict:
        return _mod("profile").read_config(self.home)

    def _settings(self, config: Optional[dict] = None):
        profile = _mod("profile")
        return _mod("settings").load_settings(profile.setting_getter(config if config is not None else self._config()))

    def _forge_now(self, response: Any) -> float:
        """Forgejo's own clock, from the Date header of its answer. The first cursor is compared with the
        created_at of comments, which Forgejo stamps: a host clock 5 minutes ahead would skip the mentions of
        those 5 minutes. The host clock only when the header is missing or cannot be read."""
        try:
            stamp = parsedate_to_datetime(str((getattr(response, "headers", None) or {}).get("date") or ""))
            return stamp.timestamp() if stamp.tzinfo is not None else self._now()
        except (TypeError, ValueError, IndexError, OverflowError):
            return self._now()

    # -- version skew ----------------------------------------------------------------------------
    # The gateway keeps an adapter alive across `plugins update`, so its own methods are the old code while
    # `_mod` can hand it newer modules (measured on a server: a 0.0.17 adapter met the 0.0.19 poller and failed
    # every poll). Each poll compares the version it was built from with the loaded modules and the copy on
    # disk; on a mismatch it stops and says how to finish the update, instead of failing every 30 s.

    def _loaded_version(self) -> str:
        try:
            return str(_mod("version").VERSION)
        except Exception:
            return ""

    def _disk_version(self) -> str:
        try:
            found = re.search(r'^version:\s*"?([^"\s]+)"?', PLUGIN_YAML.read_text(encoding="utf-8"), re.M)
        except OSError:
            return ""
        return found.group(1) if found else ""

    def _skew(self) -> Optional[str]:
        """The newer version found, when the loaded modules or the copy on disk are not this adapter's."""
        for found in (self._loaded_version(), self._disk_version()):
            if found and self._version and found != self._version:
                return found
        return None

    def _stop_for_update(self, found: str) -> None:
        message = (f"Forgebrücke {found} is installed, but the mention channel still runs {self._version}. "
                   "Restart the gateway to finish the update.")
        self._event("update_pending", running=self._version, found=found, message=message)
        if self._state_dir is not None:
            try:
                self._state_dir.mkdir(parents=True, exist_ok=True)
                (self._state_dir / UPDATE_PENDING).write_text(json.dumps(
                    {"at": time.time(), "running": self._version, "found": found, "message": message}), encoding="utf-8")
            except OSError:
                pass
        self._alive = False
        self._mark_disconnected()

    # -- lifecycle -------------------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._token:
            reason = " ".join(self._token_problems) or "FORGEBRUECKE_BOT_ACCOUNT_TOKEN is not set"
            self._event("connect_refused", reason=reason)
            return False
        try:
            config = self._config()
        except Exception as e:
            self._event("connect_refused", reason=f"config unreadable: {type(e).__name__}")
            return False
        profile = _mod("profile")
        blockers = profile.channel_blockers(config, profile.current_name(self.home))
        if blockers:
            self._event("connect_refused", reason="; ".join(blockers))
            return False
        settings = self._settings(config)
        client_mod = _mod("client")
        self._client = client_mod.ForgejoClient(settings.url, self._token)
        try:
            me = await asyncio.to_thread(self._client.get, "/user")
        except client_mod.ForgejoError as e:
            self._event("connect_refused", reason=e.message)
            return False
        self._bot = (me.data or {}).get("login") or ""
        poller = _mod("channel.poller")
        ledger = poller.Ledger.load(self.ledger_path)
        if not ledger.since:  # first start: answer what comes next, not the history
            ledger.since = poller.iso(self._forge_now(me))
            ledger.save(self.ledger_path)
        self._take_lease()
        if self._state_dir is not None:  # this adapter runs the current code: an older one's note is void
            try:
                (self._state_dir / UPDATE_PENDING).unlink()
            except OSError:
                pass
        self._alive = True
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        self._event("connect", bot=self._bot, reconnect=is_reconnect, since=ledger.since)
        return True

    async def disconnect(self) -> None:
        self._alive = False
        self._running = False
        self._mark_disconnected()
        task, self._task = self._task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._event("disconnect")

    def _take_lease(self) -> None:
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.lease_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"iid": self.iid, "since": time.time()}), encoding="utf-8")
        tmp.replace(self.lease_path)

    def _holds_lease(self) -> bool:
        try:
            return json.loads(self.lease_path.read_text(encoding="utf-8")).get("iid") == self.iid
        except (OSError, ValueError):
            return False

    async def _loop(self) -> None:
        poller = _mod("channel.poller")
        while self._alive:
            interval = 120
            try:
                if not self._holds_lease():
                    self._event("stop", reason="a newer adapter holds the lease")
                    self._alive = False
                    self._mark_disconnected()
                    return
                found = self._skew()
                if found:
                    self._stop_for_update(found)
                    return
                config = self._config()
                if not _mod("profile").enabled_in(config):
                    if not self._dormant:
                        self._dormant = True
                        self._mark_disconnected()
                        self._event("dormant", reason="the plugin was disabled in this profile")
                    await asyncio.sleep(poller.MIN_INTERVAL_S)
                    continue
                if self._dormant:
                    self._dormant = False
                    self._mark_connected()
                    self._event("resume", reason="the plugin was enabled again")
                settings = self._settings(config)
                interval = max(poller.MIN_INTERVAL_S, settings.poll_interval)
                await self._poll_once(settings)
                await self._flows_tick(settings)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep polling; the next round retries
                self._event("poll_error", error=f"{type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(interval)

    async def _flows_tick(self, settings) -> None:
        """One flows tick on the gateway's loop, with this profile's token (flows/loop.py)."""
        try:
            report = await asyncio.to_thread(_mod("flows.loop").channel_tick, settings, self._token,
                                             self._state_dir)
        except Exception as e:
            self._event("flows_error", error=f"{type(e).__name__}: {str(e)[:200]}")
            return
        if report and (report.get("writes") or report.get("errors") or report.get("busy")):
            self._event("flows_tick", writes=report.get("writes"), done=(report.get("done") or [])[:10],
                        prs=len(report.get("prs") or []), errors=(report.get("errors") or [])[:3],
                        busy=report.get("busy", False))

    async def _poll_once(self, settings) -> None:
        """Every mention is saved as answered right after its delivery. The cursor is saved last, and only when
        nothing waits for another try, so a poll that stops halfway loses no mention."""
        poller = _mod("channel.poller")
        ledger = poller.Ledger.load(self.ledger_path)  # from disk each time: another adapter may have answered
        polled = ledger.copy()  # find_mentions moves this copy's cursor
        skips: list = []
        mentions = await asyncio.to_thread(poller.find_mentions, self._client, bot=self._bot, settings=settings,
                                           ledger=polled, now=self._now(),
                                           log=lambda event, data: skips.append(data))
        if mentions or skips:
            self._event("poll", found=len(mentions), skipped=skips[:5])
        if settings.dry_run:
            self._record_only(mentions)
        elif not await self._deliver_all(mentions, ledger):
            return  # the cursor stays, so the next poll finds the undelivered mention again
        ledger.take_cursor(polled)
        ledger.save(self.ledger_path)

    def _record_only(self, mentions: list) -> None:
        for m in mentions:
            if m.key not in self._dry_seen:
                self._event("deliver_skipped", key=m.key, author=m.author, reason="Preview is on")
        self._dry_seen = {m.key for m in mentions}

    async def _deliver_all(self, mentions: list, ledger) -> bool:
        """False when a mention was not delivered and gets another try. A mention Hermes took is not handed
        over again by this process: when the save after it failed, the next poll only saves."""
        delivered_all = True
        for m in mentions:
            if m.key not in self._handed:
                try:
                    await self._deliver(m)
                except Exception as e:
                    tries = self._failed[m.key] = self._failed.get(m.key, 0) + 1
                    self._event("deliver_failed", key=m.key, tries=tries, error=f"{type(e).__name__}: {str(e)[:160]}")
                    if tries < DELIVERY_TRIES:
                        delivered_all = False
                        continue
                self._handed.add(m.key)
            self._failed.pop(m.key, None)
            ledger.mark(m.key, m.thread, self._now())
            ledger.save(self.ledger_path)
            self._handed.discard(m.key)
        return delivered_all

    async def _deliver(self, m) -> None:
        poller = _mod("channel.poller")
        await self._react(m)
        message_id = str(m.comment_id or f"body-{m.number}")
        source = self.build_source(chat_id=m.thread, chat_name=f"{m.thread} {m.title}"[:120], chat_type="dm",
                                   user_id=m.author, user_name=m.author, message_id=message_id)
        self._event("deliver", key=m.key, author=m.author)
        await self.handle_message(MessageEvent(text=poller.frame(m, self._bot), message_type=MessageType.TEXT,
                                               source=source, message_id=message_id, raw_message=None,
                                               timestamp=datetime.now(timezone.utc)))

    async def _react(self, m) -> None:
        path = (f"/repos/{m.repo}/issues/comments/{m.comment_id}/reactions" if m.comment_id
                else f"/repos/{m.repo}/issues/{m.number}/reactions")
        try:
            await asyncio.to_thread(self._client.post, path, {"content": "eyes"})
        except Exception as e:
            self._event("react_failed", key=m.key, error=str(e)[:120])

    # -- outbound --------------------------------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        repo, _, number = str(chat_id).rpartition("#")
        if not self._client or not repo or not number.isdigit():
            return SendResult(success=False, error=f"not a Forgejo thread: {chat_id!r}")
        try:
            dry_run = self._settings().dry_run
        except Exception as e:
            self._event("send_failed", chat=chat_id, error=f"settings unreadable: {type(e).__name__}")
            return SendResult(success=False, error=f"Hermes' settings could not be read ({type(e).__name__}).")
        if dry_run:
            self._event("send_skipped", chat=chat_id, reason="Preview is on")
            return SendResult(success=False, error="Preview is on, so nothing was posted to Forgejo. "
                                                   "Switch it off on the Forgebrücke page.")
        poller = _mod("channel.poller")
        footer = _mod("tools.write").FOOTER
        parts = poller.split_reply(content, poller.REPLY_LIMIT - len(footer))
        last_id = None
        for i, part in enumerate(parts):
            body = part + footer if i == len(parts) - 1 else part
            try:
                resp = await asyncio.to_thread(self._client.post, f"/repos/{repo}/issues/{number}/comments", {"body": body})
            except Exception as e:
                self._event("send_failed", chat=chat_id, part=i, error=str(e)[:160])
                # Hermes sends a failed answer again as plain text unless it learns that a part is visible.
                return SendResult(success=False, error=str(e)[:200],
                                  raw_response={"partial_overflow": True} if i else None)
            last_id = (resp.data or {}).get("id")
        self._event("send", chat=chat_id, parts=len(parts), comment=last_id)
        return SendResult(success=True, message_id=str(last_id) if last_id else None)

    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str,
                                  reply_to: Optional[str] = None,
                                  metadata: Optional[dict[str, Any]] = None) -> SendResult:
        # A Forgejo thread is public. Hermes' setup notices are not for it.
        self._event("notice_dropped", chat=chat_id, text=str(content)[:80])
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm"}


def make_adapter(config: PlatformConfig, *, home: Path, data_dir: Optional[Path],
                 get_secret: Callable[[str], Optional[str]]) -> ForgejoMentionAdapter:
    return ForgejoMentionAdapter(config, home=home, data_dir=data_dir, get_secret=get_secret)
