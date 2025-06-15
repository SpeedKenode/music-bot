import discord
from discord.ext import commands, tasks
import yt_dlp as youtube_dl
import asyncio
from collections import deque
import os
from dotenv import load_dotenv
import logging

load_dotenv()

PREFIX = os.getenv('PREFIX')
TOKEN = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

class Song:
    def __init__(self, source, data):
        self.source = source
        self.title = data.get('title', "Unknown Title")
        self.url = data.get('url', "")
        self.thumbnail = data.get('thumbnail', None)

class GuildQueue:
    def __init__(self):
        self.queue = deque()
        self.now_playing = None
        self.loop = False
        self.voice_channel = None
        self._history = deque(maxlen=10)
    
    def __len__(self):
        return len(self.queue)
    
    def add(self, song, voice_channel=None):
        self.queue.append(song)
        if voice_channel:
            self.voice_channel = voice_channel
    
    def get_next(self):
        if len(self.queue) == 0:
            return None
        
        if self.now_playing:
            self._history.append(self.now_playing)
        
        self.now_playing = self.queue.popleft()
        return self.now_playing
    
    def clear(self):
        self.queue.clear()

queues = {}

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = GuildQueue()
    return queues[guild_id]

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=1.0):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown title')
        self.url = data.get('url', '')
        self.uploader = data.get('uploader', 'Unknown artist')
        self.thumbnail = data.get('thumbnail', '')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'noplaylist': True,
            'extract_flat': False,
            'ignore_no_formats_error': True,
            'allow_multiple_video_streams': True,
            'allow_multiple_audio_streams': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'skip': ['dash', 'hls']
                }
            },
            'socket_timeout': 3,
            'source_address': '0.0.0.0',
            'extract_info': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'geo_bypass': True,
        }
        
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 -nostdin',
            'options': '-vn -b:a 128k -af "volume=1.0, dynaudnorm=f=150:g=15, aresample=48000:async=1, atempo=1.0"'
        }
        try:
            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                for attempt in range(3):
                    try:
                        data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=not stream))
                        if not data:
                            continue
                            
                        if 'entries' in data:
                            data = data['entries'][0]
                        if 'url' not in data:
                            ydl_opts['format'] = 'worstaudio/worst'
                            continue
                            
                        filename = data['url'] if stream else ydl.prepare_filename(data)
                        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
                    except Exception as e:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1)
                        
        except Exception as e:
            print(f"[YTDL Error] Failed after 3 attempts: {e}")
            return None

async def process_playlist(ctx, url, voice_client, queue):
    msg = await ctx.send("⏳ Loading playlist...")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'extract_flat': 'in_playlist',
        'playlistend': 50,
        'socket_timeout': 3
    }

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        data = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        
        if not data or 'entries' not in data:
            return await msg.edit(content="❌ Couldn't load playlist")
        
        added = 0
        for entry in data['entries']:
            if not entry:
                continue
                
            song_data = {
                'title': entry.get('title', 'Unknown'),
                'url': entry.get('url', url),
            }
            queue.add(song_data)
            added += 1
            
            if added == 1 and not voice_client.is_playing():
                await process_next_song(ctx, voice_client, queue)
        
        await msg.edit(content=f"🎵 Added {added} songs from playlist to queue")

async def process_single(ctx, query, voice_client, queue):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'socket_timeout': 3
    }

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        data = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
        
        if not data:
            return await ctx.send("❌ Couldn't find that song")
            
        if 'entries' in data:
            data = data['entries'][0]
            
        song_data = {
            'title': data.get('title', 'Unknown'),
            'url': data['url'],
        }
        
        queue.add(song_data)
        await ctx.send(f"🎵 Added to queue: **{song_data['title']}**")
        
        if not voice_client.is_playing():
            await process_next_song(ctx, voice_client, queue)

async def process_next_song(ctx, voice_client=None, queue=None):
    voice_client = voice_client or ctx.voice_client
    if not voice_client:
        return

    queue = queue or get_queue(ctx.guild.id)
    next_song = queue.get_next()
    
    if not next_song:
        return await ctx.send("🎶 Queue is empty!")
    
    try:
        source = await YTDLSource.from_url(next_song['url'], loop=bot.loop)
        if not source:
            await ctx.send("❌ Couldn't process this song (skipping)")
            return await process_next_song(ctx, voice_client, queue)
            
        def after_playing(error):
            if error:
                print(f'Player error: {error}')
            asyncio.run_coroutine_threadsafe(
                process_next_song(ctx, voice_client, queue),
                bot.loop
            )
        
        voice_client.play(source, after=after_playing)
        await ctx.send(f"🎶 Now playing: **{next_song.get('title', 'Unknown')}**")

    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)[:200]} (skipping)")
        await asyncio.sleep(1)
        await process_next_song(ctx, voice_client, queue)

@bot.command(name='join', aliases=['connect', 'j'], help="Make the bot join your voice channel")
async def join(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("🚫 You need to be in a voice channel first!")
    
    voice_client = ctx.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel == ctx.author.voice.channel:
            return await ctx.send("🤖 I'm already in your voice channel!")
        
        await voice_client.move_to(ctx.author.voice.channel)
        return await ctx.send(f"🚚 Moved to {ctx.author.voice.channel.name}")
    
    voice_client = await ctx.author.voice.channel.connect()
    await ctx.send(f"🎶 Joined {ctx.author.voice.channel.name}")

    get_queue(ctx.guild.id)

@bot.command(name='leave', help="Leave the voice channel")
async def leave(ctx):
    voice_client = ctx.voice_client

    if voice_client:
        await voice_client.disconnect()
        get_queue(ctx.guild.id).clear()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel!")

@bot.command(name='play', aliases=['p'], help="Play songs or playlists with minimal delay")
async def play(ctx, *, query):
    if not ctx.author.voice:
        return await ctx.send("🔇 You need to be in a voice channel!")

    voice_client = ctx.voice_client or await ctx.author.voice.channel.connect()
    queue = get_queue(ctx.guild.id)

    async with ctx.typing():
        try:
            is_playlist = any(p in query for p in ['list=', 'playlist', '&index='])
            if is_playlist:
                await process_playlist(ctx, query, voice_client, queue)
            else:
                await process_single(ctx, query, voice_client, queue)
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")
            print(f"Play error: {e}")

@bot.command(name='pause', help="Pause the current song")
async def pause(ctx):
    voice_client = ctx.voice_client

    if not voice_client or not voice_client.is_playing():
        return await ctx.send("⚠️ Nothing is currently playing!")
    
    if voice_client.is_paused():
        return await ctx.send("⚠️ Already paused!")
    
    voice_client.pause()
    await ctx.send("⏸️ Paused playback")

@bot.command(name='resume', aliases=['continue'], help="Resume the paused song")
async def resume(ctx):
    voice_client = ctx.voice_client

    if not voice_client or not voice_client.is_connected():
        return await ctx.send("⚠️ Not connected to a voice channel!")
    
    if not voice_client.is_paused():
        return await ctx.send("⚠️ Playback is not paused!")
    
    voice_client.resume()
    await ctx.send("▶️ Resumed playback")

@bot.command(name='previous', aliases=['back', 'last'], help="Replay the last played song")
async def previous(ctx):
    voice_client = ctx.voice_client
    queue = get_queue(ctx.guild.id)
    
    if not voice_client or not voice_client.is_connected():
        return await ctx.send("❌ Not connected to voice channel!")
    
    if len(queue._history) == 0:
        return await ctx.send("❌ No history available!")
    
    last_song = queue._history[-1]
    queue.add(last_song, ctx.author.voice.channel)
    
    if not voice_client.is_playing() and not voice_client.is_paused():
        await process_next_song(ctx)
    
    await ctx.send(f"🔁 Replaying: **{last_song.get('title', 'previous track')}**")

@bot.command(name='skip', help="Skip the current song")
async def skip(ctx):
    voice_client = ctx.voice_client

    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("⏭ Skipped current song.")
    else:
        await ctx.send("Nothing is playing!")

@bot.command(name='queue', aliases=['q', 'playlist'], help="Display the current queue with proper formatting and length checks")
async def show_queue(ctx):
    queue = get_queue(ctx.guild.id)

    if not queue.now_playing and len(queue.queue) == 0:
        return await ctx.send("🎵 The queue is currently empty!")
    
    queue_list = list(queue.queue)
    embed = discord.Embed(title="🎶 Music Queue", color=0x1DB954)

    if queue.now_playing:
        title = str(queue.now_playing.get('title', 'Unknown'))[:256]
        
        current_playing_value = (f"{title}\n")[:1024]
        
        embed.add_field(
            name="▶️ Now Playing",
            value=current_playing_value,
            inline=False
        )

    if len(queue_list) > 0:
        songs_list = []
        total_chars = 0
        max_chars = 4000

        for i, song in enumerate(queue_list[:15], 1):
            title = str(song.get('title', 'Unknown'))[:64]
            song_entry = (f"`{i}.` {title}\n")

            if total_chars + len(song_entry) > max_chars:
                remaining = len(queue_list) - i
                songs_list.append(f"\n...and {remaining} more songs")
                break
                
            songs_list.append(song_entry)
            total_chars += len(song_entry)
        
        up_next_value = "\n".join(songs_list)[:4000]
        
        embed.add_field(
            name=f"🔜 Up Next ({len(queue_list)} total)",
            value=up_next_value or "No songs in queue",
            inline=False
        )
    
    await ctx.send(embed=embed)

@bot.command(name='nowplaying', aliases=['np', 'current'], help="Show detailed information about the currently playing song")
async def now_playing(ctx):
    voice_client = ctx.voice_client
    queue = get_queue(ctx.guild.id)

    if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
        return await ctx.send("❌ Nothing is currently playing!")
    
    if not queue.now_playing:
        return await ctx.send("❌ No track information available!")
    
    song = queue.now_playing
    info=song.get('title', 'Unknown Title')
    
    await ctx.send(f"🎵 Now Playing: **{info}**")

@tasks.loop(minutes=10)
async def auto_disconnect():
    for vc in bot.voice_clients:
        if not vc.is_playing():
            await vc.disconnect()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{PREFIX}help"))
    auto_disconnect.start()

bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)