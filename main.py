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

# ─── Configuration ────────────────────────────────────────────────────────────
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

# ─── File paths ───────────────────────────────────────────────────────────────
STATUS_FILE      = "status.txt"
WEBHOOK_IDS_FILE = "webhook_message_ids.json"

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

SEVERITY_OPTIONS = ['low', 'medium', 'high', 'critical']

COMPONENT_OPTIONS = [
    'Seshy RuntimeEngine', 
    'Seshy Modules', 
    'Seshy Database', 
    'Seshy AI',
    'GitHub Data Store',
    'Website'
]

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
        # Don't sync on startup - only sync when status actually changes
        if not self._initialized:
            # Just load the initial hash, don't sync
            raw = await get_file_content(STATUS_FILE)
            if raw:
                self._status_hash = hashlib.sha256(raw.encode()).hexdigest()
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

# ─── Status Parsing ───────────────────────────────────────────────────────────
_DATE_HEADER = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$')
_UPDATE_STRUCTURED = re.compile(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w[\w ]*?)\s+-\s+(.+)$')
_UPDATE_BARE = re.compile(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+(.+)$')

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
                ts = u['timestamp']
                stat = u.get('status', '')
                desc = u['description']
                if stat:
                    lines.append(f"{ts} - {stat} - {desc}")
                else:
                    lines.append(f"{ts} {desc}")
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks) + '\n'

# ─── Webhook Management ─────────────────────────────────────────────────────
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
            ts = u.get('timestamp', '')
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

def _incident_key(inc: Dict) -> str:
    # Use date, type, and title to create a unique key
    inc_type = inc.get('type', 'INCIDENT')
    title = inc.get('title', '')
    return f"{inc['date']}|{inc_type}|{title}"

async def sync_webhooks(incidents: List[Dict], author: str = "bot"):
    """Sync webhook messages with current status - only update existing, create new ones."""
    ids = await load_webhook_ids()
    new_ids = {}
    
    for inc in incidents:
        if inc.get('no_incidents') or not inc.get('title'):
            continue
        
        key = _incident_key(inc)
        existing_id = ids.get(key)
        
        if existing_id:
            # Update existing message
            success = await update_webhook_message(existing_id, inc)
            if success:
                new_ids[key] = existing_id
            else:
                # Message was deleted, create new one
                if inc.get('updates'):
                    new_id = await create_webhook_message(inc)
                    if new_id:
                        new_ids[key] = new_id
        else:
            # Only create new messages for incidents with updates
            # AND only if they don't already have a message
            if inc.get('updates'):
                new_id = await create_webhook_message(inc)
                if new_id:
                    new_ids[key] = new_id
    
    # Only delete messages for incidents that were removed from the file
    # Don't delete messages for incidents that just moved position
    for key, msg_id in ids.items():
        if key not in new_ids:
            # Check if this incident still exists somewhere in the file
            still_exists = any(
                _incident_key(inc) == key 
                for inc in incidents 
                if not inc.get('no_incidents') and inc.get('title')
            )
            if not still_exists:
                await delete_webhook_message(msg_id)
    
    await save_webhook_ids(new_ids, author)

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
        new_hash = hashlib.sha256(raw.encode()).hexdigest()
        if new_hash == bot._status_hash:
            return
        
        print(f"[poll_status] status.txt changed — syncing webhooks")
        bot._processing = True
        incidents = parse_status(raw)
        await sync_webhooks(incidents, "auto-poll")
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

# ─── Status View with Pagination ──────────────────────────────────────────────
class StatusPaginationView(discord.ui.View):
    def __init__(self, session: Session):
        super().__init__(timeout=600)
        self.session = session
        
        # Add action dropdown
        self.add_item(ActionDropdown(session))
        
        # Add pagination buttons
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
            await self.select_incident_for_edit(interaction)
        elif action == "add_update":
            await self.select_incident_for_update(interaction)
        elif action == "delete":
            await self.select_incident_for_delete(interaction)
        elif action == "save":
            await self.save_and_sync(interaction)
    
    async def start_incident_creation(self, interaction: discord.Interaction, inc_type: str):
        """Start the step-by-step incident creation."""
        incidents = parse_status(self.session.raw)
        
        # Create basic incident structure
        new_inc = {
            'date': datetime.now().strftime('%b %-d, %Y'),
            'type': inc_type,
            'title': '',
            'severity': 'medium',
            'components': '',
            'updates': [],
            'no_incidents': False,
        }
        
        # Step 1: Get title
        await interaction.response.send_modal(TitleModal(self.session, new_inc))
    
    async def select_incident_for_edit(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        active_incidents = [i for i in incidents if not i.get('no_incidents') and i.get('title')]
        
        if not active_incidents:
            await interaction.response.send_message("No incidents to edit!", ephemeral=True)
            return
        
        options = []
        for i, inc in enumerate(active_incidents):
            emoji = "🚨" if inc.get('type') == 'INCIDENT' else "🔧"
            options.append(discord.SelectOption(
                label=f"{inc.get('title', 'Untitled')[:80]}",
                value=str(i),
                emoji=emoji
            ))
        
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Select incident to edit...", options=options)
        
        async def edit_callback(inter: discord.Interaction):
            idx = int(select.values[0])
            target_inc = active_incidents[idx]
            await inter.response.send_modal(EditIncidentModal(self.session, target_inc))
        
        select.callback = edit_callback
        view.add_item(select)
        await interaction.response.send_message("Select incident to edit:", view=view, ephemeral=True)
    
    async def select_incident_for_update(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        active_incidents = [i for i in incidents if not i.get('no_incidents') and i.get('title')]
        
        if not active_incidents:
            await interaction.response.send_message("No incidents to update!", ephemeral=True)
            return
        
        options = []
        for i, inc in enumerate(active_incidents):
            emoji = "🚨" if inc.get('type') == 'INCIDENT' else "🔧"
            options.append(discord.SelectOption(
                label=f"{inc.get('title', 'Untitled')[:80]}",
                value=str(i),
                emoji=emoji
            ))
        
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Select incident to update...", options=options)
        
        async def update_callback(inter: discord.Interaction):
            idx = int(select.values[0])
            target_inc = active_incidents[idx]
            await inter.response.send_modal(AddUpdateModal(self.session, target_inc))
        
        select.callback = update_callback
        view.add_item(select)
        await interaction.response.send_message("Select incident to add update:", view=view, ephemeral=True)
    
    async def select_incident_for_delete(self, interaction: discord.Interaction):
        incidents = parse_status(self.session.raw)
        active_incidents = [i for i in incidents if not i.get('no_incidents') and i.get('title')]
        
        if not active_incidents:
            await interaction.response.send_message("No incidents to delete!", ephemeral=True)
            return
        
        options = []
        for i, inc in enumerate(active_incidents):
            emoji = "🚨" if inc.get('type') == 'INCIDENT' else "🔧"
            options.append(discord.SelectOption(
                label=f"{inc.get('title', 'Untitled')[:80]}",
                value=str(i),
                emoji=emoji
            ))
        
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Select incident to delete...", options=options)
        
        async def delete_callback(inter: discord.Interaction):
            idx = int(select.values[0])
            target_inc = active_incidents[idx]
            
            # Remove the incident
            for i, inc in enumerate(incidents):
                if inc.get('date') == target_inc['date'] and inc.get('title') == target_inc.get('title'):
                    incidents.pop(i)
                    break
            
            self.session.raw = format_status(incidents)
            
            embed = build_status_embed(self.session)
            embed.color = 0xFF0000
            embed.set_footer(text=f"✅ Deleted: {target_inc.get('title')}")
            
            await inter.response.edit_message(
                embed=embed,
                view=StatusPaginationView(self.session)
            )
        
        select.callback = delete_callback
        view.add_item(select)
        await interaction.response.send_message("Select incident to delete:", view=view, ephemeral=True)
    
    async def save_and_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # Save to GitHub first
        ok = await commit_file(STATUS_FILE, self.session.raw, "Update status", str(self.session.author))
        
        if not ok:
            await interaction.followup.send("❌ Failed to save to GitHub! Check token.", ephemeral=True)
            return
        
        # Update hash to prevent poller from re-triggering
        bot._status_hash = hashlib.sha256(self.session.raw.encode()).hexdigest()
        
        # Sync webhooks
        incidents = parse_status(self.session.raw)
        await sync_webhooks(incidents, str(self.session.author))
        
        await interaction.followup.send("✅ Saved to GitHub and synced to webhook!", ephemeral=True)

# ─── Modals for Step-by-Step Creation ────────────────────────────────────────
class TitleModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Step 1: Incident Title")
        self.session = session
        self.incident = incident
        
        self.title_input = discord.ui.TextInput(
            label="Title",
            placeholder="Brief description of the incident",
            max_length=100,
            required=True
        )
        self.add_item(self.title_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        self.incident['title'] = self.title_input.value.strip()
        
        # Step 2: Select severity
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(
            placeholder="Select severity level...",
            options=[
                discord.SelectOption(label="Low", value="low", emoji="🟢"),
                discord.SelectOption(label="Medium", value="medium", emoji="🟡"),
                discord.SelectOption(label="High", value="high", emoji="🟠"),
                discord.SelectOption(label="Critical", value="critical", emoji="🔴"),
            ]
        )
        
        async def severity_callback(inter: discord.Interaction):
            self.incident['severity'] = select.values[0]
            
            # Step 3: Select components (multi-select)
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
        self.selected_components = []
        
        # Add multi-select for components
        options = [
            discord.SelectOption(label=comp, value=comp)
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
        
        # Add confirm button
        confirm_btn = discord.ui.Button(label="✅ Confirm Components", style=discord.ButtonStyle.success, row=1)
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
        
        # Step 4: Initial update
        await interaction.response.send_modal(InitialUpdateModal(self.session, self.incident))

class InitialUpdateModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Step 4: Initial Update")
        self.session = session
        self.incident = incident
        
        self.description = discord.ui.TextInput(
            label="Description",
            placeholder="What is the current situation?",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True
        )
        self.add_item(self.description)
    
    async def on_submit(self, interaction: discord.Interaction):
        # For maintenance, use SCHEDULED as default status instead of INVESTIGATING
        default_status = 'SCHEDULED' if self.incident.get('type') == 'MAINTENANCE' else 'INVESTIGATING'
        
        ts = datetime.now().strftime('%b %-d, %H:%M')
        self.incident['updates'].append({
            'timestamp': ts,
            'status': default_status,
            'description': self.description.value.strip(),
        })
        
        # Add to incidents list
        incidents = parse_status(self.session.raw)
        # Remove any "no incidents" entry
        incidents = [i for i in incidents if not i.get('no_incidents')]
        incidents.insert(0, self.incident)
        
        self.session.raw = format_status(incidents)
        
        # Show success and refresh
        embed = build_status_embed(self.session)
        embed.color = 0x57F287
        embed.set_footer(text="✅ Incident created! Use 'Save & Post' to publish.")
        
        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

# ─── Edit Incident Modal ──────────────────────────────────────────────────────
class EditIncidentModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Edit Incident")
        self.session = session
        self.incident = incident
        
        self.title_input = discord.ui.TextInput(
            label="Title",
            default=incident.get('title', ''),
            max_length=100,
            required=True
        )
        self.add_item(self.title_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        self.incident['title'] = self.title_input.value.strip()
        
        # Select severity
        current_sev = self.incident.get('severity', 'medium')
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(
            placeholder=f"Current: {current_sev}",
            options=[
                discord.SelectOption(label="Low", value="low", emoji="🟢"),
                discord.SelectOption(label="Medium", value="medium", emoji="🟡"),
                discord.SelectOption(label="High", value="high", emoji="🟠"),
                discord.SelectOption(label="Critical", value="critical", emoji="🔴"),
            ]
        )
        
        async def severity_callback(inter: discord.Interaction):
            self.incident['severity'] = select.values[0]
            
            # Pre-select current components
            current_comps = [c.strip() for c in self.incident.get('components', '').split(',') if c.strip()]
            await inter.response.send_message(
                "Select components:",
                view=EditComponentsView(self.session, self.incident, current_comps),
                ephemeral=True
            )
        
        select.callback = severity_callback
        view.add_item(select)
        await interaction.response.send_message("Select severity:", view=view, ephemeral=True)

class EditComponentsView(discord.ui.View):
    def __init__(self, session: Session, incident: Dict, current_comps: List[str]):
        super().__init__(timeout=120)
        self.session = session
        self.incident = incident
        self.selected_components = current_comps
        
        options = [
            discord.SelectOption(
                label=comp,
                value=comp,
                default=comp in current_comps
            )
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
        
        self.incident['components'] = ', '.join(self.selected_components)
        
        # Update the raw content
        incidents = parse_status(self.session.raw)
        for i, inc in enumerate(incidents):
            if inc.get('date') == self.incident['date'] and inc.get('title') == self.incident.get('title'):
                incidents[i] = self.incident
                break
        
        self.session.raw = format_status(incidents)
        
        embed = build_status_embed(self.session)
        embed.color = 0x57F287
        embed.set_footer(text="✅ Incident updated! Use 'Save & Post' to publish.")
        
        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

# ─── Add Update Modal ─────────────────────────────────────────────────────────
class AddUpdateModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict):
        super().__init__(title="Add Update - Step 1: Status")
        self.session = session
        self.incident = incident
        # Empty modal - we just use it as a trigger
        self.add_item(discord.ui.TextInput(
            label="Click Submit to select status first",
            placeholder="This will open the status selector...",
            required=False,
            max_length=1,
            default="."
        ))
    
    async def on_submit(self, interaction: discord.Interaction):
        # Select status FIRST
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(
            placeholder="Select status for this update...",
            options=[
                discord.SelectOption(label="Investigating", value="INVESTIGATING", emoji="🔴"),
                discord.SelectOption(label="Monitoring", value="MONITORING", emoji="🟡"),
                discord.SelectOption(label="Identified", value="IDENTIFIED", emoji="🟠"),
                discord.SelectOption(label="Resolved", value="RESOLVED", emoji="🟢"),
                discord.SelectOption(label="Scheduled", value="SCHEDULED", emoji="📅"),
                discord.SelectOption(label="In Progress", value="IN PROGRESS", emoji="⚙️"),
                discord.SelectOption(label="Completed", value="COMPLETED", emoji="✅"),
                discord.SelectOption(label="Maintenance", value="MAINTENANCE", emoji="🔧"),
            ]
        )
        
        async def status_callback(inter: discord.Interaction):
            selected_status = select.values[0]
            # Now ask for description
            await inter.response.send_modal(UpdateDescriptionModal(
                self.session, self.incident, selected_status
            ))
        
        select.callback = status_callback
        view.add_item(select)
        await interaction.response.send_message(
            "Step 1: Select the status for this update:", 
            view=view, 
            ephemeral=True
        )

class UpdateDescriptionModal(discord.ui.Modal):
    def __init__(self, session: Session, incident: Dict, status: str):
        super().__init__(title="Step 2: Description")
        self.session = session
        self.incident = incident
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
        ts = datetime.now().strftime('%b %-d, %H:%M')
        
        # Add update to incident
        incidents = parse_status(self.session.raw)
        for inc in incidents:
            if inc.get('date') == self.incident['date'] and inc.get('title') == self.incident.get('title'):
                inc['updates'].append({
                    'timestamp': ts,
                    'status': self.status,
                    'description': self.description.value.strip(),
                })
                break
        
        self.session.raw = format_status(incidents)
        
        embed = build_status_embed(self.session)
        embed.color = 0x57F287
        embed.set_footer(text="✅ Update added! Use 'Save & Post' to publish.")
        
        await interaction.response.edit_message(
            embed=embed,
            view=StatusPaginationView(self.session)
        )

# ─── Status Embed Builder ─────────────────────────────────────────────────────
def build_status_embed(session: Session) -> discord.Embed:
    incidents = parse_status(session.raw)
    
    embed = discord.Embed(
        title="🛠️ Status Editor",
        color=0x5865F2
    )
    embed.set_footer(text=f"Editor: {session.author.display_name}")
    
    # Filter out "no incidents" entries for display
    active_incidents = [i for i in incidents if not i.get('no_incidents')]
    
    if not active_incidents:
        embed.description = "*No active incidents.*"
        return embed
    
    # Paginate
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
            last_status = last.get('status', 'free-text update')
            last_emoji = STATUS_EMOJIS.get(last_status.upper(), '⚪')
        
        # Truncate title for display
        title = inc.get('title', 'Untitled')
        if len(title) > 60:
            title = title[:57] + "..."
        
        val = (
            f"{sev_e} Severity: **{sev}**\n"
            f"🔧 Components: `{inc.get('components','—')}`\n"
            f"📝 Updates: {len(updates)}\n"
            f"{last_emoji} Latest: **{last_status}**"
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
    session = Session(
        raw=raw,
        author=interaction.user,
        interaction=interaction
    )
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