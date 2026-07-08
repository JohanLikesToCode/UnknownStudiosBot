import sys
import os
import traceback
import warnings
import hashlib
import secrets
warnings.filterwarnings('ignore', category=SyntaxWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)

import logging
logging.basicConfig(level=logging.INFO)

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import aiohttp
import base64
from dataclasses import dataclass
import re
from github import Github, Auth

print("Starting bot initialization...")
print(f"Python version: {sys.version}")

# ─── Configuration ────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GITHUB_TOKEN  = os.getenv('GITHUB_TOKEN')
GITHUB_REPO   = os.getenv('GITHUB_REPO')
GUILD_ID      = os.getenv('GUILD_ID')

# Public webhook the bot posts status updates to (the channel your users see).
# NOTE: this was hardcoded with a live token in the original file. Please
# regenerate this webhook in Discord (Channel Settings > Integrations >
# Webhooks) and set STATUS_WEBHOOK_URL as an env var instead of relying on
# the fallback below.
STATUS_WEBHOOK = os.getenv(
    'STATUS_WEBHOOK_URL',
    "https://discord.com/api/webhooks/1499369589296070688/2BEPyenbkpWsy95aajJaP9XMmMDRWrHFhfALZkOIil7NFNoNPUtyw3mxksBbDn-mQunb"
)
ROLE_PING       = os.getenv('ROLE_PING', "<@&1499369803708633148>")
ALLOWED_ROLE_ID = int(os.getenv('ALLOWED_ROLE_ID', '1444271393570160680'))

# The "data message" - a message the bot owns and edits to persist internal
# state (webhook message ID tracking) across restarts, instead of committing
# a JSON file to GitHub every sync. Defaults to the message you linked:
# https://discord.com/channels/1407267319276896377/1524277928945258496/1524278108851798116
DATA_CHANNEL_ID = int(os.getenv('DATA_CHANNEL_ID', '1524277928945258496'))
DATA_MESSAGE_ID = int(os.getenv('DATA_MESSAGE_ID', '1524278108851798116'))

POLL_INTERVAL = 60
AUTO_RESOLVE_DAYS = int(os.getenv('AUTO_RESOLVE_DAYS', '50'))

print(f"Discord token present: {bool(DISCORD_TOKEN)}")
print(f"GitHub token present:  {bool(GITHUB_TOKEN)}")
print(f"GitHub repo:           {GITHUB_REPO}")

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN not set!")
    sys.exit(1)

# ─── File paths (GitHub) ──────────────────────────────────────────────────────
STATUS_FILE = "status.txt"  # still the source of truth for incident content

# ─── Status visuals ───────────────────────────────────────────────────────────
STATUS_EMOJIS = {
    "INVESTIGATING": "🔴",
    "MONITORING":    "🟡",
    "IDENTIFIED":    "🟠",
    "RESOLVED":      "🟢",
    "MAINTENANCE":   "🔧",
    "SCHEDULED":     "📅",
    "IN PROGRESS":   "⚙️",
    "COMPLETED":     "✅",
}

SEVERITY_EMOJIS = {
    'low': '🟢',
    'medium': '🟡',
    'high': '🟠',
    'critical': '🔴'
}

COMPONENT_OPTIONS = [
    'Seshy RuntimeEngine',
    'Seshy Modules',
    'Seshy Database',
    'Seshy AI',
    'GitHub Data Store',
    'Website'
]

STATUS_SELECT_OPTIONS = [
    discord.SelectOption(label="Investigating", value="INVESTIGATING", emoji="🔴"),
    discord.SelectOption(label="Monitoring",    value="MONITORING",    emoji="🟡"),
    discord.SelectOption(label="Identified",    value="IDENTIFIED",    emoji="🟠"),
    discord.SelectOption(label="Resolved",      value="RESOLVED",      emoji="🟢"),
    discord.SelectOption(label="Scheduled",     value="SCHEDULED",     emoji="📅"),
    discord.SelectOption(label="In Progress",   value="IN PROGRESS",   emoji="⚙️"),
    discord.SelectOption(label="Completed",     value="COMPLETED",     emoji="✅"),
    discord.SelectOption(label="Maintenance",   value="MAINTENANCE",   emoji="🔧"),
]

SEVERITY_SELECT_OPTIONS = [
    discord.SelectOption(label="Low",      value="low",      emoji="🟢"),
    discord.SelectOption(label="Medium",   value="medium",   emoji="🟡"),
    discord.SelectOption(label="High",     value="high",     emoji="🟠"),
    discord.SelectOption(label="Critical", value="critical", emoji="🔴"),
]

# ─── Date helpers (portable - avoids %-d which breaks on Windows) ────────────
def now_date_str() -> str:
    dt = datetime.now()
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"

def now_ts_str() -> str:
    dt = datetime.now()
    return f"{dt.strftime('%b')} {dt.day}, {dt.strftime('%H:%M')}"

_TS_RE = re.compile(r'^(\w{3})\s+(\d{1,2}),\s+(\d{2}):(\d{2})$')

def ts_to_discord(ts: str) -> str:
    """Convert 'Jul 8, 14:30' style timestamp into a Discord <t:...:f> tag."""
    if not ts or ts.startswith('<t:'):
        return ts
    m = _TS_RE.match(ts.strip())
    if not m:
        return ts
    mon, day, hh, mm = m.groups()
    try:
        dt = datetime.strptime(f"{mon} {day} {hh}:{mm}", "%b %d %H:%M").replace(year=datetime.now().year)
        return f"<t:{int(dt.timestamp())}:f>"
    except ValueError:
        return ts

# ─── Discord Bot Setup ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class DiscordBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self._status_hash: str = ""
        self._processing = False
        self._initialized = False
        self._webhook_ids: Dict[str, str] = {}   # incident_id -> webhook message id
        self._data_message: Optional[discord.Message] = None

    async def setup_hook(self):
        try:
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Synced commands to guild {GUILD_ID}")
            else:
                await self.tree.sync()
                print("Synced commands globally")
        except Exception as e:
            print(f"Error syncing commands: {e}")

        poll_status.start()

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        if not self._initialized:
            await load_data_store()

            raw = await get_file_content(STATUS_FILE)
            if raw:
                incidents = parse_status(raw)

                if not self._webhook_ids:
                    migrated = await migrate_legacy_webhook_ids(incidents)
                    if migrated:
                        self._webhook_ids = migrated
                        await save_webhook_ids(migrated)

                if auto_resolve_stale(incidents):
                    raw = format_status(incidents)
                    await commit_file(STATUS_FILE, raw, "Auto-resolve stale incidents", "auto-resolver")

                self._status_hash = hashlib.sha256(raw.encode()).hexdigest()

                active_incidents = [
                    inc for inc in incidents
                    if not inc.get('no_incidents') and inc.get('title') and inc.get('updates')
                    and inc['updates'][-1].get('status', '').upper() not in ('RESOLVED', 'COMPLETED')
                ]
                if active_incidents:
                    print(f"Found {len(active_incidents)} active incidents on startup")
                await sync_webhooks(incidents)
            self._initialized = True
            print("Bot initialized - monitoring for status changes")

bot = DiscordBot()

# ─── GitHub Client ────────────────────────────────────────────────────────────
try:
    if GITHUB_TOKEN and GITHUB_REPO:
        auth = Auth.Token(GITHUB_TOKEN)
        github_client = Github(auth=auth)
        repo = github_client.get_repo(GITHUB_REPO)
        print("GitHub client initialized successfully")
    else:
        github_client = None
        repo = None
        print("WARNING: GitHub token or repo not configured!")
except Exception as e:
    print(f"Error initializing GitHub client: {e}")
    github_client = None
    repo = None

# ─── GitHub Helpers (status.txt only) ─────────────────────────────────────────
async def get_file_content(file_path: str) -> str:
    if not repo:
        return ""
    try:
        contents = repo.get_contents(file_path)
        return base64.b64decode(contents.content).decode('utf-8')
    except Exception as e:
        print(f"Error fetching {file_path}: {e}")
        return ""

async def commit_file(file_path: str, content: str, commit_message: str, author: str) -> bool:
    if not repo:
        return False
    try:
        try:
            contents = repo.get_contents(file_path)
            repo.update_file(file_path, f"{commit_message} (by {author})", content, contents.sha)
        except Exception:
            repo.create_file(file_path, f"{commit_message} (by {author})", content)
        return True
    except Exception as e:
        print(f"GitHub commit error: {e}")
        return False

# ─── Data Store (Discord message, replaces webhook_message_ids.json) ─────────
_DATA_JSON_RE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)

async def load_data_store():
    """Fetch the data message and populate bot._webhook_ids from it."""
    try:
        channel = bot.get_channel(DATA_CHANNEL_ID) or await bot.fetch_channel(DATA_CHANNEL_ID)
    except Exception as e:
        print(f"Cannot access data channel {DATA_CHANNEL_ID}: {e}")
        bot._data_message = None
        bot._webhook_ids = {}
        return

    try:
        msg = await channel.fetch_message(DATA_MESSAGE_ID)
    except Exception as e:
        print(f"Data message {DATA_MESSAGE_ID} not found ({e}); creating a new one.")
        try:
            msg = await channel.send(
                "📊 **Status Bot Data Store** — internal use only, do not delete or edit manually.\n"
                "```json\n{}\n```"
            )
            print(f"NEW DATA MESSAGE ID: {msg.id} — set the DATA_MESSAGE_ID env var to this "
                  f"value so it persists across restarts.")
        except Exception as e2:
            print(f"Could not create a data message either: {e2}")
            bot._data_message = None
            bot._webhook_ids = {}
            return

    bot._data_message = msg
    match = _DATA_JSON_RE.search(msg.content or "")
    try:
        bot._webhook_ids = json.loads(match.group(1)) if match else {}
    except Exception:
        bot._webhook_ids = {}
    print(f"Loaded {len(bot._webhook_ids)} tracked webhook message id(s) from data message {msg.id}")

async def save_webhook_ids(ids: dict) -> bool:
    bot._webhook_ids = ids
    content = (
        "📊 **Status Bot Data Store** — internal use only, do not delete or edit manually.\n"
        f"```json\n{json.dumps(ids, indent=2)}\n```"
    )
    if len(content) > 1900:
        print("WARNING: data message content is approaching Discord's 2000 char limit.")

    try:
        if bot._data_message is None:
            channel = bot.get_channel(DATA_CHANNEL_ID) or await bot.fetch_channel(DATA_CHANNEL_ID)
            bot._data_message = await channel.fetch_message(DATA_MESSAGE_ID)
        await bot._data_message.edit(content=content)
        return True
    except Exception as e:
        print(f"Failed to save data message: {e}")
        # Try one more time with a fresh fetch in case the cached message object is stale
        try:
            channel = bot.get_channel(DATA_CHANNEL_ID) or await bot.fetch_channel(DATA_CHANNEL_ID)
            msg = await channel.fetch_message(DATA_MESSAGE_ID)
            await msg.edit(content=content)
            bot._data_message = msg
            return True
        except Exception as e2:
            print(f"Retry also failed: {e2}")
            return False

LEGACY_WEBHOOK_IDS_FILE = "webhook_message_ids.json"

async def migrate_legacy_webhook_ids(incidents: List[Dict]) -> Dict[str, str]:
    """One-time migration: the previous version of this bot tracked webhook
    message IDs in webhook_message_ids.json on GitHub, keyed by
    'date|type|title'. If the new data message is empty, pull that old file
    (if it's still there) and remap it onto the new stable incident IDs, so
    a restart doesn't treat every open incident as brand new and re-post it."""
    raw = await get_file_content(LEGACY_WEBHOOK_IDS_FILE)
    if not raw:
        return {}
    try:
        legacy = json.loads(raw)
    except Exception:
        return {}
    if not legacy:
        return {}

    migrated: Dict[str, str] = {}
    for inc in incidents:
        if inc.get('no_incidents') or not inc.get('title') or not inc.get('id'):
            continue
        legacy_key = f"{inc['date']}|{inc.get('type','INCIDENT')}|{inc['title']}"
        if legacy_key in legacy:
            migrated[inc['id']] = legacy[legacy_key]

    if migrated:
        print(f"Migrated {len(migrated)} tracked webhook message id(s) from legacy {LEGACY_WEBHOOK_IDS_FILE}")
    return migrated

# ─── Status Parsing ───────────────────────────────────────────────────────────
_DATE_HEADER = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$')
_ID_LINE = re.compile(r'^ID:\s+(\S+)$')
_UPDATE_STRUCTURED = re.compile(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w[\w ]*?)\s+-\s+(.+)$')
_UPDATE_BARE = re.compile(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+(.+)$')

def _finalize(inc: Optional[Dict]) -> Optional[Dict]:
    """Ensure a completed incident has a stable ID before it's used anywhere."""
    if inc and not inc.get('no_incidents') and inc.get('title') and not inc.get('id'):
        # Deterministic fallback for legacy status.txt entries with no ID line,
        # so the same incident maps to the same ID across parses.
        inc['id'] = hashlib.sha1(f"{inc['date']}|{inc['title']}".encode()).hexdigest()[:6]
    return inc

def parse_status(content: str) -> List[Dict]:
    incidents: List[Dict] = []
    current: Optional[Dict] = None

    for raw_line in content.split('\n'):
        line = raw_line.rstrip()

        if not line:
            if current is not None:
                incidents.append(_finalize(current))
                current = None
            continue

        if _DATE_HEADER.match(line):
            if current is not None:
                incidents.append(_finalize(current))
            current = {
                'date': line,
                'id': None,
                'type': None,
                'title': None,
                'severity': None,
                'components': '',
                'updates': [],
                'no_incidents': False,
            }
            continue

        if current is None:
            continue

        if line.strip().lower() == 'no incidents reported.':
            current['no_incidents'] = True
            continue

        m = _ID_LINE.match(line)
        if m:
            current['id'] = m.group(1).strip()
            continue

        m = re.match(r'^(INCIDENT|MAINTENANCE):\s+(.+)$', line)
        if m:
            current['type'] = m.group(1)
            current['title'] = m.group(2)
            continue

        m = re.match(r'^SEVERITY:\s+(.+)$', line)
        if m:
            current['severity'] = m.group(1).strip().lower()
            continue

        m = re.match(r'^COMPONENTS:\s+(.+)$', line)
        if m:
            current['components'] = m.group(1).strip()
            continue

        m = _UPDATE_STRUCTURED.match(line)
        if m:
            current['updates'].append({
                'timestamp': m.group(1).strip(),
                'status': m.group(2).strip().upper(),
                'description': m.group(3).strip(),
            })
            continue

        m = _UPDATE_BARE.match(line)
        if m:
            current['updates'].append({
                'timestamp': m.group(1).strip(),
                'status': '',
                'description': m.group(2).strip(),
            })
            continue

    if current is not None:
        incidents.append(_finalize(current))

    return incidents

def format_status(incidents: List[Dict]) -> str:
    blocks = []
    for inc in incidents:
        lines = [inc['date']]
        if inc.get('no_incidents'):
            lines.append('No incidents reported.')
        else:
            if not inc.get('id'):
                inc['id'] = secrets.token_hex(3)
            lines.append(f"ID: {inc['id']}")
            lines.append(f"{inc.get('type','INCIDENT')}: {inc.get('title','Untitled')}")
            lines.append(f"SEVERITY: {inc.get('severity','medium')}")
            lines.append(f"COMPONENTS: {inc.get('components','')}")
            for u in inc.get('updates', []):
                ts = u['timestamp']
                stat = u.get('status', '')
                desc = u['description']
                if stat:
                    lines.append(f"{ts} - {stat} - {desc}")
                else:
                    lines.append(f"{ts} {desc}")
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks) + '\n'

def _parse_incident_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%b %d, %Y")
    except ValueError:
        return None

def auto_resolve_stale(incidents: List[Dict]) -> bool:
    """Auto-resolve any incident that's been open longer than AUTO_RESOLVE_DAYS
    days and hasn't already been marked resolved/completed. Mutates incidents
    in place. Returns True if anything changed (caller should re-save)."""
    changed = False
    cutoff = datetime.now() - timedelta(days=AUTO_RESOLVE_DAYS)

    for inc in incidents:
        if inc.get('no_incidents') or not inc.get('title'):
            continue
        updates = inc.get('updates', [])
        if not updates:
            continue
        last_status = updates[-1].get('status', '').upper()
        if last_status in ('RESOLVED', 'COMPLETED'):
            continue

        inc_date = _parse_incident_date(inc['date'])
        if inc_date and inc_date < cutoff:
            auto_status = 'COMPLETED' if inc.get('type') == 'MAINTENANCE' else 'RESOLVED'
            updates.append({
                'timestamp': now_ts_str(),
                'status': auto_status,
                'description': f"Automatically marked {auto_status.lower()} after "
                                f"{AUTO_RESOLVE_DAYS} days with no update.",
            })
            changed = True
            print(f"Auto-resolved stale incident {inc.get('id')}: {inc.get('title')}")

    return changed

# ─── Public Webhook Management (status channel) ───────────────────────────────
def format_webhook_message(incident: Dict) -> str:
    inc_type = incident.get('type', 'INCIDENT')
    title = incident.get('title', 'Unknown Incident')
    severity = incident.get('severity', 'medium')
    updates = incident.get('updates', [])
    comps = incident.get('components', '')

    emoji = "🚨" if inc_type == 'INCIDENT' else "🔧"
    sev_e = SEVERITY_EMOJIS.get(severity, '⚪')

    lines = [
        f"-# {ROLE_PING}",
        f"# {emoji} {title}",
        f"-# {sev_e} Severity: **{severity.upper()}** • {inc_type} • Components: {comps}",
        "",
    ]

    if updates:
        for u in updates:
            ts = ts_to_discord(u.get('timestamp', ''))
            stat = u.get('status', '').upper()
            desc = u.get('description', '')

            if stat:
                stat_emoji = STATUS_EMOJIS.get(stat, '⚪')
                lines.append(f"{ts} — {stat_emoji} **{stat}**")
                lines.append(f"> {desc}")
            else:
                lines.append(f"{ts}")
                lines.append(f"> {desc}")
            lines.append("")
    else:
        lines.append("*No updates yet.*")

    return '\n'.join(lines)

async def update_webhook_message(msg_id: str, incident: Dict) -> bool:
    content = format_webhook_message(incident)
    payload = {"content": content}
    async with aiohttp.ClientSession() as session:
        url = f"{STATUS_WEBHOOK}/messages/{msg_id}"
        async with session.patch(url, json=payload) as resp:
            if resp.status == 200:
                return True
            text = await resp.text()
            print(f"Webhook PATCH failed ({resp.status}): {text}")
            return False

async def create_webhook_message(incident: Dict) -> Optional[str]:
    content = format_webhook_message(incident)
    payload = {"content": content}
    async with aiohttp.ClientSession() as session:
        url = f"{STATUS_WEBHOOK}?wait=true"
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                return str(data['id'])
            text = await resp.text()
            print(f"Webhook POST failed ({resp.status}): {text}")
            return None

async def delete_webhook_message(msg_id: str):
    async with aiohttp.ClientSession() as session:
        await session.delete(f"{STATUS_WEBHOOK}/messages/{msg_id}")

async def sync_webhooks(incidents: List[Dict]):
    """Sync webhook messages - update existing ones, only create if truly new.
    Tracking is keyed by stable incident ID, so renaming an incident's title
    no longer creates a duplicate message. Resolved incidents get one final
    update and are dropped from tracking (message stays in the channel)."""
    ids = dict(bot._webhook_ids)
    new_ids: Dict[str, str] = {}

    for inc in incidents:
        if inc.get('no_incidents') or not inc.get('title') or not inc.get('id'):
            continue

        key = inc['id']
        existing_id = ids.get(key)

        updates = inc.get('updates', [])
        is_resolved = bool(updates) and updates[-1].get('status', '').upper() in ('RESOLVED', 'COMPLETED')

        if existing_id:
            if is_resolved:
                await update_webhook_message(existing_id, inc)
                print(f"Final update for resolved incident {key} - removed from tracking")
            else:
                success = await update_webhook_message(existing_id, inc)
                if success:
                    new_ids[key] = existing_id
                    print(f"Updated existing message {existing_id} for incident {key}")
                elif updates:
                    new_id = await create_webhook_message(inc)
                    if new_id:
                        new_ids[key] = new_id
                        print(f"Recreated message {new_id} for incident {key} (original was deleted)")
        else:
            if updates and not is_resolved:
                new_id = await create_webhook_message(inc)
                if new_id:
                    new_ids[key] = new_id
                    print(f"Created new message {new_id} for incident {key}")
            elif updates and is_resolved:
                new_id = await create_webhook_message(inc)
                if new_id:
                    print(f"Posted already-resolved incident {key} - not tracking")

    for key in ids:
        if key not in new_ids:
            print(f"Removed from tracking (message kept in channel): {key}")

    await save_webhook_ids(new_ids)
    print(f"Tracking {len(new_ids)} active webhook message(s)")

# ─── Background Poller ────────────────────────────────────────────────────────
@tasks.loop(seconds=POLL_INTERVAL)
async def poll_status():
    """Periodically fetch status.txt and sync if changed."""
    if bot._processing:
        return

    try:
        raw = await get_file_content(STATUS_FILE)
        if not raw:
            return

        bot._processing = True
        incidents = parse_status(raw)

        if auto_resolve_stale(incidents):
            raw = format_status(incidents)
            await commit_file(STATUS_FILE, raw, "Auto-resolve stale incidents", "auto-resolver")

        new_hash = hashlib.sha256(raw.encode()).hexdigest()
        if new_hash != bot._status_hash:
            print("[poll_status] status.txt changed — syncing webhooks")
            await sync_webhooks(incidents)
            bot._status_hash = new_hash

        bot._processing = False
    except Exception as e:
        print(f"[poll_status] error: {e}")
        bot._processing = False

@poll_status.before_loop
async def before_poll():
    await bot.wait_until_ready()

# ─── Session State ────────────────────────────────────────────────────────────
@dataclass
class Session:
    raw: str
    author: discord.Member
    interaction: discord.Interaction
    page: int = 0
    items_per_page: int = 5

sessions: Dict[int, Session] = {}

def find_incident(incidents: List[Dict], incident_id: str) -> Optional[Dict]:
    return next((i for i in incidents if i.get('id') == incident_id), None)

# ─── Status View with Pagination ──────────────────────────────────────────────
class StatusPaginationView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

        self.add_item(ActionDropdown(session))

        incidents = parse_status(session.raw)
        active_incidents = [i for i in incidents if not i.get('no_incidents') and i.get('title')]
        total_pages = max(1, (len(active_incidents) + session.items_per_page - 1) // session.items_per_page)

        if total_pages > 1:
            prev_btn = discord.ui.Button(label="◀️ Previous", style=discord.ButtonStyle.secondary, row=1)
            page_indicator = discord.ui.Button(
                label=f"Page {session.page + 1}/{total_pages}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1
            )
            next_btn = discord.ui.Button(label="Next ▶️", style=discord.ButtonStyle.secondary, row=1)

            prev_btn.callback = self.prev_page
            next_btn.callback = self.next_page

            if session.page == 0:
                prev_btn.disabled = True
            if session.page >= total_pages - 1:
                next_btn.disabled = True

            self.add_item(prev_btn)
            self.add_item(page_indicator)
            self.add_item(next_btn)

    async def prev_page(self, interaction: discord.Interaction):
        self.session.page -= 1
        await interaction.response.edit_message(
            embed=build_status_embed(self.session),
            view=StatusPaginationView(self.session)
        )

    async def next_page(self, interaction: discord.Interaction):
        self.session.page += 1
        await interaction.response.edit_message(
            embed=build_status_embed(self.session),
            view=StatusPaginationView(self.session)
        )

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

# ─── Action Dropdown ──────────────────────────────────────────────────────────
class ActionDropdown(discord.ui.Select):
    def __init__(self, session: Session):
        self.session = session

        options = [
            discord.SelectOption(label="➕ New Incident", value="new_incident", emoji="🚨"),
            discord.SelectOption(label="🔧 New Maintenance", value="new_maintenance", emoji="🔧"),
            discord.SelectOption(label="✏️ Edit Incident", value="edit", emoji="✏️"),
            discord.SelectOption(label="📝 Add Update", value="add_update", emoji="📝"),
            discord.SelectOption(label="🗑️ Delete Incident", value="delete", emoji="🗑️"),
            discord.SelectOption(label="💾 Save & Post", value="save", emoji="💾"),
        ]

        super().__init__(
            placeholder="Choose an action...",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]

        if action == "new_incident":
            await self.start_incident_creation(interaction, "INCIDENT")
        elif action == "new_maintenance":
            await self.start_incident_creation(interaction, "MAINTENANCE")
        elif action == "edit":
            await self.select_incident(interaction, self.go_to_edit, "edit")
        elif action == "add_update":
            await self.select_incident(interaction, self.go_to_update, "add an update to")
        elif action == "delete":
            await self.select_incident(interaction, self.go_to_delete, "delete")
        elif action == "save":
            await self.save_and_sync(interaction)

    async def start_incident_creation(self, interaction: discord.Interaction, inc_type: str):
        new_inc = {
            'date': now_date_str(),
            'id': None,
            'type': inc_type,
            'title': '',
            'severity': 'medium' if inc_type == 'INCIDENT' else 'maintenance',
            'components': '',
            'updates': [],
            'no_incidents': False,
        }
        await interaction.response.send_modal(TitleModal(self.session, new_inc))

    def _active_incidents(self):
        incidents = parse_status(self.session.raw)
        return [i for i in incidents if not i.get('no_incidents') and i.get('title')]

    async def select_incident(self, interaction: discord.Interaction, next_step, verb: str):
        active_incidents = self._active_incidents()
        if not active_incidents:
            await interaction.response.send_message("No incidents to work with!", ephemeral=True)
            return

        options = []
        for inc in active_incidents:
            emoji = "🚨" if inc.get('type') == 'INCIDENT' else "🔧"
            options.append(discord.SelectOption(
                label=inc.get('title', 'Untitled')[:80],
                value=inc['id'],
                emoji=emoji
            ))

        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder=f"Select incident to {verb}...", options=options)

        async def picked(inter: discord.Interaction):
            target = find_incident(active_incidents, select.values[0])
            await next_step(inter, target)

        select.callback = picked
        view.add_item(select)
        await interaction.response.send_message(f"Select incident to {verb}:", view=view, ephemeral=True)

    async def go_to_edit(self, interaction: discord.Interaction, target_inc: Dict):
        await interaction.response.send_modal(EditIncidentModal(self.session, target_inc))

    async def go_to_update(self, interaction: discord.Interaction, target_inc: Dict):
        view = discord.ui.View(timeout=60)
        status_select = discord.ui.Select(
            placeholder="Select status for this update...",
            options=STATUS_SELECT_OPTIONS
        )

        async def status_picked(inter: discord.Interaction):
            await inter.response.send_modal(
                UpdateDescriptionModal(self.session, target_inc, status_select.values[0])
            )

        status_select.callback = status_picked
        view.add_item(status_select)
        await interaction.response.send_message("Select status for this update:", view=view, ephemeral=True)

    async def go_to_delete(self, interaction: discord.Interaction, target_inc: Dict):
        incidents = parse_status(self.session.raw)
        incidents = [i for i in incidents if i.get('id') != target_inc['id']]
        self.session.raw = format_status(incidents)

        embed = build_status_embed(self.session)
        embed.color = 0xFF0000
        embed.set_footer(text=f"✅ Deleted: {target_inc.get('title')}")

        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

    async def save_and_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        ok = await commit_file(STATUS_FILE, self.session.raw, "Update status", str(self.session.author))

        if not ok:
            await interaction.followup.send("❌ Failed to save to GitHub! Check token.", ephemeral=True)
            return

        bot._status_hash = hashlib.sha256(self.session.raw.encode()).hexdigest()

        incidents = parse_status(self.session.raw)
        await sync_webhooks(incidents)

        await interaction.followup.send("✅ Saved to GitHub and synced to webhook!", ephemeral=True)

# ─── Modals ───────────────────────────────────────────────────────────────────
class TitleModal(discord.ui.Modal):
    """Step 1: title + initial description in one modal (fewer round trips)."""
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Step 1: Incident Details")
        self.session = session
        self.incident = incident

        self.title_input = discord.ui.TextInput(
            label="Title",
            placeholder="Brief description",
            max_length=100,
            required=True
        )
        self.description = discord.ui.TextInput(
            label="Initial Description",
            placeholder="What is the current situation?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True
        )
        self.add_item(self.title_input)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        self.incident['title'] = self.title_input.value.strip()
        self.incident['_pending_description'] = self.description.value.strip()

        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Select severity level...", options=SEVERITY_SELECT_OPTIONS)

        async def severity_callback(inter: discord.Interaction):
            self.incident['severity'] = select.values[0]
            await inter.response.send_message(
                "Step 3: Select affected components:",
                view=ComponentsSelectView(self.session, self.incident),
                ephemeral=True
            )

        select.callback = severity_callback
        view.add_item(select)
        await interaction.response.send_message("Step 2: Select severity:", view=view, ephemeral=True)

class ComponentsSelectView(discord.ui.View):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(timeout=120)
        self.session = session
        self.incident = incident
        self.selected_components: List[str] = []

        options = [discord.SelectOption(label=comp, value=comp) for comp in COMPONENT_OPTIONS]

        self.component_select = discord.ui.Select(
            placeholder="Select components (can select multiple)...",
            min_values=1,
            max_values=len(COMPONENT_OPTIONS),
            options=options
        )
        self.component_select.callback = self.on_component_select
        self.add_item(self.component_select)

        confirm_btn = discord.ui.Button(label="✅ Confirm & Create", style=discord.ButtonStyle.success, row=1)
        confirm_btn.callback = self.confirm_components
        self.add_item(confirm_btn)

    async def on_component_select(self, interaction: discord.Interaction):
        self.selected_components = self.component_select.values
        await interaction.response.defer()

    async def confirm_components(self, interaction: discord.Interaction):
        if not self.selected_components:
            await interaction.response.send_message("Please select at least one component!", ephemeral=True)
            return

        self.incident['components'] = ', '.join(self.selected_components)

        view = discord.ui.View(timeout=60)
        default_status = 'SCHEDULED' if self.incident.get('type') == 'MAINTENANCE' else 'INVESTIGATING'
        status_select = discord.ui.Select(
            placeholder=f"Select status (default: {default_status})...",
            options=STATUS_SELECT_OPTIONS
        )

        async def status_callback(inter: discord.Interaction):
            self.incident['id'] = secrets.token_hex(3)
            self.incident['updates'].append({
                'timestamp': now_ts_str(),
                'status': status_select.values[0],
                'description': self.incident.pop('_pending_description', '(no description provided)'),
            })

            incidents = parse_status(self.session.raw)
            incidents = [i for i in incidents if not i.get('no_incidents')]
            incidents.insert(0, self.incident)
            self.session.raw = format_status(incidents)

            embed = build_status_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Incident created! Use 'Save & Post' to publish.")

            await inter.response.edit_message(
                embed=embed,
                view=StatusPaginationView(self.session)
            )

        status_select.callback = status_callback
        view.add_item(status_select)
        await interaction.response.send_message("Step 4: Select initial status:", view=view, ephemeral=True)

class UpdateDescriptionModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict, status: str):
        super().__init__(title="Add Update - Description")
        self.session = session
        self.incident_id = incident['id']
        self.status = status

        self.description = discord.ui.TextInput(
            label="Description",
            placeholder="What's the current status update?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True
        )
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        target = find_incident(incidents, self.incident_id)
        if target is None:
            await interaction.response.send_message("That incident no longer exists.", ephemeral=True)
            return

        target['updates'].append({
            'timestamp': now_ts_str(),
            'status': self.status,
            'description': self.description.value.strip(),
        })

        self.session.raw = format_status(incidents)

        embed = build_status_embed(self.session)
        embed.color = 0x57F287
        embed.set_footer(text="✅ Update added! Use 'Save & Post' to publish.")

        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

class EditIncidentModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Edit Incident")
        self.session = session
        self.incident_id = incident['id']

        self.title_input = discord.ui.TextInput(
            label="Title",
            default=incident.get('title', ''),
            max_length=100,
            required=True
        )
        self.add_item(self.title_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_title = self.title_input.value.strip()

        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Select severity...", options=SEVERITY_SELECT_OPTIONS)

        async def severity_callback(inter: discord.Interaction):
            incidents = parse_status(self.session.raw)
            target = find_incident(incidents, self.incident_id)
            if target is None:
                await inter.response.send_message("That incident no longer exists.", ephemeral=True)
                return

            current_comps = [c.strip() for c in target.get('components', '').split(',') if c.strip()]
            await inter.response.send_message(
                "Select components:",
                view=EditComponentsView(self.session, self.incident_id, new_title, select.values[0], current_comps),
                ephemeral=True
            )

        select.callback = severity_callback
        view.add_item(select)
        await interaction.response.send_message("Select severity:", view=view, ephemeral=True)

class EditComponentsView(discord.ui.View):
    def __init__(self, session: Session, incident_id: str, new_title: str, new_severity: str, current_comps: List[str]):
        super().__init__(timeout=120)
        self.session = session
        self.incident_id = incident_id
        self.new_title = new_title
        self.new_severity = new_severity
        self.selected_components = current_comps

        options = [
            discord.SelectOption(label=comp, value=comp, default=comp in current_comps)
            for comp in COMPONENT_OPTIONS
        ]

        self.component_select = discord.ui.Select(
            placeholder="Select components (can select multiple)...",
            min_values=1,
            max_values=len(COMPONENT_OPTIONS),
            options=options
        )
        self.component_select.callback = self.on_component_select
        self.add_item(self.component_select)

        confirm_btn = discord.ui.Button(label="✅ Confirm Edit", style=discord.ButtonStyle.success, row=1)
        confirm_btn.callback = self.confirm_edit
        self.add_item(confirm_btn)

    async def on_component_select(self, interaction: discord.Interaction):
        self.selected_components = self.component_select.values
        await interaction.response.defer()

    async def confirm_edit(self, interaction: discord.Interaction):
        if not self.selected_components:
            await interaction.response.send_message("Please select at least one component!", ephemeral=True)
            return

        incidents = parse_status(self.session.raw)
        target = find_incident(incidents, self.incident_id)
        if target is None:
            await interaction.response.send_message("That incident no longer exists.", ephemeral=True)
            return

        target['title'] = self.new_title
        target['severity'] = self.new_severity
        target['components'] = ', '.join(self.selected_components)

        self.session.raw = format_status(incidents)

        embed = build_status_embed(self.session)
        embed.color = 0x57F287
        embed.set_footer(text="✅ Incident updated! Use 'Save & Post' to publish.")

        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

# ─── Status Embed Builder ─────────────────────────────────────────────────────
def build_status_embed(session: Session) -> discord.Embed:
    incidents = parse_status(session.raw)

    embed = discord.Embed(title="🛠️ Status Editor", color=0x5865F2)
    embed.set_footer(text=f"Editor: {session.author.display_name}")

    active_incidents = [i for i in incidents if not i.get('no_incidents')]

    if not active_incidents:
        embed.description = "*No active incidents.*"
        return embed

    start = session.page * session.items_per_page
    end = start + session.items_per_page
    page_incidents = active_incidents[start:end]

    total_pages = max(1, (len(active_incidents) + session.items_per_page - 1) // session.items_per_page)

    for i, inc in enumerate(page_incidents):
        actual_idx = start + i
        emoji = "🚨" if inc.get('type') == 'INCIDENT' else "🔧"
        sev = inc.get('severity', '?')
        sev_e = SEVERITY_EMOJIS.get(sev, '⚪')
        updates = inc.get('updates', [])

        last_status = "No updates"
        last_emoji = '⚪'
        if updates:
            last = updates[-1]
            last_status = last.get('status') or 'free-text update'
            last_emoji = STATUS_EMOJIS.get(last_status.upper(), '⚪')

        title = inc.get('title', 'Untitled')
        if len(title) > 60:
            title = title[:57] + "..."

        val = (
            f"{sev_e} Severity: **{sev}**\n"
            f"🔧 Components: `{inc.get('components','—')}`\n"
            f"📝 Updates: {len(updates)}\n"
            f"{last_emoji} Latest: **{last_status}**\n"
            f"🆔 `{inc.get('id','?')}`"
        )

        embed.add_field(
            name=f"{emoji} [{actual_idx+1}] {title} — {inc['date']}",
            value=val,
            inline=False
        )

    if total_pages > 1:
        embed.set_footer(text=f"Page {session.page + 1}/{total_pages} • Editor: {session.author.display_name}")

    return embed

# ─── Slash Commands ───────────────────────────────────────────────────────────
def has_permission(member: discord.Member) -> bool:
    return any(role.id == ALLOWED_ROLE_ID for role in member.roles)

@bot.tree.command(name="edit_status", description="Edit the status page content")
async def edit_status(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    raw = await get_file_content(STATUS_FILE) or ""
    session = Session(raw=raw, author=interaction.user, interaction=interaction)
    sessions[interaction.user.id] = session

    await interaction.followup.send(
        embed=build_status_embed(session),
        view=StatusPaginationView(session),
        ephemeral=True
    )

# ─── Keep-alive ───────────────────────────────────────────────────────────────
from flask import Flask
from threading import Thread

app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Bot is online!"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    print(f"Starting web server on port {port}")
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

if __name__ == "__main__":
    keep_alive()
    print("Starting Discord bot...")
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
