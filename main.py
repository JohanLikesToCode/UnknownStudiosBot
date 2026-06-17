import sys
import os
import traceback
import warnings
import hashlib
warnings.filterwarnings('ignore', category=SyntaxWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)

import logging
logging.basicConfig(level=logging.INFO)

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
import aiohttp
import base64
from dataclasses import dataclass, field
from enum import Enum
import re
from github import Github, Auth

print("Starting bot initialization...")
print(f"Python version: {sys.version}")

DISCORD_TOKEN  = os.getenv('DISCORD_TOKEN')
GITHUB_TOKEN   = os.getenv('GITHUB_TOKEN')
GITHUB_REPO    = os.getenv('GITHUB_REPO')
GUILD_ID       = os.getenv('GUILD_ID')
STATUS_WEBHOOK = "https://discord.com/api/webhooks/1499369589296070688/2BEPyenbkpWsy95aajJaP9XMmMDRWrHFhfALZkOIil7NFNoNPUtyw3mxksBbDn-mQunb"
ROLE_PING      = "<@&1499369803708633148>"
ALLOWED_ROLE_ID = 1444271393570160680

POLL_INTERVAL = 60

print(f"Discord token present: {bool(DISCORD_TOKEN)}")
print(f"GitHub token present:  {bool(GITHUB_TOKEN)}")
print(f"GitHub repo:           {GITHUB_REPO}")

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN not set!")
    sys.exit(1)

STATUS_FILE      = "status.txt"
CHANGELOG_FILE   = "changelog.txt"
TEAM_FILE        = "teams.json"
BLOG_FILE        = "blogs.txt"
WEBHOOK_IDS_FILE = "webhook_message_ids.json"

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
INCIDENT_EMOJIS = {
    "INCIDENT":    "🚨",
    "MAINTENANCE": "🔧",
}
SEV_EMOJI = {
    'low': '🟢', 'medium': '🟡', 'high': '🟠',
    'critical': '🔴', 'maintenance': '🔧'
}

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class DiscordBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self._status_hash: str = ""

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
        print(f'Bot is in {len(self.guilds)} guilds')

bot = DiscordBot()

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

_DATE_HEADER = re.compile(
    r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$'
)
_UPDATE_STRUCTURED = re.compile(
    r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w[\w ]*?)\s+-\s+(.+)$'
)
_UPDATE_BARE = re.compile(
    r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+(.+)$'
)


def parse_status(content: str) -> List[Dict]:
    incidents: List[Dict] = []
    current: Optional[Dict] = None

    for raw_line in content.split('\n'):
        line = raw_line.rstrip()

        if not line:
            if current is not None:
                incidents.append(current)
                current = None
            continue

        if _DATE_HEADER.match(line):
            if current is not None:
                incidents.append(current)
            current = {
                'date': line,
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
        incidents.append(current)

    return incidents


def format_status(incidents: List[Dict]) -> str:
    blocks = []
    for inc in incidents:
        lines = [inc['date']]
        if inc.get('no_incidents'):
            lines.append('No incidents reported.')
        else:
            lines.append(f"{inc.get('type','INCIDENT')}: {inc.get('title','Untitled')}")
            lines.append(f"SEVERITY: {inc.get('severity','medium')}")
            lines.append(f"COMPONENTS: {inc.get('components','')}")
            for u in inc.get('updates', []):
                ts   = u['timestamp']
                stat = u.get('status', '')
                desc = u['description']
                if stat:
                    lines.append(f"{ts} - {stat} - {desc}")
                else:
                    lines.append(f"{ts} {desc}")
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks) + '\n'


def parse_changelog(content: str) -> List[Dict]:
    versions = []
    current = None
    for line in content.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^VERSION\s+(.+)$', line)
        if m:
            if current:
                versions.append(current)
            current = {'version': m.group(1), 'entries': []}
            continue
        if current is None:
            continue
        m = re.match(r'^DATE\s+(.+)$', line)
        if m:
            current['date'] = m.group(1)
            continue
        m = re.match(r'^(\w+)\s+(.+)$', line)
        if m:
            current['entries'].append({'type': m.group(1), 'description': m.group(2)})
    if current:
        versions.append(current)
    return versions

def format_changelog(versions: List[Dict]) -> str:
    lines = []
    for v in versions:
        lines.append(f"VERSION {v['version']}")
        lines.append(f"DATE {v.get('date', datetime.now().strftime('%Y-%m-%d'))}")
        for e in v.get('entries', []):
            lines.append(f"{e['type']} {e['description']}")
        lines.append("")
    return '\n'.join(lines)

def parse_team(content: str) -> Dict:
    try:
        return json.loads(content)
    except Exception:
        return {"members": []}

def format_team(data: Dict) -> str:
    return json.dumps(data, indent=2)

def parse_blog(content: str) -> List[Dict]:
    posts = []
    blocks_raw = content.split("\n=====================================\n")
    for block in blocks_raw:
        if not block.strip():
            continue
        lines = block.strip().split('\n')
        post = {
            'id': '', 'title': '', 'subheading': '', 'date': '',
            'author': '', 'category': 'general', 'featureImage': '', 'content': []
        }
        in_content = False
        current_block_type = None
        current_block_lines = []

        def flush_block():
            nonlocal current_block_type, current_block_lines
            if current_block_type and (current_block_type == 'divider' or current_block_lines):
                post['content'].append({
                    'type': current_block_type,
                    'data': '\n'.join(current_block_lines) if current_block_type != 'divider' else ''
                })
            current_block_type = None
            current_block_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped == 'CONTENT_START':
                in_content = True
                continue
            elif stripped == 'CONTENT_END':
                flush_block()
                in_content = False
                continue
            if not in_content:
                for key, attr in [('ID:', 'id'), ('TITLE:', 'title'), ('SUBHEADING:', 'subheading'),
                                   ('DATE:', 'date'), ('AUTHOR:', 'author'), ('CATEGORY:', 'category'),
                                   ('IMAGE:', 'featureImage')]:
                    if stripped.startswith(key):
                        post[attr] = stripped[len(key):].strip()
                        break
            else:
                if stripped.startswith('BLOCK:'):
                    flush_block()
                    current_block_type = stripped[6:].strip()
                    if current_block_type == 'divider':
                        flush_block()
                elif current_block_type:
                    current_block_lines.append(line)
        flush_block()
        if post['id'] and post['title']:
            posts.append(post)
    return posts

def format_blog(posts: List[Dict]) -> str:
    lines = []
    for i, post in enumerate(posts):
        lines.append(f"ID: {post['id']}")
        lines.append(f"TITLE: {post['title']}")
        if post.get('subheading'):
            lines.append(f"SUBHEADING: {post['subheading']}")
        lines.append(f"DATE: {post.get('date', datetime.now().strftime('%Y-%m-%d'))}")
        lines.append(f"AUTHOR: {post.get('author', 'Unknown')}")
        lines.append(f"CATEGORY: {post.get('category', 'general')}")
        if post.get('featureImage'):
            lines.append(f"IMAGE: {post['featureImage']}")
        lines.append("CONTENT_START")
        for block in post.get('content', []):
            lines.append(f"BLOCK: {block['type']}")
            if block['type'] != 'divider':
                lines.append(block['data'])
        lines.append("CONTENT_END")
        if i < len(posts) - 1:
            lines.append("\n=====================================\n")
    return '\n'.join(lines)

async def load_webhook_ids() -> dict:
    content = await get_file_content(WEBHOOK_IDS_FILE)
    if content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return {}

async def save_webhook_ids(ids: dict, author: str = "bot") -> bool:
    return await commit_file(WEBHOOK_IDS_FILE, json.dumps(ids, indent=2),
                             "Update webhook message IDs", author)

def format_webhook_message(incident: Dict) -> str:
    inc_type = incident.get('type', 'INCIDENT')
    title    = incident.get('title', 'Unknown Incident')
    severity = incident.get('severity', 'medium')
    updates  = incident.get('updates', [])
    comps    = incident.get('components', '')

    emoji = INCIDENT_EMOJIS.get(inc_type, '🚨')
    sev_e = SEV_EMOJI.get(severity, '⚪')

    lines = [
        f"-# {ROLE_PING}",
        f"# {emoji} {title}",
        f"-# {sev_e} Severity: **{severity.upper()}** • {inc_type} • Components: {comps}",
        "",
    ]

    if updates:
        for u in updates:
            ts   = u.get('timestamp', '')
            stat = u.get('status', '').upper()
            desc = u.get('description', '')

            if ts and not ts.startswith('<t:'):
                for fmt in ('%b %d, %H:%M', '%b  %d, %H:%M'):
                    try:
                        dt = datetime.strptime(ts, fmt).replace(year=datetime.now().year)
                        ts = f"<t:{int(dt.timestamp())}:f>"
                        break
                    except ValueError:
                        pass

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

async def post_or_update_webhook(incident: Dict, existing_msg_id: Optional[str] = None) -> Optional[str]:
    content = format_webhook_message(incident)
    payload = {"content": content}
    async with aiohttp.ClientSession() as session:
        if existing_msg_id:
            url = f"{STATUS_WEBHOOK}/messages/{existing_msg_id}"
            async with session.patch(url, json=payload) as resp:
                if resp.status == 200:
                    return existing_msg_id
                text = await resp.text()
                print(f"Webhook PATCH failed ({resp.status}): {text}")
                return None
        else:
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

async def verify_webhook_message(msg_id: str) -> bool:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{STATUS_WEBHOOK}/messages/{msg_id}") as resp:
            return resp.status == 200

def _incident_key(inc: Dict) -> str:
    return f"{inc['date']}|{inc.get('title','')}"

def _is_resolved(inc: Dict) -> bool:
    updates = inc.get('updates', [])
    if not updates:
        return False
    last_status = updates[-1].get('status', '').upper()
    return last_status in ('RESOLVED', 'COMPLETED')

async def sync_webhooks(incidents: List[Dict], author: str = "bot"):
    ids = await load_webhook_ids()
    new_ids: dict = {}

    ordered = list(reversed(incidents))

    for inc in ordered:
        if inc.get('no_incidents') or not inc.get('title'):
            continue
        key = _incident_key(inc)
        existing_id = ids.get(key)

        if existing_id:
            alive = await verify_webhook_message(existing_id)
            if alive:
                result_id = await post_or_update_webhook(inc, existing_id)
                new_ids[key] = result_id or existing_id
            else:
                if not _is_resolved(inc):
                    result_id = await post_or_update_webhook(inc, None)
                    if result_id:
                        new_ids[key] = result_id
        else:
            if not _is_resolved(inc):
                result_id = await post_or_update_webhook(inc, None)
                if result_id:
                    new_ids[key] = result_id

    for key, msg_id in ids.items():
        if key not in new_ids:
            await delete_webhook_message(msg_id)

    await save_webhook_ids(new_ids, author)

@tasks.loop(seconds=POLL_INTERVAL)
async def poll_status():
    try:
        raw = await get_file_content(STATUS_FILE)
        if not raw:
            return
        new_hash = hashlib.sha256(raw.encode()).hexdigest()
        if new_hash == bot._status_hash:
            return
        print(f"[poll_status] status.txt changed — syncing webhooks")
        bot._status_hash = new_hash
        incidents = parse_status(raw)
        await sync_webhooks(incidents, "auto-poll")
    except Exception as e:
        print(f"[poll_status] error: {e}")

@poll_status.before_loop
async def before_poll():
    await bot.wait_until_ready()

class ContentType(Enum):
    STATUS    = "status"
    CHANGELOG = "changelog"
    TEAM      = "team"
    BLOG      = "blog"

@dataclass
class Session:
    content_type: ContentType
    raw: str
    file_path: str
    author: discord.Member
    interaction: discord.Interaction
    selected_index: Optional[int] = None

sessions: Dict[int, Session] = {}

def build_status_embed(session: Session) -> discord.Embed:
    incidents = parse_status(session.raw)
    embed = discord.Embed(title="🛠️ Status Editor", color=0x5865F2)
    embed.set_footer(text=f"Editor: {session.author.display_name} • Session expires in 10 min")
    if not incidents:
        embed.description = "*No incidents found.*"
        return embed
    for i, inc in enumerate(incidents):
        if inc.get('no_incidents'):
            embed.add_field(name=f"📅 {inc['date']}", value="No incidents reported.", inline=False)
            continue
        selected = session.selected_index == i
        marker = "▶ " if selected else ""
        emoji  = INCIDENT_EMOJIS.get(inc.get('type', 'INCIDENT'), '🚨')
        sev    = inc.get('severity', '?')
        sev_e  = SEV_EMOJI.get(sev, '⚪')
        updates = inc.get('updates', [])
        last_status = updates[-1]['status'] if updates else "No updates"
        last_emoji  = STATUS_EMOJIS.get(last_status.upper(), '⚪') if last_status else '⚪'
        val = (
            f"{sev_e} Severity: **{sev}**\n"
            f"🔧 Components: `{inc.get('components','—')}`\n"
            f"📝 Updates: {len(updates)} update(s)\n"
            f"{last_emoji} Latest: **{last_status or 'free-text'}**"
        )
        embed.add_field(
            name=f"{marker}{emoji} [{i+1}] {inc.get('title','Untitled')} — {inc['date']}",
            value=val,
            inline=False
        )
    return embed

def build_changelog_embed(session: Session) -> discord.Embed:
    versions = parse_changelog(session.raw)
    embed = discord.Embed(title="📋 Changelog Editor", color=0x57F287)
    embed.set_footer(text=f"Editor: {session.author.display_name} • Session expires in 10 min")
    if not versions:
        embed.description = "*No versions found.*"
        return embed
    for i, v in enumerate(versions):
        selected = session.selected_index == i
        marker = "▶ " if selected else ""
        entries = v.get('entries', [])
        entry_lines = '\n'.join(f"`{e['type']}` {e['description']}" for e in entries[:5])
        if len(entries) > 5:
            entry_lines += f"\n*…and {len(entries)-5} more*"
        embed.add_field(
            name=f"{marker}[{i+1}] v{v['version']} — {v.get('date','?')}",
            value=entry_lines or "*No entries*",
            inline=False
        )
    return embed

def build_team_embed(session: Session) -> discord.Embed:
    team = parse_team(session.raw)
    members = team.get('members', [])
    embed = discord.Embed(title="👥 Team Editor", color=0xEB459E)
    embed.set_footer(text=f"Editor: {session.author.display_name} • Session expires in 10 min")
    if not members:
        embed.description = "*No members found.*"
        return embed
    for i, m in enumerate(members):
        selected = session.selected_index == i
        marker = "▶ " if selected else ""
        roles = ', '.join(m.get('roles', []))
        embed.add_field(
            name=f"{marker}[{i+1}] {m.get('name','?')} (@{m.get('handle','?')})",
            value=f"ID: `{m.get('id','?')}` | Roles: {roles or '—'}\n{m.get('about','')[:80]}",
            inline=False
        )
    return embed

def build_blog_embed(session: Session) -> discord.Embed:
    posts = parse_blog(session.raw)
    embed = discord.Embed(title="📝 Blog Editor", color=0xF1C40F)
    embed.set_footer(text=f"Editor: {session.author.display_name} • Session expires in 10 min")
    if not posts:
        embed.description = "*No blog posts found.*"
        return embed
    for i, p in enumerate(posts):
        selected = session.selected_index == i
        marker = "▶ " if selected else ""
        preview = p.get('subheading', '')[:80]
        embed.add_field(
            name=f"{marker}[{i+1}] {p['title']} — {p.get('date','?')}",
            value=(f"📁 {p.get('category','general').upper()}\n✍️ {p.get('author','Unknown')}\n{preview}…"
                   if preview else f"📁 {p.get('category','general').upper()}\n✍️ {p.get('author','Unknown')}"),
            inline=False
        )
    return embed

def build_embed(session: Session) -> discord.Embed:
    if session.content_type == ContentType.STATUS:
        return build_status_embed(session)
    elif session.content_type == ContentType.CHANGELOG:
        return build_changelog_embed(session)
    elif session.content_type == ContentType.TEAM:
        return build_team_embed(session)
    elif session.content_type == ContentType.BLOG:
        return build_blog_embed(session)

class IncidentTypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Incident or Maintenance?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="🚨 Incident", value="INCIDENT", description="Something is broken"),
                discord.SelectOption(label="🔧 Maintenance", value="MAINTENANCE", description="Planned maintenance"),
            ],
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(interaction.user.id)
        if not session:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        await interaction.response.send_modal(IncidentModal(session, incident_type=self.values[0]))


class SeveritySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select severity",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="🟢 Low", value="low"),
                discord.SelectOption(label="🟡 Medium", value="medium"),
                discord.SelectOption(label="🟠 High", value="high"),
                discord.SelectOption(label="🔴 Critical", value="critical"),
                discord.SelectOption(label="🔧 Maintenance", value="maintenance"),
            ],
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        pass


class StatusActionView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

        self.add_item(IncidentTypeSelect())

        has_incidents = any(not inc.get('no_incidents') and inc.get('title') for inc in parse_status(session.raw))

        add_update_btn = discord.ui.Button(label="📝 Add Update", style=discord.ButtonStyle.primary, row=2, disabled=not has_incidents)
        add_update_btn.callback = self.add_update_callback
        self.add_item(add_update_btn)

        edit_btn = discord.ui.Button(label="✏️ Edit Incident", style=discord.ButtonStyle.primary, row=2, disabled=not has_incidents)
        edit_btn.callback = self.edit_incident_callback
        self.add_item(edit_btn)

        delete_btn = discord.ui.Button(label="🗑️ Delete Incident", style=discord.ButtonStyle.danger, row=2, disabled=not has_incidents)
        delete_btn.callback = self.delete_incident_callback
        self.add_item(delete_btn)

        save_btn = discord.ui.Button(label="💾 Save & Post", style=discord.ButtonStyle.success, row=3)
        save_btn.callback = self.save_callback
        self.add_item(save_btn)

        cancel_btn = discord.ui.Button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=3)
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    async def add_update_callback(self, interaction: discord.Interaction):
        incidents = [inc for inc in parse_status(self.session.raw) if not inc.get('no_incidents') and inc.get('title')]
        if not incidents:
            await interaction.response.send_message("No incidents to update.", ephemeral=True)
            return

        if len(incidents) == 1:
            self.session.selected_index = 0
            await interaction.response.send_modal(UpdateModal(self.session, incidents[0]))
            return

        view = discord.ui.View(timeout=60)
        select = IncidentSelect(incidents, "add_update")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def edit_incident_callback(self, interaction: discord.Interaction):
        incidents = [inc for inc in parse_status(self.session.raw) if not inc.get('no_incidents') and inc.get('title')]
        if not incidents:
            await interaction.response.send_message("No incidents to edit.", ephemeral=True)
            return

        if len(incidents) == 1:
            self.session.selected_index = 0
            await interaction.response.send_modal(IncidentModal(self.session, existing=incidents[0]))
            return

        view = discord.ui.View(timeout=60)
        select = IncidentSelect(incidents, "edit")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def delete_incident_callback(self, interaction: discord.Interaction):
        incidents = [inc for inc in parse_status(self.session.raw) if not inc.get('no_incidents') and inc.get('title')]
        if not incidents:
            await interaction.response.send_message("No incidents to delete.", ephemeral=True)
            return

        if len(incidents) == 1:
            self.session.selected_index = 0
            await self._confirm_delete(interaction, incidents[0])
            return

        view = discord.ui.View(timeout=60)
        select = IncidentSelect(incidents, "delete")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def _confirm_delete(self, interaction: discord.Interaction, incident: Dict):
        view = discord.ui.View(timeout=30)
        confirm = discord.ui.Button(label="✅ Yes, delete it", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

        async def confirm_cb(inter: discord.Interaction):
            incidents = parse_status(self.session.raw)
            for i, inc in enumerate(incidents):
                if inc.get('date') == incident['date'] and inc.get('title') == incident.get('title'):
                    incidents.pop(i)
                    break
            self.session.raw = format_status(incidents)
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=StatusActionView(self.session))

        async def cancel_cb(inter: discord.Interaction):
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=StatusActionView(self.session))

        confirm.callback = confirm_cb
        cancel_btn.callback = cancel_cb
        view.add_item(confirm)
        view.add_item(cancel_btn)

        embed = discord.Embed(title="⚠️ Confirm Delete", description=f"Delete incident: **{incident['title']}**?", color=0xFF0000)
        await interaction.response.edit_message(embed=embed, view=view)

    async def save_callback(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        active = [inc for inc in incidents if not inc.get('no_incidents') and inc.get('title')]

        for inc in active:
            if not inc.get('updates'):
                await interaction.response.send_message(
                    f"❌ Incident \"{inc['title']}\" has no updates. Add at least one update before saving.",
                    ephemeral=True
                )
                return

        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw,
                               "Update status", str(self.session.author))
        if ok:
            await sync_webhooks(incidents, str(self.session.author))
            bot._status_hash = hashlib.sha256(self.session.raw.encode()).hexdigest()
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub & webhook synced!")
            await interaction.edit_original_response(embed=embed, view=StatusActionView(self.session))
        else:
            await interaction.followup.send("❌ Failed to save!", ephemeral=True)

    async def cancel_callback(self, interaction: discord.Interaction):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)


class IncidentSelect(discord.ui.Select):
    def __init__(self, incidents: List[Dict], action: str):
        self.incidents = incidents
        self.action = action
        options = []
        for i, inc in enumerate(incidents):
            emoji = INCIDENT_EMOJIS.get(inc.get('type', 'INCIDENT'), '🚨')
            label = inc.get('title', 'Untitled')[:80]
            options.append(discord.SelectOption(label=f"{label}", value=str(i), emoji=emoji, description=inc.get('date', '')))
        super().__init__(placeholder=f"Select incident to {action}…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(interaction.user.id)
        if not session:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        idx = int(self.values[0])
        session.selected_index = idx
        incident = self.incidents[idx]

        if self.action == "add_update":
            await interaction.response.send_modal(UpdateModal(session, incident))
        elif self.action == "edit":
            await interaction.response.send_modal(IncidentModal(session, existing=incident))
        elif self.action == "delete":
            await self._confirm_delete(interaction, session, incident)

    async def _confirm_delete(self, interaction: discord.Interaction, session: Session, incident: Dict):
        view = discord.ui.View(timeout=30)
        confirm = discord.ui.Button(label="✅ Yes, delete it", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

        async def confirm_cb(inter: discord.Interaction):
            incidents = parse_status(session.raw)
            for i, inc in enumerate(incidents):
                if inc.get('date') == incident['date'] and inc.get('title') == incident.get('title'):
                    incidents.pop(i)
                    break
            session.raw = format_status(incidents)
            session.selected_index = None
            await inter.response.edit_message(embed=build_embed(session), view=StatusActionView(session))

        async def cancel_cb(inter: discord.Interaction):
            session.selected_index = None
            await inter.response.edit_message(embed=build_embed(session), view=StatusActionView(session))

        confirm.callback = confirm_cb
        cancel_btn.callback = cancel_cb
        view.add_item(confirm)
        view.add_item(cancel_btn)

        embed = discord.Embed(title="⚠️ Confirm Delete", description=f"Delete incident: **{incident['title']}**?", color=0xFF0000)
        await interaction.response.edit_message(embed=embed, view=view)


class IncidentModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None, incident_type: str = "INCIDENT"):
        super().__init__(title="Edit Incident" if existing else "New Incident")
        self.session  = session
        self.existing = existing

        self.f_title = discord.ui.TextInput(
            label="Title",
            placeholder="Brief description of the issue",
            default=existing.get('title', '') if existing else '',
            max_length=100
        )
        self.add_item(self.f_title)

        self.incident_type = incident_type if not existing else existing.get('type', 'INCIDENT')

        if not existing:
            self.f_severity = discord.ui.TextInput(
                label="Severity",
                placeholder="low / medium / high / critical / maintenance",
                default='medium',
                max_length=20
            )
            self.add_item(self.f_severity)

            self.f_components = discord.ui.TextInput(
                label="Components (comma-separated)",
                placeholder="Seshy RuntimeEngine, Seshy AI",
                default='',
                max_length=200,
                required=False
            )
            self.add_item(self.f_components)

    async def on_submit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        if self.existing:
            for i, inc in enumerate(incidents):
                if inc.get('date') == self.existing['date'] and inc.get('title') == self.existing.get('title'):
                    incidents[i]['title'] = self.f_title.value.strip()
                    incidents[i]['type'] = self.incident_type
                    break
        else:
            new_inc = {
                'date':       datetime.now().strftime('%b %d, %Y'),
                'type':       self.incident_type,
                'title':      self.f_title.value.strip(),
                'severity':   self.f_severity.value.strip().lower() if hasattr(self, 'f_severity') else 'medium',
                'components': self.f_components.value.strip() if hasattr(self, 'f_components') else '',
                'updates':    [],
                'no_incidents': False,
            }
            incidents.insert(0, new_inc)
        self.session.raw = format_status(incidents)
        self.session.selected_index = None
        await interaction.response.edit_message(
            embed=build_embed(self.session), view=StatusActionView(self.session))


class UpdateModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title=f"Update: {incident.get('title','')[:40]}")
        self.session  = session
        self.incident = incident

        self.f_status = discord.ui.TextInput(
            label="Status",
            placeholder="INVESTIGATING / MONITORING / IDENTIFIED / RESOLVED / COMPLETED",
            required=True,
            max_length=30
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            placeholder="What is the current situation?",
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        self.add_item(self.f_status)
        self.add_item(self.f_desc)

    async def on_submit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        ts = datetime.now().strftime('%b %d, %H:%M')
        for inc in incidents:
            if inc.get('date') == self.incident['date'] and inc.get('title') == self.incident.get('title'):
                inc['updates'].append({
                    'timestamp':   ts,
                    'status':      self.f_status.value.strip().upper(),
                    'description': self.f_desc.value.strip(),
                })
                break
        self.session.raw = format_status(incidents)
        self.session.selected_index = None
        await interaction.response.edit_message(
            embed=build_embed(self.session), view=StatusActionView(self.session))


class ChangelogView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ New Version", style=discord.ButtonStyle.success, row=0)
    async def add_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VersionModal(self.session))

    @discord.ui.button(label="✏️ Edit Version", style=discord.ButtonStyle.primary, row=0)
    async def edit_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        versions = parse_changelog(self.session.raw)
        if not versions:
            await interaction.response.send_message("No versions to edit.", ephemeral=True)
            return
        if len(versions) == 1:
            self.session.selected_index = 0
            await interaction.response.send_modal(VersionModal(self.session, existing=versions[0]))
            return
        view = discord.ui.View(timeout=60)
        select = VersionSelect(versions, "edit")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    @discord.ui.button(label="🗑️ Delete Version", style=discord.ButtonStyle.danger, row=0)
    async def delete_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        versions = parse_changelog(self.session.raw)
        if not versions:
            await interaction.response.send_message("No versions to delete.", ephemeral=True)
            return
        if len(versions) == 1:
            self.session.selected_index = 0
            await self._confirm_delete_version(interaction, versions[0])
            return
        view = discord.ui.View(timeout=60)
        select = VersionSelect(versions, "delete")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def _confirm_delete_version(self, interaction: discord.Interaction, version: Dict):
        view = discord.ui.View(timeout=30)
        confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

        async def confirm_cb(inter: discord.Interaction):
            versions = parse_changelog(self.session.raw)
            for i, v in enumerate(versions):
                if v['version'] == version['version']:
                    versions.pop(i)
                    break
            self.session.raw = format_changelog(versions)
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=ChangelogView(self.session))

        async def cancel_cb(inter: discord.Interaction):
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=ChangelogView(self.session))

        confirm.callback = confirm_cb
        cancel_btn.callback = cancel_cb
        view.add_item(confirm)
        view.add_item(cancel_btn)
        await interaction.response.edit_message(
            embed=discord.Embed(title="⚠️ Confirm Delete", description=f"Delete v{version['version']}?", color=0xFF0000),
            view=view
        )

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update changelog", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=ChangelogView(self.session))
        else:
            await interaction.followup.send("❌ Failed to save!", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)


class VersionSelect(discord.ui.Select):
    def __init__(self, versions: List[Dict], action: str):
        self.versions = versions
        self.action = action
        options = []
        for i, v in enumerate(versions):
            options.append(discord.SelectOption(
                label=f"v{v['version']}",
                description=v.get('date', ''),
                value=str(i),
                emoji='📋'
            ))
        super().__init__(placeholder=f"Select version to {action}…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(interaction.user.id)
        if not session:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        idx = int(self.values[0])
        session.selected_index = idx
        version = self.versions[idx]

        if self.action == "edit":
            await interaction.response.send_modal(VersionModal(session, existing=version))
        elif self.action == "delete":
            view = discord.ui.View(timeout=30)
            confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
            cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

            async def confirm_cb(inter: discord.Interaction):
                versions = parse_changelog(session.raw)
                for i, v in enumerate(versions):
                    if v['version'] == version['version']:
                        versions.pop(i)
                        break
                session.raw = format_changelog(versions)
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=ChangelogView(session))

            async def cancel_cb(inter: discord.Interaction):
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=ChangelogView(session))

            confirm.callback = confirm_cb
            cancel_btn.callback = cancel_cb
            view.add_item(confirm)
            view.add_item(cancel_btn)
            await interaction.response.edit_message(
                embed=discord.Embed(title="⚠️ Confirm Delete", description=f"Delete v{version['version']}?", color=0xFF0000),
                view=view
            )


class VersionModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Version" if existing else "New Version")
        self.session  = session
        self.existing = existing

        self.f_version = discord.ui.TextInput(
            label="Version",
            placeholder="1.2.3",
            default=existing['version'] if existing else '',
            max_length=30
        )
        self.f_date = discord.ui.TextInput(
            label="Date",
            placeholder="YYYY-MM-DD",
            default=existing.get('date', datetime.now().strftime("%Y-%m-%d")) if existing else datetime.now().strftime("%Y-%m-%d"),
            max_length=20
        )
        self.f_entries = discord.ui.TextInput(
            label="Entries (TYPE Description, one per line)",
            placeholder="FEATURE Added dark mode\nFIX Fixed login bug",
            default='\n'.join(f"{e['type']} {e['description']}" for e in existing.get('entries', [])) if existing else '',
            style=discord.TextStyle.paragraph,
            max_length=1500
        )
        self.add_item(self.f_version)
        self.add_item(self.f_date)
        self.add_item(self.f_entries)

    async def on_submit(self, interaction: discord.Interaction):
        versions = parse_changelog(self.session.raw)
        entries = []
        for line in self.f_entries.value.split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) == 2:
                entries.append({'type': parts[0].upper(), 'description': parts[1]})
        new_ver = {
            'version': self.f_version.value.strip(),
            'date':    self.f_date.value.strip(),
            'entries': entries,
        }
        if self.existing:
            for i, v in enumerate(versions):
                if v['version'] == self.existing['version']:
                    versions[i] = new_ver
                    break
        else:
            versions.insert(0, new_ver)
        self.session.raw = format_changelog(versions)
        self.session.selected_index = None
        await interaction.response.edit_message(
            embed=build_embed(self.session), view=ChangelogView(self.session))


class TeamView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ Add Member", style=discord.ButtonStyle.success, row=0)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MemberModal(self.session))

    @discord.ui.button(label="✏️ Edit Member", style=discord.ButtonStyle.primary, row=0)
    async def edit_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        members = parse_team(self.session.raw).get('members', [])
        if not members:
            await interaction.response.send_message("No members to edit.", ephemeral=True)
            return
        if len(members) == 1:
            self.session.selected_index = 0
            await interaction.response.send_modal(MemberModal(self.session, existing=members[0]))
            return
        view = discord.ui.View(timeout=60)
        select = MemberSelect(members, "edit")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    @discord.ui.button(label="🗑️ Remove Member", style=discord.ButtonStyle.danger, row=0)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        members = parse_team(self.session.raw).get('members', [])
        if not members:
            await interaction.response.send_message("No members to remove.", ephemeral=True)
            return
        if len(members) == 1:
            self.session.selected_index = 0
            await self._confirm_remove_member(interaction, members[0])
            return
        view = discord.ui.View(timeout=60)
        select = MemberSelect(members, "delete")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def _confirm_remove_member(self, interaction: discord.Interaction, member: Dict):
        view = discord.ui.View(timeout=30)
        confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

        async def confirm_cb(inter: discord.Interaction):
            team = parse_team(self.session.raw)
            team['members'] = [m for m in team['members'] if m['id'] != member['id']]
            self.session.raw = format_team(team)
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=TeamView(self.session))

        async def cancel_cb(inter: discord.Interaction):
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=TeamView(self.session))

        confirm.callback = confirm_cb
        cancel_btn.callback = cancel_cb
        view.add_item(confirm)
        view.add_item(cancel_btn)
        await interaction.response.edit_message(
            embed=discord.Embed(title="⚠️ Confirm Remove", description=f"Remove {member.get('name','?')}?", color=0xFF0000),
            view=view
        )

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update team", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=TeamView(self.session))
        else:
            await interaction.followup.send("❌ Failed to save!", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)


class MemberSelect(discord.ui.Select):
    def __init__(self, members: List[Dict], action: str):
        self.members = members
        self.action = action
        options = []
        for i, m in enumerate(members):
            options.append(discord.SelectOption(
                label=m.get('name', '?')[:80],
                description=f"@{m.get('handle','?')}",
                value=str(i),
                emoji='👤'
            ))
        super().__init__(placeholder=f"Select member to {action}…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(interaction.user.id)
        if not session:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        idx = int(self.values[0])
        session.selected_index = idx
        member = self.members[idx]

        if self.action == "edit":
            await interaction.response.send_modal(MemberModal(session, existing=member))
        elif self.action == "delete":
            view = discord.ui.View(timeout=30)
            confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
            cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

            async def confirm_cb(inter: discord.Interaction):
                team = parse_team(session.raw)
                team['members'] = [m for m in team['members'] if m['id'] != member['id']]
                session.raw = format_team(team)
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=TeamView(session))

            async def cancel_cb(inter: discord.Interaction):
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=TeamView(session))

            confirm.callback = confirm_cb
            cancel_btn.callback = cancel_cb
            view.add_item(confirm)
            view.add_item(cancel_btn)
            await interaction.response.edit_message(
                embed=discord.Embed(title="⚠️ Confirm Remove", description=f"Remove {member.get('name','?')}?", color=0xFF0000),
                view=view
            )


class MemberModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Member" if existing else "New Member")
        self.session  = session
        self.existing = existing

        self.f_id = discord.ui.TextInput(
            label="Member ID (slug)",
            placeholder="johndoe",
            default=existing['id'] if existing else '',
            max_length=30
        )
        self.f_name = discord.ui.TextInput(
            label="Display Name",
            placeholder="John Doe",
            default=existing.get('name', '') if existing else '',
            max_length=50
        )
        self.f_handle = discord.ui.TextInput(
            label="Discord Handle",
            placeholder="johndoe",
            default=existing.get('handle', '') if existing else '',
            max_length=50
        )
        self.f_roles = discord.ui.TextInput(
            label="Roles (comma-separated)",
            placeholder="Developer, Support",
            default=', '.join(existing.get('roles', [])) if existing else '',
            max_length=200
        )
        self.f_about = discord.ui.TextInput(
            label="About",
            placeholder="Short bio (optional)",
            default=existing.get('about', '') if existing else '',
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        self.add_item(self.f_id)
        self.add_item(self.f_name)
        self.add_item(self.f_handle)
        self.add_item(self.f_roles)
        self.add_item(self.f_about)

    async def on_submit(self, interaction: discord.Interaction):
        team = parse_team(self.session.raw)
        new_member = {
            'id':         self.f_id.value.strip(),
            'name':       self.f_name.value.strip(),
            'handle':     self.f_handle.value.strip(),
            'roles':      [r.strip() for r in self.f_roles.value.split(',') if r.strip()],
            'about':      self.f_about.value.strip(),
            'status':     'Online',
            'joinedYear': str(datetime.now().year),
            'avatarUrl': '', 'timeline': [], 'skills': [],
            'tags': [], 'stats': [], 'dataFields': []
        }
        if self.existing:
            merged = {**self.existing, **new_member}
            for i, m in enumerate(team['members']):
                if m['id'] == self.existing['id']:
                    team['members'][i] = merged
                    break
        else:
            team['members'].append(new_member)
        self.session.raw = format_team(team)
        self.session.selected_index = None
        await interaction.response.edit_message(
            embed=build_embed(self.session), view=TeamView(self.session))


class BlogView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ New Post", style=discord.ButtonStyle.success, row=0)
    async def add_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BlogPostModal(self.session))

    @discord.ui.button(label="✏️ Edit Post", style=discord.ButtonStyle.primary, row=0)
    async def edit_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        posts = parse_blog(self.session.raw)
        if not posts:
            await interaction.response.send_message("No posts to edit.", ephemeral=True)
            return
        if len(posts) == 1:
            self.session.selected_index = 0
            await interaction.response.send_modal(BlogPostModal(self.session, existing=posts[0]))
            return
        view = discord.ui.View(timeout=60)
        select = BlogPostSelect(posts, "edit")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    @discord.ui.button(label="🗑️ Delete Post", style=discord.ButtonStyle.danger, row=0)
    async def delete_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        posts = parse_blog(self.session.raw)
        if not posts:
            await interaction.response.send_message("No posts to delete.", ephemeral=True)
            return
        if len(posts) == 1:
            self.session.selected_index = 0
            await self._confirm_delete_post(interaction, posts[0])
            return
        view = discord.ui.View(timeout=60)
        select = BlogPostSelect(posts, "delete")
        view.add_item(select)
        await interaction.response.edit_message(embed=build_embed(self.session), view=view)

    async def _confirm_delete_post(self, interaction: discord.Interaction, post: Dict):
        view = discord.ui.View(timeout=30)
        confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

        async def confirm_cb(inter: discord.Interaction):
            posts = parse_blog(self.session.raw)
            posts = [p for p in posts if p['id'] != post['id']]
            self.session.raw = format_blog(posts)
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=BlogView(self.session))

        async def cancel_cb(inter: discord.Interaction):
            self.session.selected_index = None
            await inter.response.edit_message(embed=build_embed(self.session), view=BlogView(self.session))

        confirm.callback = confirm_cb
        cancel_btn.callback = cancel_cb
        view.add_item(confirm)
        view.add_item(cancel_btn)
        await interaction.response.edit_message(
            embed=discord.Embed(title="⚠️ Confirm Delete", description=f"Delete post '{post['title']}'?", color=0xFF0000),
            view=view
        )

    @discord.ui.button(label="📝 Direct Entry", style=discord.ButtonStyle.secondary, row=1)
    async def direct_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RawPasteModal(self.session))

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=1)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update blog", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=BlogView(self.session))
        else:
            await interaction.followup.send("❌ Failed to save!", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)


class BlogPostSelect(discord.ui.Select):
    def __init__(self, posts: List[Dict], action: str):
        self.posts = posts
        self.action = action
        options = []
        for i, p in enumerate(posts):
            options.append(discord.SelectOption(
                label=p['title'][:80],
                description=p.get('date', ''),
                value=str(i),
                emoji='📝'
            ))
        super().__init__(placeholder=f"Select post to {action}…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(interaction.user.id)
        if not session:
            await interaction.response.send_message("Session expired.", ephemeral=True)
            return
        idx = int(self.values[0])
        session.selected_index = idx
        post = self.posts[idx]

        if self.action == "edit":
            await interaction.response.send_modal(BlogPostModal(session, existing=post))
        elif self.action == "delete":
            view = discord.ui.View(timeout=30)
            confirm = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.danger)
            cancel_btn = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.secondary)

            async def confirm_cb(inter: discord.Interaction):
                posts = parse_blog(session.raw)
                posts = [p for p in posts if p['id'] != post['id']]
                session.raw = format_blog(posts)
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=BlogView(session))

            async def cancel_cb(inter: discord.Interaction):
                session.selected_index = None
                await inter.response.edit_message(embed=build_embed(session), view=BlogView(session))

            confirm.callback = confirm_cb
            cancel_btn.callback = cancel_cb
            view.add_item(confirm)
            view.add_item(cancel_btn)
            await interaction.response.edit_message(
                embed=discord.Embed(title="⚠️ Confirm Delete", description=f"Delete post '{post['title']}'?", color=0xFF0000),
                view=view
            )


class BlogPostModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Blog Post" if existing else "New Blog Post")
        self.session  = session
        self.existing = existing

        self.f_id = discord.ui.TextInput(
            label="Post ID (slug)",
            placeholder="my-awesome-post",
            default=existing['id'] if existing else '',
            max_length=100
        )
        self.f_title = discord.ui.TextInput(
            label="Title",
            placeholder="My Awesome Blog Post",
            default=existing.get('title', '') if existing else '',
            max_length=200
        )
        self.f_subheading = discord.ui.TextInput(
            label="Subheading",
            placeholder="A brief description",
            default=existing.get('subheading', '') if existing else '',
            required=False,
            max_length=300
        )
        self.f_meta = discord.ui.TextInput(
            label="Date | Author | Category",
            placeholder="2026-05-01 | Johan | announcements",
            default=(f"{existing.get('date','')} | {existing.get('author','')} | {existing.get('category','general')}"
                     if existing else datetime.now().strftime("%Y-%m-%d") + " | Unknown | general"),
            max_length=100
        )
        self.f_content = discord.ui.TextInput(
            label="Content (BLOCK:type per line, then data)",
            placeholder="BLOCK:paragraph\nWelcome!\nBLOCK:heading\nSection Title",
            default='\n'.join(f"BLOCK:{b['type']}\n{b['data']}" for b in existing.get('content', [])) if existing else '',
            style=discord.TextStyle.paragraph,
            max_length=4000
        )
        self.add_item(self.f_id)
        self.add_item(self.f_title)
        self.add_item(self.f_subheading)
        self.add_item(self.f_meta)
        self.add_item(self.f_content)

    async def on_submit(self, interaction: discord.Interaction):
        posts = parse_blog(self.session.raw)
        meta_parts = [p.strip() for p in self.f_meta.value.split('|')]
        date   = meta_parts[0] if len(meta_parts) > 0 else datetime.now().strftime("%Y-%m-%d")
        author = meta_parts[1] if len(meta_parts) > 1 else "Unknown"
        cat    = meta_parts[2].lower() if len(meta_parts) > 2 else "general"

        content_blocks = []
        lines = self.f_content.value.split('\n')
        current_type  = None
        current_lines = []

        def flush():
            nonlocal current_type, current_lines
            if current_type and (current_type == 'divider' or current_lines):
                content_blocks.append({
                    'type': current_type,
                    'data': '\n'.join(current_lines) if current_type != 'divider' else ''
                })
            current_type  = None
            current_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('BLOCK:'):
                flush()
                current_type  = stripped[6:].strip()
                current_lines = []
                if current_type == 'divider':
                    flush()
            elif current_type:
                current_lines.append(line)
        flush()

        new_post = {
            'id':          self.f_id.value.strip().lower().replace(' ', '-'),
            'title':       self.f_title.value.strip(),
            'subheading':  self.f_subheading.value.strip(),
            'date':        date,
            'author':      author,
            'category':    cat,
            'featureImage': self.existing.get('featureImage', '') if self.existing else '',
            'content':     content_blocks,
        }
        if self.existing:
            for i, p in enumerate(posts):
                if p['id'] == self.existing['id']:
                    posts[i] = new_post
                    break
        else:
            posts.insert(0, new_post)
        self.session.raw = format_blog(posts)
        self.session.selected_index = None
        await interaction.response.edit_message(
            embed=build_embed(self.session), view=BlogView(self.session))


class RawPasteModal(discord.ui.Modal):
    def __init__(self, session: Session):
        super().__init__(title="Direct Blog Entry — Paste Raw Text")
        self.session = session
        self.f_raw = discord.ui.TextInput(
            label="Paste the complete blog entry here",
            placeholder="ID: my-post\nTITLE: My Post\n...",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True
        )
        self.add_item(self.f_raw)

    async def on_submit(self, interaction: discord.Interaction):
        raw_text = self.f_raw.value.strip()
        if not raw_text:
            await interaction.response.send_message("❌ Please paste the blog entry text.", ephemeral=True)
            return
        try:
            posts = parse_blog(raw_text)
            if not posts:
                await interaction.response.send_message("❌ Could not parse a valid blog post.", ephemeral=True)
                return
            new_post = posts[0]
            existing_posts = parse_blog(self.session.raw)
            existing_idx = next((i for i, p in enumerate(existing_posts) if p['id'] == new_post['id']), None)
            if existing_idx is not None:
                existing_posts[existing_idx] = new_post
            else:
                existing_posts.insert(0, new_post)
            self.session.raw = format_blog(existing_posts)
            self.session.selected_index = None
            embed = build_embed(self.session)
            embed.set_footer(text=f"✅ Post added successfully!")
            await interaction.response.edit_message(embed=embed, view=BlogView(self.session))
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


def build_view(session: Session) -> discord.ui.View:
    if session.content_type == ContentType.STATUS:
        return StatusActionView(session)
    elif session.content_type == ContentType.CHANGELOG:
        return ChangelogView(session)
    elif session.content_type == ContentType.TEAM:
        return TeamView(session)
    elif session.content_type == ContentType.BLOG:
        return BlogView(session)


def has_permission(member: discord.Member) -> bool:
    return any(role.id == ALLOWED_ROLE_ID for role in member.roles)

@bot.tree.command(name="edit", description="Edit website content (status, changelog, team, blog)")
@app_commands.describe(content_type="What do you want to edit?")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status",    value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team",      value="team"),
    app_commands.Choice(name="Blog",      value="blog"),
])
async def edit_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    if not has_permission(interaction.user):
        await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ct = ContentType(content_type.value)
    fp = {ContentType.STATUS: STATUS_FILE, ContentType.CHANGELOG: CHANGELOG_FILE,
          ContentType.TEAM: TEAM_FILE, ContentType.BLOG: BLOG_FILE}[ct]
    raw = await get_file_content(fp) or ""
    session = Session(content_type=ct, raw=raw, file_path=fp,
                      author=interaction.user, interaction=interaction)
    sessions[interaction.user.id] = session
    await interaction.followup.send(embed=build_embed(session), view=build_view(session), ephemeral=True)

@bot.tree.command(name="view", description="View current website content")
@app_commands.describe(content_type="What do you want to view?")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status",    value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team",      value="team"),
    app_commands.Choice(name="Blog",      value="blog"),
])
async def view_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    ct = ContentType(content_type.value)
    fp = {ContentType.STATUS: STATUS_FILE, ContentType.CHANGELOG: CHANGELOG_FILE,
          ContentType.TEAM: TEAM_FILE, ContentType.BLOG: BLOG_FILE}[ct]
    raw = await get_file_content(fp)
    if not raw:
        await interaction.followup.send("*No content found or GitHub not configured.*", ephemeral=True)
        return
    session = Session(content_type=ct, raw=raw, file_path=fp,
                      author=interaction.user, interaction=interaction)
    embed = build_embed(session)
    embed.title = embed.title.replace(" Editor", "")
    embed.color = 0x99AAB5
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="sync_webhooks", description="Force re-sync all status incidents to webhook channel")
async def cmd_sync_webhooks(interaction: discord.Interaction):
    if not has_permission(interaction.user):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    raw = await get_file_content(STATUS_FILE)
    if not raw:
        await interaction.followup.send("Could not fetch status file.", ephemeral=True)
        return
    incidents = parse_status(raw)
    await sync_webhooks(incidents, str(interaction.user))
    active = [i for i in incidents if not i.get('no_incidents') and i.get('title')]
    await interaction.followup.send(f"✅ Synced {len(active)} incident(s) to webhook.", ephemeral=True)

@bot.tree.command(name="help_editor", description="Show help for the content editor")
async def help_editor(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Content Editor Help", color=0x5865F2)
    embed.add_field(name="Commands",
                    value="`/edit <type>` — Open editor\n`/view <type>` — Read-only view\n`/sync_webhooks` — Force re-sync",
                    inline=False)
    embed.add_field(name="Status Editing",
                    value="1. Click dropdown to choose Incident or Maintenance\n2. Fill in title, severity, components\n3. Save creates the incident file\n4. Add an update (required) then Save & Post to push to webhook\n5. Each incident gets ONE message, updated with new info",
                    inline=False)
    embed.add_field(name="Webhook behaviour",
                    value="• Incident posts once when first update is added\n• All subsequent updates edit that same message\n• Deleting an incident removes its webhook message\n• Bot auto-syncs every 60 seconds",
                    inline=False)
    embed.set_footer(text="All editor sessions are ephemeral (only visible to you)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

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