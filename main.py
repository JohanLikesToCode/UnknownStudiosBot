import sys
import os
import traceback
import warnings
warnings.filterwarnings('ignore', category=SyntaxWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)

import logging
logging.basicConfig(level=logging.INFO)

import discord
from discord.ext import commands
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

# ─── Configuration ───────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.getenv('DISCORD_TOKEN')
GITHUB_TOKEN    = os.getenv('GITHUB_TOKEN')
GITHUB_REPO     = os.getenv('GITHUB_REPO')
GUILD_ID        = os.getenv('GUILD_ID')
STATUS_WEBHOOK  = "https://discord.com/api/webhooks/1499369589296070688/2BEPyenbkpWsy95aajJaP9XMmMDRWrHFhfALZkOIil7NFNoNPUtyw3mxksBbDn-mQunb"
ROLE_PING       = "<@&1499369803708633148>"

print(f"Discord token present: {bool(DISCORD_TOKEN)}")
print(f"GitHub token present: {bool(GITHUB_TOKEN)}")
print(f"GitHub repo: {GITHUB_REPO}")

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN not set!")
    sys.exit(1)

# ─── File paths ───────────────────────────────────────────────────────────────
STATUS_FILE    = "status.txt"
CHANGELOG_FILE = "changelog.txt"
TEAM_FILE      = "teams.json"
BLOG_FILE      = "blogs.txt"
WEBHOOK_IDS_FILE = "webhook_message_ids.json"  # track posted webhook messages

# ─── Components list (edit as needed) ────────────────────────────────────────
COMPONENT_OPTIONS = [
    "API", "Website", "Dashboard", "Database", "Authentication",
    "Payments", "Notifications", "CDN", "Search", "Media",
    "Webhooks", "Analytics", "Storage", "Email", "Chat"
]

SEVERITY_OPTIONS = ["low", "medium", "high", "critical", "maintenance"]

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

# ─── Discord Bot Setup ────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class DiscordBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

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

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print(f'Bot is in {len(self.guilds)} guilds')

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

# ─── GitHub Helpers ───────────────────────────────────────────────────────────
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

# ─── Webhook Message ID Tracking ─────────────────────────────────────────────
async def load_webhook_ids() -> dict:
    content = await get_file_content(WEBHOOK_IDS_FILE)
    if content:
        try:
            return json.loads(content)
        except Exception:
            pass
    return {}

async def save_webhook_ids(ids: dict, author: str = "bot") -> bool:
    return await commit_file(WEBHOOK_IDS_FILE, json.dumps(ids, indent=2), "Update webhook message IDs", author)

# ─── Status Parsing ───────────────────────────────────────────────────────────
def parse_status(content: str) -> List[Dict]:
    incidents = []
    current = None
    for line in content.strip().split('\n'):
        line = line.rstrip()
        if not line:
            if current:
                incidents.append(current)
                current = None
            continue
        m = re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$', line)
        if m:
            if current:
                incidents.append(current)
            current = {'date': line, 'updates': [], 'tasks': []}
            continue
        if current is None:
            continue
        m = re.match(r'^(INCIDENT|MAINTENANCE):\s+(.+)$', line)
        if m:
            current['type'] = m.group(1)
            current['title'] = m.group(2)
            continue
        m = re.match(r'^SEVERITY:\s+(.+)$', line)
        if m:
            current['severity'] = m.group(1)
            continue
        m = re.match(r'^COMPONENTS:\s+(.+)$', line)
        if m:
            current['components'] = m.group(1)
            continue
        m = re.match(r'^TASK:\s+(.+)$', line)
        if m:
            current.setdefault('tasks', []).append(m.group(1))
            continue
        m = re.match(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w[\w ]*?)\s+-\s+(.+)$', line)
        if m:
            current['updates'].append({
                'timestamp': m.group(1),
                'status': m.group(2).strip(),
                'description': m.group(3)
            })
            continue
        if line == "No incidents reported." and current:
            current['no_incidents'] = True
    if current:
        incidents.append(current)
    return incidents

def format_status(incidents: List[Dict]) -> str:
    lines = []
    for inc in incidents:
        lines.append(inc['date'])
        if inc.get('no_incidents'):
            lines.append("No incidents reported.")
            lines.append("")
            continue
        lines.append(f"{inc.get('type','INCIDENT')}: {inc.get('title','Untitled')}")
        lines.append(f"SEVERITY: {inc.get('severity','medium')}")
        lines.append(f"COMPONENTS: {inc.get('components','')}")
        for task in inc.get('tasks', []):
            lines.append(f"TASK: {task}")
        for u in inc.get('updates', []):
            lines.append(f"{u['timestamp']} - {u['status']} - {u['description']}")
        lines.append("")
    return '\n'.join(lines)

# ─── Changelog Parsing ────────────────────────────────────────────────────────
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

# ─── Team Parsing ─────────────────────────────────────────────────────────────
def parse_team(content: str) -> Dict:
    try:
        return json.loads(content)
    except Exception:
        return {"members": []}

def format_team(data: Dict) -> str:
    return json.dumps(data, indent=2)

# ─── Blog Parsing ───────────────────────────────────────────────────────────────
def parse_blog(content: str) -> List[Dict]:
    """Parse blog posts from text format into structured data."""
    posts = []
    blocks = content.split("\n=====================================\n")
    
    for block in blocks:
        if not block.strip():
            continue
        
        lines = block.strip().split('\n')
        post = {
            'id': '',
            'title': '',
            'subheading': '',
            'date': '',
            'author': '',
            'category': 'general',
            'featureImage': '',
            'content': []
        }
        
        i = 0
        in_content = False
        current_content = []
        
        while i < len(lines):
            line = lines[i].strip()
            
            if not in_content:
                if line.startswith('ID:'):
                    post['id'] = line.replace('ID:', '').strip()
                elif line.startswith('TITLE:'):
                    post['title'] = line.replace('TITLE:', '').strip()
                elif line.startswith('SUBHEADING:'):
                    post['subheading'] = line.replace('SUBHEADING:', '').strip()
                elif line.startswith('DATE:'):
                    post['date'] = line.replace('DATE:', '').strip()
                elif line.startswith('AUTHOR:'):
                    post['author'] = line.replace('AUTHOR:', '').strip()
                elif line.startswith('CATEGORY:'):
                    post['category'] = line.replace('CATEGORY:', '').strip().lower()
                elif line.startswith('IMAGE:'):
                    post['featureImage'] = line.replace('IMAGE:', '').strip()
                elif line == 'CONTENT_START':
                    in_content = True
            else:
                if line == 'CONTENT_END':
                    break
                elif line.startswith('BLOCK:'):
                    # Parse block
                    block_type = line.replace('BLOCK:', '').strip()
                    i += 1
                    if i < len(lines):
                        block_data = lines[i].strip()
                        current_content.append({
                            'type': block_type,
                            'data': block_data
                        })
                elif current_content and current_content[-1].get('data') and not line.startswith('BLOCK:'):
                    # Multi-line block content
                    current_content[-1]['data'] += '\n' + line
            
            i += 1
        
        post['content'] = current_content
        if post['id'] and post['title']:
            posts.append(post)
    
    return posts

def format_blog(posts: List[Dict]) -> str:
    """Convert blog posts back to text format."""
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
            lines.append(block['data'])
        
        lines.append("CONTENT_END")
        if i < len(posts) - 1:
            lines.append("\n=====================================\n")
    
    return '\n'.join(lines)

# ─── Webhook Formatting ───────────────────────────────────────────────────────
def format_webhook_message(incident: Dict) -> str:
    inc_type  = incident.get('type', 'INCIDENT')
    title     = incident.get('title', 'Unknown Incident')
    severity  = incident.get('severity', 'medium')
    tasks     = incident.get('tasks', [])
    updates   = incident.get('updates', [])

    emoji = INCIDENT_EMOJIS.get(inc_type, '🚨')
    lines = [f"-# {ROLE_PING}", f"# {emoji} {title}"]
    lines.append(f"-# Severity: **{severity.upper()}** • {inc_type}")
    lines.append("")

    for u in updates:
        ts     = u.get('timestamp', '')
        status = u.get('status', '').upper()
        desc   = u.get('description', '')
        status_emoji = STATUS_EMOJIS.get(status, '⚪')
        # If it's already a Discord timestamp keep it, otherwise wrap it
        if not ts.startswith('<t:'):
            try:
                ts = f"<t:{int(datetime.strptime(ts, '%b %d, %H:%M').replace(year=datetime.now().year).timestamp())}:f>"
            except Exception:
                pass
        lines.append(f"{ts} — {status_emoji} **{status}**")
        lines.append(f"> {desc}")
        for task in tasks:
            lines.append(f"> 📋 {task}")
        lines.append("")

    if not updates:
        lines.append("*No updates yet.*")

    return '\n'.join(lines)

async def post_or_update_webhook(incident: Dict, existing_msg_id: Optional[str] = None) -> Optional[str]:
    """Post new or edit existing webhook message. Returns message ID."""
    content = format_webhook_message(incident)
    payload = {"content": content}

    async with aiohttp.ClientSession() as session:
        if existing_msg_id:
            url = f"{STATUS_WEBHOOK}/messages/{existing_msg_id}"
            async with session.patch(url, json=payload) as resp:
                if resp.status == 200:
                    return existing_msg_id
                else:
                    text = await resp.text()
                    print(f"Webhook edit failed ({resp.status}): {text}")
                    return None
        else:
            url = f"{STATUS_WEBHOOK}?wait=true"
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return str(data['id'])
                else:
                    text = await resp.text()
                    print(f"Webhook post failed ({resp.status}): {text}")
                    return None

async def delete_webhook_message(msg_id: str):
    async with aiohttp.ClientSession() as session:
        url = f"{STATUS_WEBHOOK}/messages/{msg_id}"
        await session.delete(url)

async def verify_webhook_message(msg_id: str) -> bool:
    """Check if a webhook message still exists on Discord."""
    async with aiohttp.ClientSession() as session:
        url = f"{STATUS_WEBHOOK}/messages/{msg_id}"
        async with session.get(url) as resp:
            return resp.status == 200

async def sync_webhooks(incidents: List[Dict], author: str = "bot"):
    """Sync all incidents with webhook messages, surviving bot restarts."""
    ids = await load_webhook_ids()  # always fresh from GitHub
    new_ids = {}

    for inc in incidents:
        if inc.get('no_incidents'):
            continue
        key = f"{inc['date']}_{inc.get('title','')}"
        existing_id = ids.get(key)

        if existing_id:
            still_alive = await verify_webhook_message(existing_id)
            if still_alive:
                result_id = await post_or_update_webhook(inc, existing_id)
                new_ids[key] = result_id or existing_id
            else:
                # Message was manually deleted — repost fresh
                result_id = await post_or_update_webhook(inc, None)
                if result_id:
                    new_ids[key] = result_id
        else:
            # New incident — post for the first time
            result_id = await post_or_update_webhook(inc, None)
            if result_id:
                new_ids[key] = result_id

    # Remove webhook messages for incidents that were deleted
    for key, msg_id in ids.items():
        if key not in new_ids:
            await delete_webhook_message(msg_id)

    await save_webhook_ids(new_ids, author)

# ─── Session State ────────────────────────────────────────────────────────────
class ContentType(Enum):
    STATUS    = "status"
    CHANGELOG = "changelog"
    TEAM      = "team"
    BLOG      = "blog"

@dataclass
class Session:
    content_type: ContentType
    raw: str                          # current working copy
    file_path: str
    author: discord.Member
    interaction: discord.Interaction  # original interaction (ephemeral message)
    selected_index: Optional[int] = None  # which incident/version/member is selected

# Active sessions keyed by user ID
sessions: Dict[int, Session] = {}

# ─── Embed Builders ───────────────────────────────────────────────────────────
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
        emoji = INCIDENT_EMOJIS.get(inc.get('type','INCIDENT'), '🚨')
        sev = inc.get('severity','?')
        sev_emoji = {'low':'🟢','medium':'🟡','high':'🟠','critical':'🔴','maintenance':'🔧'}.get(sev,'⚪')
        tasks = inc.get('tasks', [])
        updates = inc.get('updates', [])
        last_status = updates[-1]['status'] if updates else "No updates"
        last_emoji = STATUS_EMOJIS.get(last_status.upper(), '⚪')

        val = (
            f"{sev_emoji} Severity: **{sev}**\n"
            f"🔧 Components: `{inc.get('components','—')}`\n"
            f"📋 Tasks: {len(tasks)} task(s)\n"
            f"📝 Updates: {len(updates)} update(s)\n"
            f"{last_emoji} Latest: **{last_status}**"
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
        content_preview = p.get('subheading', '')[:80] or (p.get('content', [{}])[0].get('data', '')[:80] if p.get('content') else '')
        
        embed.add_field(
            name=f"{marker}[{i+1}] {p['title']} — {p.get('date', '?')}",
            value=f"📁 {p.get('category', 'general').upper()}\n✍️ {p.get('author', 'Unknown')}\n{content_preview}…" if content_preview else f"📁 {p.get('category', 'general').upper()}\n✍️ {p.get('author', 'Unknown')}",
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

# ─── Select Menus ─────────────────────────────────────────────────────────────
class SelectItemDropdown(discord.ui.Select):
    def __init__(self, session: Session, action: str):
        self.session = session
        self.action = action
        options = self._build_options()
        super().__init__(
            placeholder=f"Select item to {action}…",
            min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="— empty —", value="__none__")]
        )

    def _build_options(self):
        opts = []
        if self.session.content_type == ContentType.STATUS:
            for i, inc in enumerate(parse_status(self.session.raw)):
                if not inc.get('no_incidents'):
                    emoji = INCIDENT_EMOJIS.get(inc.get('type', 'INCIDENT'), '🚨')
                    opts.append(discord.SelectOption(label=f"[{i+1}] {inc.get('title','Untitled')[:80]}", value=str(i), emoji=emoji))
        elif self.session.content_type == ContentType.CHANGELOG:
            for i, v in enumerate(parse_changelog(self.session.raw)):
                opts.append(discord.SelectOption(label=f"v{v['version']} — {v.get('date','?')}", value=str(i), emoji='📋'))
        elif self.session.content_type == ContentType.TEAM:
            for i, m in enumerate(parse_team(self.session.raw).get('members', [])):
                opts.append(discord.SelectOption(label=f"{m.get('name','?')} (@{m.get('handle','?')})", value=str(i), emoji='👤'))
        elif self.session.content_type == ContentType.BLOG:
            for i, p in enumerate(parse_blog(self.session.raw)):
                opts.append(discord.SelectOption(label=f"{p['title'][:80]}", value=str(i), emoji='📝'))
        return opts

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.defer()
            return
        self.session.selected_index = int(self.values[0])
        view = build_view(self.session)
        embed = build_embed(self.session)

        if self.action == "edit":
            # Launch appropriate modal
            await launch_edit_modal(interaction, self.session)
        elif self.action == "delete":
            # Delete and refresh
            await do_delete(interaction, self.session)
        elif self.action == "add_update":
            await launch_update_modal(interaction, self.session)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

async def launch_edit_modal(interaction: discord.Interaction, session: Session):
    if session.content_type == ContentType.STATUS:
        incidents = parse_status(session.raw)
        inc = incidents[session.selected_index]
        await interaction.response.send_modal(IncidentModal(session, inc))
    elif session.content_type == ContentType.CHANGELOG:
        versions = parse_changelog(session.raw)
        ver = versions[session.selected_index]
        await interaction.response.send_modal(VersionModal(session, ver))
    elif session.content_type == ContentType.TEAM:
        team = parse_team(session.raw)
        member = team['members'][session.selected_index]
        await interaction.response.send_modal(MemberModal(session, member))
    elif session.content_type == ContentType.BLOG:
        posts = parse_blog(session.raw)
        post = posts[session.selected_index]
        await interaction.response.send_modal(BlogPostModal(session, post))

async def launch_update_modal(interaction: discord.Interaction, session: Session):
    incidents = parse_status(session.raw)
    inc = incidents[session.selected_index]
    await interaction.response.send_modal(UpdateModal(session, inc))

async def do_delete(interaction: discord.Interaction, session: Session):
    idx = session.selected_index
    if session.content_type == ContentType.STATUS:
        items = parse_status(session.raw)
        items.pop(idx)
        session.raw = format_status(items)
    elif session.content_type == ContentType.CHANGELOG:
        items = parse_changelog(session.raw)
        items.pop(idx)
        session.raw = format_changelog(items)
    elif session.content_type == ContentType.TEAM:
        team = parse_team(session.raw)
        team['members'].pop(idx)
        session.raw = format_team(team)
    elif session.content_type == ContentType.BLOG:
        items = parse_blog(session.raw)
        items.pop(idx)
        session.raw = format_blog(items)
    session.selected_index = None
    await interaction.response.edit_message(embed=build_embed(session), view=build_view(session))

# ─── Component Selector ───────────────────────────────────────────────────────
class ComponentSelector(discord.ui.Select):
    def __init__(self, session: Session, modal_ref):
        self.session = session
        self.modal_ref = modal_ref
        options = [discord.SelectOption(label=c, value=c) for c in COMPONENT_OPTIONS]
        super().__init__(
            placeholder="Select affected components…",
            min_values=1,
            max_values=len(COMPONENT_OPTIONS),
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        self.modal_ref.selected_components = self.values
        await interaction.response.defer()

# ─── Modals ───────────────────────────────────────────────────────────────────
class IncidentModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Incident" if existing else "New Incident")
        self.session = session
        self.existing = existing
        self.selected_components = (existing.get('components','').split(', ') if existing and existing.get('components') else [])

        self.f_type = discord.ui.TextInput(
            label="Type",
            placeholder="INCIDENT or MAINTENANCE",
            default=existing.get('type','INCIDENT') if existing else 'INCIDENT',
            max_length=20
        )
        self.f_title = discord.ui.TextInput(
            label="Title",
            placeholder="Brief description of the incident",
            default=existing.get('title','') if existing else '',
            max_length=100
        )
        self.f_severity = discord.ui.TextInput(
            label="Severity",
            placeholder="low / medium / high / critical / maintenance",
            default=existing.get('severity','medium') if existing else 'medium',
            max_length=20
        )
        self.f_components = discord.ui.TextInput(
            label="Components (comma-separated)",
            placeholder="API, Website, Dashboard",
            default=existing.get('components','') if existing else '',
            max_length=200,
            required=False
        )
        self.f_task = discord.ui.TextInput(
            label="Task (required — what is being done?)",
            placeholder="Investigate elevated error rates on API",
            default=', '.join(existing.get('tasks',[])) if existing else '',
            max_length=300,
            style=discord.TextStyle.paragraph
        )

        self.add_item(self.f_type)
        self.add_item(self.f_title)
        self.add_item(self.f_severity)
        self.add_item(self.f_components)
        self.add_item(self.f_task)

    async def on_submit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        tasks = [t.strip() for t in self.f_task.value.split(',') if t.strip()]
        if not tasks:
            await interaction.response.send_message("❌ At least one task is required.", ephemeral=True)
            return

        new_inc = {
            'date': self.existing['date'] if self.existing else datetime.now().strftime("%b %d, %Y"),
            'type': self.f_type.value.upper().strip(),
            'title': self.f_title.value.strip(),
            'severity': self.f_severity.value.lower().strip(),
            'components': self.f_components.value.strip(),
            'tasks': tasks,
            'updates': self.existing.get('updates', []) if self.existing else []
        }

        if self.existing:
            for i, inc in enumerate(incidents):
                if inc.get('date') == self.existing['date'] and inc.get('title') == self.existing.get('title'):
                    incidents[i] = new_inc
                    break
        else:
            incidents.insert(0, new_inc)

        self.session.raw = format_status(incidents)
        self.session.selected_index = None
        await interaction.response.edit_message(embed=build_embed(self.session), view=build_view(self.session))

class UpdateModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title=f"Add Update")
        self.session = session
        self.incident = incident

        self.f_status = discord.ui.TextInput(
            label="Status",
            placeholder="INVESTIGATING / MONITORING / IDENTIFIED / RESOLVED",
            max_length=30
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            placeholder="What is the current situation?",
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        self.f_task = discord.ui.TextInput(
            label="Update Task (optional — leave blank to keep existing)",
            placeholder="Add or replace task description",
            required=False,
            max_length=300,
            style=discord.TextStyle.paragraph
        )

        self.add_item(self.f_status)
        self.add_item(self.f_desc)
        self.add_item(self.f_task)

    async def on_submit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        ts = f"<t:{int(datetime.now().timestamp())}:f>"

        for inc in incidents:
            if inc.get('date') == self.incident['date'] and inc.get('title') == self.incident.get('title'):
                inc['updates'].append({
                    'timestamp': ts,
                    'status': self.f_status.value.upper().strip(),
                    'description': self.f_desc.value.strip()
                })
                if self.f_task.value.strip():
                    new_tasks = [t.strip() for t in self.f_task.value.split(',') if t.strip()]
                    inc['tasks'] = new_tasks
                break

        self.session.raw = format_status(incidents)
        self.session.selected_index = None
        await interaction.response.edit_message(embed=build_embed(self.session), view=build_view(self.session))

class VersionModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Version" if existing else "New Version")
        self.session = session
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
            placeholder="FEATURE Added dark mode\nFIX Fixed login bug\nBREAKING Removed legacy API",
            default='\n'.join(f"{e['type']} {e['description']}" for e in existing.get('entries',[])) if existing else '',
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
            'date': self.f_date.value.strip(),
            'entries': entries
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
        await interaction.response.edit_message(embed=build_embed(self.session), view=build_view(self.session))

class MemberModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Member" if existing else "New Member")
        self.session = session
        self.existing = existing

        self.f_id = discord.ui.TextInput(
            label="Member ID (slug, no spaces)",
            placeholder="johndoe",
            default=existing['id'] if existing else '',
            max_length=30
        )
        self.f_name = discord.ui.TextInput(
            label="Display Name",
            placeholder="John Doe",
            default=existing.get('name','') if existing else '',
            max_length=50
        )
        self.f_handle = discord.ui.TextInput(
            label="Discord Handle",
            placeholder="johndoe",
            default=existing.get('handle','') if existing else '',
            max_length=50
        )
        self.f_roles = discord.ui.TextInput(
            label="Roles (comma-separated)",
            placeholder="Developer, Support, Admin",
            default=', '.join(existing.get('roles',[])) if existing else '',
            max_length=200
        )
        self.f_about = discord.ui.TextInput(
            label="About",
            placeholder="Short bio (optional)",
            default=existing.get('about','') if existing else '',
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
            'id': self.f_id.value.strip(),
            'name': self.f_name.value.strip(),
            'handle': self.f_handle.value.strip(),
            'roles': [r.strip() for r in self.f_roles.value.split(',') if r.strip()],
            'about': self.f_about.value.strip(),
            'status': 'Online',
            'joinedYear': str(datetime.now().year),
            'avatarUrl': '',
            'timeline': [],
            'skills': [],
            'tags': [],
            'stats': [],
            'dataFields': []
        }

        if self.existing:
            # Preserve extra fields from existing
            merged = {**self.existing, **new_member}
            for i, m in enumerate(team['members']):
                if m['id'] == self.existing['id']:
                    team['members'][i] = merged
                    break
        else:
            team['members'].append(new_member)

        self.session.raw = format_team(team)
        self.session.selected_index = None
        await interaction.response.edit_message(embed=build_embed(self.session), view=build_view(self.session))

class BlogPostModal(discord.ui.Modal):
    def __init__(self, session: Session, existing: Optional[Dict] = None):
        super().__init__(title="Edit Blog Post" if existing else "New Blog Post")
        self.session = session
        self.existing = existing

        self.f_id = discord.ui.TextInput(
            label="Post ID (slug, no spaces)",
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
            placeholder="A brief description of this post",
            default=existing.get('subheading', '') if existing else '',
            required=False,
            max_length=300
        )
        self.f_date = discord.ui.TextInput(
            label="Date",
            placeholder="YYYY-MM-DD",
            default=existing.get('date', datetime.now().strftime("%Y-%m-%d")) if existing else datetime.now().strftime("%Y-%m-%d"),
            max_length=20
        )
        self.f_author = discord.ui.TextInput(
            label="Author (Discord name)",
            placeholder="Johan",
            default=existing.get('author', '') if existing else '',
            max_length=100
        )
        self.f_category = discord.ui.TextInput(
            label="Category",
            placeholder="announcements, guides, security, updates",
            default=existing.get('category', 'general') if existing else 'general',
            max_length=50
        )
        self.f_image = discord.ui.TextInput(
            label="Feature Image URL",
            placeholder="https://example.com/image.jpg",
            default=existing.get('featureImage', '') if existing else '',
            required=False,
            max_length=500
        )
        self.f_content = discord.ui.TextInput(
            label="Content (format: BLOCK:type\ndata)",
            placeholder="BLOCK:paragraph\nWelcome to my post!\nBLOCK:heading\nSection Title\nBLOCK:image\nhttps://example.com/img.jpg\nCaption here",
            default='\n'.join(f"BLOCK:{b['type']}\n{b['data']}" for b in existing.get('content', [])) if existing else '',
            style=discord.TextStyle.paragraph,
            max_length=4000
        )

        self.add_item(self.f_id)
        self.add_item(self.f_title)
        self.add_item(self.f_subheading)
        self.add_item(self.f_date)
        self.add_item(self.f_author)
        self.add_item(self.f_category)
        self.add_item(self.f_image)
        self.add_item(self.f_content)

    async def on_submit(self, interaction: discord.Interaction):
        posts = parse_blog(self.session.raw)
        
        # Parse content blocks
        content_blocks = []
        lines = self.f_content.value.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('BLOCK:'):
                block_type = line.replace('BLOCK:', '').strip()
                i += 1
                if i < len(lines):
                    block_data = lines[i].strip()
                    content_blocks.append({'type': block_type, 'data': block_data})
            i += 1
        
        new_post = {
            'id': self.f_id.value.strip().lower().replace(' ', '-'),
            'title': self.f_title.value.strip(),
            'subheading': self.f_subheading.value.strip(),
            'date': self.f_date.value.strip(),
            'author': self.f_author.value.strip(),
            'category': self.f_category.value.strip().lower(),
            'featureImage': self.f_image.value.strip(),
            'content': content_blocks
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
        await interaction.response.edit_message(embed=build_embed(self.session), view=build_view(self.session))

class ActionDropdown(discord.ui.Select):
    def __init__(self, session: Session, actions: list[tuple[str, str]]):
        self.session = session
        options = [discord.SelectOption(label=label, value=value) for label, value in actions]
        super().__init__(placeholder="Choose an action…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "add_incident":
            await interaction.response.send_modal(IncidentModal(self.session))
        elif action == "add_version":
            await interaction.response.send_modal(VersionModal(self.session))
        elif action == "add_member":
            await interaction.response.send_modal(MemberModal(self.session))
        elif action in ("edit", "delete", "add_update"):
            view = discord.ui.View(timeout=60)
            view.add_item(SelectItemDropdown(self.session, action))
            await interaction.response.edit_message(view=view)
        elif action == "save_status":
            await interaction.response.defer()
            ok = await commit_file(self.session.file_path, self.session.raw, "Update status", str(self.session.author))
            if ok:
                incidents = parse_status(self.session.raw)
                await sync_webhooks(incidents, str(self.session.author))
                embed = build_embed(self.session)
                embed.color = 0x57F287
                embed.set_footer(text="✅ Saved to GitHub & webhook updated!")
                await interaction.edit_original_response(embed=embed, view=build_view(self.session))
            else:
                await interaction.followup.send("❌ Failed to save!", ephemeral=True)
        elif action == "save":
            await interaction.response.defer()
            label = "changelog" if self.session.content_type == ContentType.CHANGELOG else "team"
            ok = await commit_file(self.session.file_path, self.session.raw, f"Update {label}", str(self.session.author))
            if ok:
                embed = build_embed(self.session)
                embed.color = 0x57F287
                embed.set_footer(text="✅ Saved to GitHub!")
                await interaction.edit_original_response(embed=embed, view=build_view(self.session))
            else:
                await interaction.followup.send("❌ Failed to save!", ephemeral=True)
        elif action == "cancel":
            sessions.pop(self.session.author.id, None)
            await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

# ─── Views ────────────────────────────────────────────────────────────────────
def build_view(session: Session) -> discord.ui.View:
    if session.content_type == ContentType.STATUS:
        return StatusView(session)
    elif session.content_type == ContentType.CHANGELOG:
        return ChangelogView(session)
    elif session.content_type == ContentType.TEAM:
        return TeamView(session)
    elif session.content_type == ContentType.BLOG:
        return BlogView(session)

class StatusView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session
        self.add_item(ActionDropdown(session, [
            ("➕ New Incident",  "add_incident"),
            ("✏️ Edit Incident",  "edit"),
            ("📝 Add Update",     "add_update"),
            ("🗑️ Delete Incident","delete"),
            ("💾 Save & Post",    "save_status"),
            ("↩️ Cancel",         "cancel"),
        ]))

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ New Incident", style=discord.ButtonStyle.success, row=1)
    async def add_incident(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IncidentModal(self.session))

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=1)
    async def edit_incident(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "edit"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="📝 Add Update", style=discord.ButtonStyle.primary, row=1)
    async def add_update(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "add_update"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=1)
    async def delete_incident(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "delete"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="💾 Save & Post", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update status", str(self.session.author))
        if ok:
            incidents = parse_status(self.session.raw)
            await sync_webhooks(incidents, str(self.session.author))
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub & webhook updated!")
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Failed to save! Check GitHub token.", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

class ChangelogView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session
        self.add_item(ActionDropdown(session, [
            ("➕ New Version",   "add_version"),
            ("✏️ Edit Version",  "edit"),
            ("🗑️ Delete Version","delete"),
            ("💾 Save",          "save"),
            ("↩️ Cancel",        "cancel"),
        ]))

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ New Version", style=discord.ButtonStyle.success, row=1)
    async def add_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VersionModal(self.session))

    @discord.ui.button(label="✏️ Edit Version", style=discord.ButtonStyle.primary, row=1)
    async def edit_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "edit"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="🗑️ Delete Version", style=discord.ButtonStyle.danger, row=1)
    async def delete_version(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "delete"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update changelog", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Failed to save! Check GitHub token.", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

class BlogView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session
        self.add_item(ActionDropdown(session, [
            ("➕ New Post",     "add_post"),
            ("✏️ Edit Post",    "edit"),
            ("🗑️ Delete Post",  "delete"),
            ("💾 Save",         "save"),
            ("↩️ Cancel",       "cancel"),
        ]))

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ New Post", style=discord.ButtonStyle.success, row=1)
    async def add_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BlogPostModal(self.session))

    @discord.ui.button(label="✏️ Edit Post", style=discord.ButtonStyle.primary, row=1)
    async def edit_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "edit"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="🗑️ Delete Post", style=discord.ButtonStyle.danger, row=1)
    async def delete_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "delete"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update blog", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Failed to save! Check GitHub token.", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

class TeamView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session
        self.add_item(ActionDropdown(session, [
            ("➕ Add Member",   "add_member"),
            ("✏️ Edit Member",  "edit"),
            ("🗑️ Remove Member","delete"),
            ("💾 Save",         "save"),
            ("↩️ Cancel",       "cancel"),
        ]))

    async def on_timeout(self):
        sessions.pop(self.session.author.id, None)

    @discord.ui.button(label="➕ Add Member", style=discord.ButtonStyle.success, row=1)
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MemberModal(self.session))

    @discord.ui.button(label="✏️ Edit Member", style=discord.ButtonStyle.primary, row=1)
    async def edit_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "edit"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="🗑️ Remove Member", style=discord.ButtonStyle.danger, row=1)
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=60)
        view.add_item(SelectItemDropdown(self.session, "delete"))
        await interaction.response.edit_message(view=view)

    @discord.ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await commit_file(self.session.file_path, self.session.raw, "Update team", str(self.session.author))
        if ok:
            embed = build_embed(self.session)
            embed.color = 0x57F287
            embed.set_footer(text="✅ Saved to GitHub!")
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Failed to save! Check GitHub token.", ephemeral=True)

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions.pop(self.session.author.id, None)
        await interaction.response.edit_message(content="*Session cancelled.*", embed=None, view=None)

# ─── Slash Commands ───────────────────────────────────────────────────────────
ALLOWED_ROLE_ID = 1444271393570160680

def has_permission(member: discord.Member) -> bool:
    return any(role.id == ALLOWED_ROLE_ID for role in member.roles)

@bot.tree.command(name="edit", description="Edit website content (status, changelog, team, blog)")
@app_commands.describe(content_type="What do you want to edit?")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status",    value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team",      value="team"),
    app_commands.Choice(name="Blog",      value="blog"),  # ADD THIS
])
async def edit_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    if not has_permission(interaction.user):
        await interaction.response.send_message("❌ You don't have permission to edit content.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    ct = ContentType(content_type.value)
    fp = {
        ContentType.STATUS: STATUS_FILE, 
        ContentType.CHANGELOG: CHANGELOG_FILE, 
        ContentType.TEAM: TEAM_FILE,
        ContentType.BLOG: BLOG_FILE
    }[ct]

    raw = await get_file_content(fp)
    if not raw:
        raw = ""

    session = Session(
        content_type=ct,
        raw=raw,
        file_path=fp,
        author=interaction.user,
        interaction=interaction
    )
    sessions[interaction.user.id] = session

    embed = build_embed(session)
    view = build_view(session)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="view", description="View current website content")
@app_commands.describe(content_type="What do you want to view?")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status",    value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team",      value="team"),
    app_commands.Choice(name="Blog",      value="blog"),  # ADD THIS
])
async def view_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    ct = ContentType(content_type.value)
    fp = {
        ContentType.STATUS: STATUS_FILE,
        ContentType.CHANGELOG: CHANGELOG_FILE,
        ContentType.TEAM: TEAM_FILE,
        ContentType.BLOG: BLOG_FILE  # ADD THIS
    }[ct]

    raw = await get_file_content(fp)
    if not raw:
        await interaction.followup.send("*No content found or GitHub not configured.*", ephemeral=True)
        return

    # Make a read-only session just for display
    session = Session(content_type=ct, raw=raw, file_path=fp, author=interaction.user, interaction=interaction)
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
    await interaction.followup.send(f"✅ Synced {len([i for i in incidents if not i.get('no_incidents')])} incident(s) to webhook.", ephemeral=True)

@bot.tree.command(name="help_editor", description="Show help for the content editor")
async def help_editor(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Content Editor Help", color=0x5865F2)
    embed.add_field(name="Commands", value="`/edit <type>` — Open editor\n`/view <type>` — View without editing\n`/sync_webhooks` — Re-sync status to channel", inline=False)
    embed.add_field(name="Status", value="• Add incidents with type, severity, components & tasks\n• **Tasks are required** on every incident\n• Add timestamped updates (INVESTIGATING → MONITORING → RESOLVED)\n• Saving auto-posts/edits the webhook channel message", inline=False)
    embed.add_field(name="Changelog", value="• Add versions with date and typed entries\n• Entry format: `TYPE Description` (FEATURE, FIX, BREAKING, etc.)", inline=False)
    embed.add_field(name="Team", value="• Add/edit/remove team members\n• Fields: ID, name, Discord handle, roles, bio", inline=False)
    embed.add_field(name="Webhook behaviour", value="• Each incident gets its own message in the status channel\n• Saving updates that message in-place\n• Deleting an incident removes its webhook message\n• Resolving keeps the message but marks it resolved", inline=False)
    embed.set_footer(text="All editor sessions are ephemeral (only visible to you)")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    embed.add_field(name="Blog", value="• Add blog posts with ID, title, subheading, date, author, category, feature image\n• Content uses BLOCK system: paragraph, heading, subheading, image, two_column, accordion, slideshow, code, callout, quote, list, divider\n• Each post appears on the /blog page", inline=False)

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
