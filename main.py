# main.py
import discord
from discord.ext import commands
import json
import os
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
import aiohttp
from aiohttp import web
from github import Github, InputGitTreeElement
import base64
from dataclasses import dataclass, asdict
from enum import Enum
import re
import keep_alive  # We'll create this separately

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPO = os.getenv('GITHUB_REPO')  # Format: "username/repo"
GUILD_ID = int(os.getenv('GUILD_ID', '0'))  # Optional: restrict to specific server

# File paths in your repo
STATUS_FILE = "status.txt"
CHANGELOG_FILE = "changelog.txt"
TEAM_FILE = "teams.json"

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# GitHub client
github_client = Github(GITHUB_TOKEN)
repo = github_client.get_repo(GITHUB_REPO)

class ContentManager:
    """Manages all content types and their file operations"""
    
    def __init__(self):
        self.active_sessions = {}  # channel_id: ContentSession
    
    async def get_file_content(self, file_path: str) -> str:
        """Get file content from GitHub"""
        try:
            contents = repo.get_contents(file_path)
            return base64.b64decode(contents.content).decode('utf-8')
        except:
            return ""
    
    async def commit_changes(self, file_path: str, content: str, commit_message: str, author: str):
        """Commit changes to GitHub"""
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
                
            # Check if this is a date line
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
            
            # Check for incident/maintenance header
            incident_match = re.match(r'^(INCIDENT|MAINTENANCE):\s+(.+)$', line)
            if incident_match and current_incident:
                current_incident['type'] = incident_match.group(1)
                current_incident['title'] = incident_match.group(2)
                i += 1
                continue
            
            # Check for severity
            severity_match = re.match(r'^SEVERITY:\s+(.+)$', line)
            if severity_match and current_incident:
                current_incident['severity'] = severity_match.group(1)
                i += 1
                continue
            
            # Check for components
            components_match = re.match(r'^COMPONENTS:\s+(.+)$', line)
            if components_match and current_incident:
                current_incident['components'] = components_match.group(1)
                i += 1
                continue
            
            # Check for updates
            update_match = re.match(r'^(\w{3}\s+\d{1,2},\s+\d{2}:\d{2})\s+-\s+(\w+)\s+-\s+(.+)$', line)
            if update_match and current_incident:
                current_incident['updates'].append({
                    'timestamp': update_match.group(1),
                    'status': update_match.group(2),
                    'description': update_match.group(3)
                })
                i += 1
                continue
            
            # Check for "No incidents reported"
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
        super().__init__(timeout=300)  # 5 minute timeout
        self.session = session
        self.manager = manager
        self.add_buttons()
    
    def add_buttons(self):
        """Add relevant buttons based on content type"""
        if self.session.content_type == ContentType.STATUS:
            self.add_item(AddIncidentButton())
            self.add_item(EditIncidentButton())
            self.add_item(RemoveIncidentButton())
        elif self.session.content_type == ContentType.CHANGELOG:
            self.add_item(AddVersionButton())
            self.add_item(EditVersionButton())
        elif self.session.content_type == ContentType.TEAM:
            self.add_item(AddMemberButton())
            self.add_item(EditMemberButton())
            self.add_item(RemoveMemberButton())
        
        self.add_item(SaveButton())
        self.add_item(CancelButton())
        self.add_item(RefreshButton())

class AddIncidentButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Add Incident", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = IncidentModal(self.view.session)
        await interaction.response.send_modal(modal)

class EditIncidentButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Edit Incident", style=discord.ButtonStyle.primary, row=0)
    
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
            
            # Find the incident
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
        super().__init__(label="Remove Incident", style=discord.ButtonStyle.red, row=0)
    
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
            placeholder="Brief description of the incident",
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
            placeholder="Comma-separated components",
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
            # Update existing incident
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
            # Add new incident
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
        super().__init__(label="Add Version", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = VersionModal(self.view.session)
        await interaction.response.send_modal(modal)

class EditVersionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Edit Version", style=discord.ButtonStyle.primary, row=0)
    
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
            placeholder="FEATURE Added new feature\nFIX Fixed bug\nOne per line: TYPE Description",
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
        super().__init__(label="Add Member", style=discord.ButtonStyle.green, row=0)
    
    async def callback(self, interaction: discord.Interaction):
        modal = MemberModal(self.view.session)
        await interaction.response.send_modal(modal)

class EditMemberButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Edit Member", style=discord.ButtonStyle.primary, row=0)
    
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
        super().__init__(label="Remove Member", style=discord.ButtonStyle.red, row=0)
    
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
            placeholder="Brief description about the member",
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
            # Preserve existing data not in the form
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
            await interaction.response.send_message("✅ Changes saved to GitHub!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Failed to save changes!", ephemeral=True)

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
    
    # Truncate content for display
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

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} guilds')
    
    # Clean up any old messages from previous sessions
    # This happens automatically since messages aren't persisted

@bot.slash_command(name="edit", description="Start editing website content")
async def edit_content(
    ctx: discord.ApplicationContext,
    content_type: discord.Option(str, "Type of content to edit", choices=[
        discord.OptionChoice(name="Status", value="status"),
        discord.OptionChoice(name="Changelog", value="changelog"),
        discord.OptionChoice(name="Team", value="team")
    ])
):
    """Start an editing session for website content"""
    
    # Check permissions (adjust role names as needed)
    allowed_roles = ["Developer", "Admin", "Content Editor"]
    if not any(role.name in allowed_roles for role in ctx.author.roles):
        await ctx.respond("You don't have permission to edit content!", ephemeral=True)
        return
    
    # Get current content from GitHub
    content_type_enum = ContentType(content_type)
    
    if content_type_enum == ContentType.STATUS:
        file_path = STATUS_FILE
        manager = status_manager
    elif content_type_enum == ContentType.CHANGELOG:
        file_path = CHANGELOG_FILE
        manager = changelog_manager
    elif content_type_enum == ContentType.TEAM:
        file_path = TEAM_FILE
        manager = team_manager
    
    # Fetch current content
    content = await manager.get_file_content(file_path)
    
    # Create session
    session = ContentSession(
        content_type=content_type_enum,
        data=None,
        original_content=content,
        file_path=file_path,
        author=ctx.author,
        message=None
    )
    
    # Send interactive message
    embed = discord.Embed(
        title=f"📝 Editing {content_type.title()}",
        description="Use the buttons below to make changes. Click Save when done.",
        color=discord.Color.blue()
    )
    
    # Truncate content for display
    display_content = content[:1000]
    if len(content) > 1000:
        display_content += "\n... (truncated)"
    
    embed.add_field(name="Current Content", value=f"```{display_content}```", inline=False)
    embed.set_footer(text=f"Editor: {ctx.author.name}")
    
    view = ContentView(session, manager)
    message = await ctx.respond(embed=embed, view=view)
    
    # Store message reference in session
    if hasattr(message, 'message'):
        session.message = message.message
    else:
        session.message = message

@bot.slash_command(name="view", description="View current website content")
async def view_content(
    ctx: discord.ApplicationContext,
    content_type: discord.Option(str, "Type of content to view", choices=[
        discord.OptionChoice(name="Status", value="status"),
        discord.OptionChoice(name="Changelog", value="changelog"),
        discord.OptionChoice(name="Team", value="team")
    ])
):
    """View current website content without editing"""
    
    if content_type == "status":
        file_path = STATUS_FILE
        manager = status_manager
    elif content_type == "changelog":
        file_path = CHANGELOG_FILE
        manager = changelog_manager
    else:
        file_path = TEAM_FILE
        manager = team_manager
    
    content = await manager.get_file_content(file_path)
    
    # Truncate if too long
    if len(content) > 1900:
        content = content[:1900] + "\n... (truncated)"
    
    embed = discord.Embed(
        title=f"📄 Current {content_type.title()}",
        description=f"```{content}```",
        color=discord.Color.green()
    )
    
    await ctx.respond(embed=embed)

@bot.slash_command(name="help_editor", description="Show help for the content editor")
async def help_editor(ctx: discord.ApplicationContext):
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
        value="• Add/Edit/Remove incidents\n• Each incident has date, type, severity, components\n• Add updates with timestamps",
        inline=False
    )
    
    embed.add_field(
        name="Editing Changelog",
        value="• Add/Edit versions\n• Each version has entries with type (FEATURE/FIX/UX/etc)\n• Entries format: TYPE Description",
        inline=False
    )
    
    embed.add_field(
        name="Editing Team",
        value="• Add/Edit/Remove team members\n• Each member has ID, name, handle, roles, about\n• Timeline and skills can be added later",
        inline=False
    )
    
    embed.add_field(
        name="Saving",
        value="• Click 💾 Save to commit changes to GitHub\n• Changes are immediate on the website\n• Author is tracked in commit message",
        inline=False
    )
    
    embed.set_footer(text="Only authorized roles can edit content")
    
    await ctx.respond(embed=embed)

# Keep-alive web server for Render
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
