# main.py - with better error handling and logging
import sys
import os
import traceback
import warnings
warnings.filterwarnings('ignore', category=SyntaxWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)

# Enable logging to see what's happening
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
from dataclasses import dataclass
from enum import Enum
import re
from github import Github, Auth

# Print startup info for debugging
print("Starting bot initialization...")
print(f"Python version: {sys.version}")

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')
GUILD_ID = os.getenv('GUILD_ID')

print(f"Discord token present: {bool(DISCORD_TOKEN)}")
print(f"GitHub token present: {bool(GITHUB_TOKEN)}")
print(f"GitHub repo: {GITHUB_REPO}")

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set!")
    sys.exit(1)

# File paths in your repo
STATUS_FILE = "status.txt"
CHANGELOG_FILE = "changelog.txt"
TEAM_FILE = "teams.json"

# Discord bot setup
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

# GitHub client with new authentication
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

class ContentManager:
    """Manages all content types and their file operations"""
    
    def __init__(self):
        self.active_sessions = {}
    
    async def get_file_content(self, file_path: str) -> str:
        """Get file content from GitHub"""
        if not repo:
            return "Error: GitHub not configured. Set GITHUB_TOKEN and GITHUB_REPO environment variables."
        try:
            contents = repo.get_contents(file_path)
            return base64.b64decode(contents.content).decode('utf-8')
        except Exception as e:
            print(f"Error fetching {file_path}: {e}")
            return f"Error fetching file: {str(e)}"
    
    async def commit_changes(self, file_path: str, content: str, commit_message: str, author: str):
        """Commit changes to GitHub"""
        if not repo:
            return False
        try:
            try:
                contents = repo.get_contents(file_path)
                repo.update_file(
                    file_path,
                    f"{commit_message} (by {author})",
                    content,
                    contents.sha
                )
            except:
                repo.create_file(
                    file_path,
                    f"{commit_message} (by {author})",
                    content
                )
            return True
        except Exception as e:
            print(f"GitHub commit error: {e}")
            return False

class StatusManager(ContentManager):
    """Manages status.txt operations"""
    
    @staticmethod
    def parse_status(content: str) -> List[Dict]:
        """Parse status.txt into structured data"""
        incidents = []
        current_incident = None
        
        lines = content.strip().split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i].strip()
            
            if not line:
                i += 1
                continue
                
            date_match = re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}$', line)
            if date_match:
                if current_incident:
                    incidents.append(current_incident)
                
                current_incident = {
                    'date': line,
                    'updates': []
                }
                i += 1
                continue
            
            incident_match = re.match(r'^(INCIDENT|MAINTENANCE):\s+(.+)$', line)
            if incident_match and current_incident:
                current_incident['type'] = incident_match.group(1)
                current_incident['title'] = incident_match.group(2)
                i += 1
                continue
            
            severity_match = re.match(r'^SEVERITY:\s+(.+)$', line)
            if severity_match and current_incident:
                current_incident['severity'] = severity_match.group(1)
                i += 1
                continue
            
            components_match = re.match(r'^COMPONENTS:\s+(.+)$', line)
            if components_match and current_incident:
                current_incident['components'] = components_match.group(1)
                i += 1
                continue
            
            update_match = re.match(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w+)\s+-\s+(.+)$', line)
            if update_match and current_incident:
                current_incident['updates'].append({
                    'timestamp': update_match.group(1),
                    'status': update_match.group(2),
                    'description': update_match.group(3)
                })
                i += 1
                continue
            
            if line == "No incidents reported." and current_incident:
                current_incident['no_incidents'] = True
                i += 1
                continue
            
            i += 1
        
        if current_incident:
            incidents.append(current_incident)
        
        return incidents
    
    @staticmethod
    def format_status(incidents: List[Dict]) -> str:
        """Format incidents back to status.txt format"""
        output = []
        
        for incident in incidents:
            output.append(incident['date'])
            
            if incident.get('no_incidents'):
                output.append("No incidents reported.")
                output.append("")
                continue
            
            output.append(f"{incident['type']}: {incident['title']}")
            output.append(f"SEVERITY: {incident['severity']}")
            output.append(f"COMPONENTS: {incident['components']}")
            
            for update in incident['updates']:
                output.append(f"{update['timestamp']} - {update['status']} - {update['description']}")
            
            output.append("")
        
        return '\n'.join(output)

class ChangelogManager(ContentManager):
    """Manages changelog operations"""
    
    @staticmethod
    def parse_changelog(content: str) -> List[Dict]:
        """Parse changelog into structured data"""
        versions = []
        current_version = None
        
        lines = content.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            version_match = re.match(r'^VERSION\s+(.+)$', line)
            if version_match:
                if current_version:
                    versions.append(current_version)
                current_version = {
                    'version': version_match.group(1),
                    'entries': []
                }
                continue
            
            if current_version is not None:
                date_match = re.match(r'^DATE\s+(.+)$', line)
                if date_match:
                    current_version['date'] = date_match.group(1)
                    continue
                
                entry_match = re.match(r'^(\w+)\s+(.+)$', line)
                if entry_match:
                    current_version['entries'].append({
                        'type': entry_match.group(1),
                        'description': entry_match.group(2)
                    })
        
        if current_version:
            versions.append(current_version)
        
        return versions
    
    @staticmethod
    def format_changelog(versions: List[Dict]) -> str:
        """Format versions back to changelog format"""
        output = []
        
        for version in versions:
            output.append(f"VERSION {version['version']}")
            output.append(f"DATE {version['date']}")
            
            for entry in version['entries']:
                output.append(f"{entry['type']} {entry['description']}")
            
            output.append("")
        
        return '\n'.join(output)

class TeamManager(ContentManager):
    """Manages team.json operations"""
    
    @staticmethod
    def parse_team(content: str) -> Dict:
        """Parse team.json into structured data"""
        try:
            return json.loads(content)
        except:
            return {"members": []}
    
    @staticmethod
    def format_team(team_data: Dict) -> str:
        """Format team data back to JSON"""
        return json.dumps(team_data, indent=2)

# Initialize managers
status_manager = StatusManager()
changelog_manager = ChangelogManager()
team_manager = TeamManager()

class ContentType(Enum):
    STATUS = "status"
    CHANGELOG = "changelog"
    TEAM = "team"

@dataclass
class ContentSession:
    """Represents an active editing session"""
    content_type: ContentType
    data: Any
    original_content: str
    file_path: str
    author: discord.Member
    message: discord.Message

class ContentView(discord.ui.View):
    """Interactive view for content management"""
    
    def __init__(self, session: ContentSession, manager: ContentManager):
        super().__init__(timeout=600)
        self.session = session
        self.manager = manager
        self.add_buttons()
    
    def add_buttons(self):
        """Add relevant buttons based on content type"""
        if self.session.content_type == ContentType.STATUS:
            self.add_item(AddIncidentButton())
            self.add_item(AddUpdateButton())
            self.add_item(EditIncidentButton())
            self.add_item(RemoveIncidentButton())
        elif self.session.content_type == ContentType.CHANGELOG:
            self.add_item(AddVersionButton())
            self.add_item(EditVersionButton())
            self.add_item(RemoveVersionButton())
        elif self.session.content_type == ContentType.TEAM:
            self.add_item(AddMemberButton())
            self.add_item(EditMemberButton())
            self.add_item(RemoveMemberButton())
        
        self.add_item(SaveButton())
        self.add_item(CancelButton())
        self.add_item(RefreshButton())
    
    async def on_timeout(self):
        """Clean up when view times out"""
        try:
            await self.session.message.delete()
        except:
            pass

class AddIncidentButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Add Incident", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = IncidentModal(self.view.session)
        await interaction.response.send_modal(modal)

class AddUpdateButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📝 Add Update", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which incident date do you want to add an update to? (e.g., 'Apr 30, 2025'):",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            date = reply.content.strip()
            
            incidents = status_manager.parse_status(self.view.session.original_content)
            incident = next((i for i in incidents if i['date'] == date), None)
            
            if incident:
                modal = UpdateModal(self.view.session, incident)
                await reply.reply("Opening update modal...", ephemeral=True)
                await interaction.followup.send_modal(modal)
            else:
                await reply.reply("Incident not found!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class UpdateModal(discord.ui.Modal):
    def __init__(self, session: ContentSession, incident: Dict):
        super().__init__(title=f"Add Update to {incident['date']}")
        
        self.session = session
        self.incident = incident
        
        self.timestamp = discord.ui.TextInput(
            label="Timestamp",
            placeholder="Apr 30, 14:30",
            default=datetime.now().strftime("%b %d, %H:%M"),
            required=True
        )
        
        self.status = discord.ui.TextInput(
            label="Status",
            placeholder="INVESTIGATING/MONITORING/RESOLVED",
            required=True
        )
        
        self.description = discord.ui.TextInput(
            label="Description",
            placeholder="What's happening with this incident?",
            required=True,
            style=discord.TextStyle.paragraph
        )
        
        self.add_item(self.timestamp)
        self.add_item(self.status)
        self.add_item(self.description)
    
    async def on_submit(self, interaction: discord.Interaction):
        incidents = status_manager.parse_status(self.session.original_content)
        
        for i, inc in enumerate(incidents):
            if inc['date'] == self.incident['date']:
                if 'updates' not in inc:
                    inc['updates'] = []
                inc['updates'].append({
                    'timestamp': self.timestamp.value,
                    'status': self.status.value,
                    'description': self.description.value
                })
                break
        
        self.session.original_content = status_manager.format_status(incidents)
        await update_display(self.session.message, self.session)
        await interaction.response.send_message("Update added! Don't forget to save.", ephemeral=True)

class EditIncidentButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✏️ Edit Incident", style=discord.ButtonStyle.primary, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which incident would you like to edit? Reply with the date (e.g., 'Apr 30, 2025'):",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            date = reply.content.strip()
            
            incidents = status_manager.parse_status(self.view.session.original_content)
            incident = next((i for i in incidents if i['date'] == date), None)
            
            if incident:
                modal = IncidentModal(self.view.session, incident)
                await reply.reply("Opening editor...", ephemeral=True)
                await interaction.followup.send_modal(modal)
            else:
                await reply.reply("Incident not found!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class RemoveIncidentButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑️ Remove Incident", style=discord.ButtonStyle.red, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which incident would you like to remove? Reply with the date (e.g., 'Apr 30, 2025'):",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            date = reply.content.strip()
            
            incidents = status_manager.parse_status(self.view.session.original_content)
            incidents = [i for i in incidents if i['date'] != date]
            
            self.view.session.original_content = status_manager.format_status(incidents)
            await update_display(self.view.session.message, self.view.session)
            await reply.reply(f"Removed incident from {date}!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class IncidentModal(discord.ui.Modal):
    def __init__(self, session: ContentSession, existing=None):
        title = "Edit Incident" if existing else "Add Incident"
        super().__init__(title=title)
        
        self.session = session
        self.existing = existing
        
        self.date = discord.ui.TextInput(
            label="Date",
            placeholder="Apr 30, 2025",
            default=existing['date'] if existing else datetime.now().strftime("%b %d, %Y"),
            required=True
        )
        
        self.type = discord.ui.TextInput(
            label="Type (INCIDENT/MAINTENANCE)",
            placeholder="INCIDENT or MAINTENANCE",
            default=existing.get('type', '') if existing else '',
            required=True
        )
        
        self.title_input = discord.ui.TextInput(
            label="Title",
            placeholder="Brief description",
            default=existing.get('title', '') if existing else '',
            required=True
        )
        
        self.severity = discord.ui.TextInput(
            label="Severity",
            placeholder="low/medium/high/maintenance",
            default=existing.get('severity', '') if existing else '',
            required=True
        )
        
        self.components = discord.ui.TextInput(
            label="Components",
            placeholder="Comma-separated",
            default=existing.get('components', '') if existing else '',
            required=True
        )
        
        self.add_item(self.date)
        self.add_item(self.type)
        self.add_item(self.title_input)
        self.add_item(self.severity)
        self.add_item(self.components)
    
    async def on_submit(self, interaction: discord.Interaction):
        incidents = status_manager.parse_status(self.session.original_content)
        
        if self.existing:
            for i, inc in enumerate(incidents):
                if inc['date'] == self.existing['date']:
                    incidents[i] = {
                        'date': self.date.value,
                        'type': self.type.value,
                        'title': self.title_input.value,
                        'severity': self.severity.value,
                        'components': self.components.value,
                        'updates': inc.get('updates', [])
                    }
                    break
        else:
            incidents.insert(0, {
                'date': self.date.value,
                'type': self.type.value,
                'title': self.title_input.value,
                'severity': self.severity.value,
                'components': self.components.value,
                'updates': []
            })
        
        self.session.original_content = status_manager.format_status(incidents)
        await update_display(self.session.message, self.session)
        await interaction.response.send_message("Incident updated! Don't forget to save.", ephemeral=True)

class AddVersionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Add Version", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = VersionModal(self.view.session)
        await interaction.response.send_modal(modal)

class EditVersionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✏️ Edit Version", style=discord.ButtonStyle.primary, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which version would you like to edit? Reply with the version (e.g., '1.0.0'):",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            version = reply.content.strip()
            
            versions = changelog_manager.parse_changelog(self.view.session.original_content)
            ver = next((v for v in versions if v['version'] == version), None)
            
            if ver:
                modal = VersionModal(self.view.session, ver)
                await interaction.followup.send_modal(modal)
            else:
                await reply.reply("Version not found!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class RemoveVersionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑️ Remove Version", style=discord.ButtonStyle.red, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which version would you like to remove? Reply with the version (e.g., '1.0.0'):",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            version = reply.content.strip()
            
            versions = changelog_manager.parse_changelog(self.view.session.original_content)
            versions = [v for v in versions if v['version'] != version]
            
            self.view.session.original_content = changelog_manager.format_changelog(versions)
            await update_display(self.view.session.message, self.view.session)
            await reply.reply(f"Removed version {version}!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class VersionModal(discord.ui.Modal):
    def __init__(self, session: ContentSession, existing=None):
        title = "Edit Version" if existing else "Add Version"
        super().__init__(title=title)
        
        self.session = session
        self.existing = existing
        
        self.version = discord.ui.TextInput(
            label="Version",
            placeholder="1.0.0",
            default=existing['version'] if existing else '',
            required=True
        )
        
        self.date = discord.ui.TextInput(
            label="Date",
            placeholder="2025-04-20",
            default=existing.get('date', '') if existing else datetime.now().strftime("%Y-%m-%d"),
            required=True
        )
        
        self.entries = discord.ui.TextInput(
            label="Changelog Entries",
            placeholder="FEATURE Added new feature\nFIX Fixed bug",
            default='\n'.join([f"{e['type']} {e['description']}" for e in existing.get('entries', [])]) if existing else '',
            required=True,
            style=discord.TextStyle.paragraph
        )
        
        self.add_item(self.version)
        self.add_item(self.date)
        self.add_item(self.entries)
    
    async def on_submit(self, interaction: discord.Interaction):
        versions = changelog_manager.parse_changelog(self.session.original_content)
        
        entries = []
        for line in self.entries.value.split('\n'):
            line = line.strip()
            if line:
                parts = line.split(' ', 1)
                if len(parts) == 2:
                    entries.append({'type': parts[0], 'description': parts[1]})
        
        new_version = {
            'version': self.version.value,
            'date': self.date.value,
            'entries': entries
        }
        
        if self.existing:
            for i, v in enumerate(versions):
                if v['version'] == self.existing['version']:
                    versions[i] = new_version
                    break
        else:
            versions.insert(0, new_version)
        
        self.session.original_content = changelog_manager.format_changelog(versions)
        await update_display(self.session.message, self.session)
        await interaction.response.send_message("Version updated! Don't forget to save.", ephemeral=True)

class AddMemberButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Add Member", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = MemberModal(self.view.session)
        await interaction.response.send_modal(modal)

class EditMemberButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✏️ Edit Member", style=discord.ButtonStyle.primary, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which member would you like to edit? Reply with their ID:",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            member_id = reply.content.strip()
            
            team = team_manager.parse_team(self.view.session.original_content)
            member = next((m for m in team['members'] if m['id'] == member_id), None)
            
            if member:
                modal = MemberModal(self.view.session, member)
                await interaction.followup.send_modal(modal)
            else:
                await reply.reply("Member not found!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class RemoveMemberButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑️ Remove Member", style=discord.ButtonStyle.red, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Which member would you like to remove? Reply with their ID:",
            ephemeral=True
        )
        
        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel
        
        try:
            reply = await bot.wait_for('message', check=check, timeout=30)
            member_id = reply.content.strip()
            
            team = team_manager.parse_team(self.view.session.original_content)
            team['members'] = [m for m in team['members'] if m['id'] != member_id]
            
            self.view.session.original_content = team_manager.format_team(team)
            await update_display(self.view.session.message, self.view.session)
            await reply.reply(f"Removed member with ID: {member_id}!", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("Timed out!", ephemeral=True)

class MemberModal(discord.ui.Modal):
    def __init__(self, session: ContentSession, existing=None):
        title = "Edit Member" if existing else "Add Member"
        super().__init__(title=title)
        
        self.session = session
        self.existing = existing
        
        self.member_id = discord.ui.TextInput(
            label="Member ID",
            placeholder="johan",
            default=existing['id'] if existing else '',
            required=True
        )
        
        self.name = discord.ui.TextInput(
            label="Name",
            placeholder="John Doe",
            default=existing.get('name', '') if existing else '',
            required=True
        )
        
        self.handle = discord.ui.TextInput(
            label="Discord Handle",
            placeholder="username",
            default=existing.get('handle', '') if existing else '',
            required=True
        )
        
        self.roles = discord.ui.TextInput(
            label="Roles (comma-separated)",
            placeholder="Developer, Support",
            default=', '.join(existing.get('roles', [])) if existing else '',
            required=True
        )
        
        self.about = discord.ui.TextInput(
            label="About",
            placeholder="Brief description",
            default=existing.get('about', '') if existing else '',
            required=False,
            style=discord.TextStyle.paragraph
        )
        
        self.add_item(self.member_id)
        self.add_item(self.name)
        self.add_item(self.handle)
        self.add_item(self.roles)
        self.add_item(self.about)
    
    async def on_submit(self, interaction: discord.Interaction):
        team = team_manager.parse_team(self.session.original_content)
        
        member_data = {
            'id': self.member_id.value,
            'name': self.name.value,
            'handle': self.handle.value,
            'roles': [r.strip() for r in self.roles.value.split(',')],
            'status': 'Online',
            'joinedYear': str(datetime.now().year),
            'avatarUrl': '',
            'about': self.about.value,
            'timeline': [],
            'skills': [],
            'tags': [],
            'stats': [],
            'dataFields': []
        }
        
        if self.existing:
            member_data.update({k: v for k, v in self.existing.items() 
                              if k not in member_data})
            
            for i, m in enumerate(team['members']):
                if m['id'] == self.existing['id']:
                    team['members'][i] = member_data
                    break
        else:
            team['members'].append(member_data)
        
        self.session.original_content = team_manager.format_team(team)
        await update_display(self.session.message, self.session)
        await interaction.response.send_message("Member updated! Don't forget to save.", ephemeral=True)

class SaveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="💾 Save to GitHub", style=discord.ButtonStyle.success, row=1)
    
    async def callback(self, interaction: discord.Interaction):
        session = self.view.session
        
        await interaction.response.defer(ephemeral=True)
        
        if session.content_type == ContentType.STATUS:
            success = await status_manager.commit_changes(
                session.file_path,
                session.original_content,
                "Update status",
                str(session.author)
            )
        elif session.content_type == ContentType.CHANGELOG:
            success = await changelog_manager.commit_changes(
                session.file_path,
                session.original_content,
                "Update changelog",
                str(session.author)
            )
        elif session.content_type == ContentType.TEAM:
            success = await team_manager.commit_changes(
                session.file_path,
                session.original_content,
                "Update team",
                str(session.author)
            )
        
        if success:
            await interaction.followup.send("✅ Changes saved to GitHub!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to save changes! Check if GitHub token has repo permissions.", ephemeral=True)

class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    
    async def callback(self, interaction: discord.Interaction):
        await self.view.session.message.delete()
        await interaction.response.send_message("Session cancelled.", ephemeral=True)

class RefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔄 Refresh Display", style=discord.ButtonStyle.secondary, row=1)
    
    async def callback(self, interaction: discord.Interaction):
        await update_display(self.view.session.message, self.view.session)
        await interaction.response.send_message("Display refreshed!", ephemeral=True)

async def update_display(message: discord.Message, session: ContentSession):
    """Update the display message with current content"""
    embed = discord.Embed(
        title=f"📝 Editing {session.content_type.value.title()}",
        description="Use the buttons below to make changes. Click Save when done.",
        color=discord.Color.blue()
    )
    
    content = session.original_content[:1000]
    if len(session.original_content) > 1000:
        content += "\n... (truncated)"
    
    embed.add_field(name="Current Content", value=f"```{content}```", inline=False)
    embed.set_footer(text=f"Editor: {session.author.name}")
    
    await message.edit(embed=embed, view=ContentView(session, get_manager(session.content_type)))

def get_manager(content_type: ContentType) -> ContentManager:
    """Get the appropriate manager for content type"""
    if content_type == ContentType.STATUS:
        return status_manager
    elif content_type == ContentType.CHANGELOG:
        return changelog_manager
    elif content_type == ContentType.TEAM:
        return team_manager

# Slash Commands
@bot.tree.command(name="edit", description="Start editing website content")
@app_commands.describe(content_type="Type of content to edit")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status", value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team", value="team")
])
async def edit_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    """Start an editing session for website content"""
    
    allowed_roles = ["Developer", "Admin", "Content Editor"]
    if not any(role.name in allowed_roles for role in interaction.user.roles):
        await interaction.response.send_message(
            "You don't have permission to edit content!",
            ephemeral=True
        )
        return
    
    content_type_enum = ContentType(content_type.value)
    
    if content_type_enum == ContentType.STATUS:
        file_path = STATUS_FILE
        manager = status_manager
    elif content_type_enum == ContentType.CHANGELOG:
        file_path = CHANGELOG_FILE
        manager = changelog_manager
    elif content_type_enum == ContentType.TEAM:
        file_path = TEAM_FILE
        manager = team_manager
    
    await interaction.response.defer()
    
    content = await manager.get_file_content(file_path)
    
    session = ContentSession(
        content_type=content_type_enum,
        data=None,
        original_content=content,
        file_path=file_path,
        author=interaction.user,
        message=None
    )
    
    embed = discord.Embed(
        title=f"📝 Editing {content_type.name}",
        description="Use the buttons below to make changes. Click Save when done.",
        color=discord.Color.blue()
    )
    
    display_content = content[:1000]
    if len(content) > 1000:
        display_content += "\n... (truncated)"
    
    embed.add_field(name="Current Content", value=f"```{display_content}```", inline=False)
    embed.set_footer(text=f"Editor: {interaction.user.name}")
    
    view = ContentView(session, manager)
    await interaction.followup.send(embed=embed, view=view)
    
    message = await interaction.original_response()
    session.message = message

@bot.tree.command(name="view", description="View current website content")
@app_commands.describe(content_type="Type of content to view")
@app_commands.choices(content_type=[
    app_commands.Choice(name="Status", value="status"),
    app_commands.Choice(name="Changelog", value="changelog"),
    app_commands.Choice(name="Team", value="team")
])
async def view_content(interaction: discord.Interaction, content_type: app_commands.Choice[str]):
    """View current website content without editing"""
    
    await interaction.response.defer()
    
    if content_type.value == "status":
        file_path = STATUS_FILE
        manager = status_manager
    elif content_type.value == "changelog":
        file_path = CHANGELOG_FILE
        manager = changelog_manager
    else:
        file_path = TEAM_FILE
        manager = team_manager
    
    content = await manager.get_file_content(file_path)
    
    if len(content) > 1900:
        content = content[:1900] + "\n... (truncated)"
    
    if not content:
        content = "No content found or error fetching file."
    
    embed = discord.Embed(
        title=f"📄 Current {content_type.name}",
        description=f"```{content}```",
        color=discord.Color.green()
    )
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help_editor", description="Show help for the content editor")
async def help_editor(interaction: discord.Interaction):
    """Show help information for the content editor"""
    
    embed = discord.Embed(
        title="📚 Content Editor Help",
        description="Here's how to use the GitHub content editor:",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="Getting Started",
        value="Use `/edit <type>` to start editing content.\nUse `/view <type>` to view current content.",
        inline=False
    )
    
    embed.add_field(
        name="Editing Status",
        value="• Add/Edit/Remove incidents\n• Add updates to existing incidents\n• Each incident has date, type, severity, components",
        inline=False
    )
    
    embed.add_field(
        name="Editing Changelog",
        value="• Add/Edit/Remove versions\n• Each version has entries with type\n• Entries format: TYPE Description",
        inline=False
    )
    
    embed.add_field(
        name="Editing Team",
        value="• Add/Edit/Remove team members\n• Each member has ID, name, handle, roles, about",
        inline=False
    )
    
    embed.add_field(
        name="Important",
        value="• Click 💾 Save to commit changes to GitHub\n• Author is tracked in commit message\n• Set up GITHUB_TOKEN and GITHUB_REPO env vars",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

# Keep-alive web server
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is online and running!"

def run():
    port = int(os.environ.get('PORT', 10000))  # Render's default port
    print(f"Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == "__main__":
    print("Starting keep-alive server...")
    keep_alive()
    print("Starting Discord bot...")
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
