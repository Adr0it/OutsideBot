import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, time, timedelta
import pytz
import sqlite3
import requests
import asyncio
import schedule
from geopy.geocoders import Nominatim

# Fill in fields
TOKEN = ''
server_timezone = ''

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

# Event triggered when bot connects to server
@bot.event
async def on_ready():
    print(f'OutsideBot has connected...')
    await bot.tree.sync()
    await daily_updates()

    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

# !setup "Ukrainian Village, Chicago" America/Chicago 10 0 6 1
# Setup command for bot (updates database.db)
@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def setup(ctx: commands.Context, location: str, timezone: str, update_hour: int, update_min: int, forecast_duration: int, min_members: int):
    try:
        # Convert "update at" time to UTC format
        update_at_local_time = datetime.combine(datetime.now().date(), time(update_hour, update_min, 0))
        local_tz = pytz.timezone(timezone)

        localized_dt = local_tz.localize(update_at_local_time)
        utc_dt = localized_dt.astimezone(pytz.utc)
        update_at_utc_time = utc_dt.strftime('%H:%M:%S')

        # Add to database
        connection = sqlite3.connect('database.db')
        cursor = connection.cursor()

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS update_info(
            server_id INTEGER,
            channel_id INTEGER,
            location TEXT,
            timezone TEXT,
            update_time TEXT,
            forecast_duration INT,
            min_members INT,
            UNIQUE (server_id, channel_id)
        )
        ''')

        cursor.execute(f'''
        INSERT OR REPLACE INTO update_info VALUES
        ({ctx.guild.id}, {ctx.channel.id}, '{location}', '{timezone}', '{str(update_at_utc_time)}', '{forecast_duration}', '{min_members}')
        ''')

        cursor.execute('''
        SELECT * FROM update_info
        WHERE server_id = 1250448472654614558
        ''')

        results = cursor.fetchall()

        for row in results:
            print(row)

        connection.commit()
        connection.close()

        await ctx.send(f'OutsideBot setup finished! Daily update time (UTC) is: {str(update_at_utc_time)}')

        # Run Daily Updates (because of new entry)
        await daily_updates()



    except Exception as e:
        await ctx.send(f'OutsideBot setup failed! Try again. \nError: {str(e)}')

# Delete server-side command for bot (updates database.db)
async def server_delete(server_id, channel_id):
    try:
        connection = sqlite3.connect('database.db')
        cursor = connection.cursor()

        cursor.execute(f'''
            DELETE FROM update_info 
            WHERE server_id = {server_id} AND channel_id = {channel_id}
        ''')
        connection.commit()

        cursor.close()
        connection.close()

        await print(f'OutsideBot has been successfully removed from the database!')
    except Exception as e:
        await print(f'Deletion failed! Try again. \nError: {str(e)}')

# Delete command for bot (updates database.db)
@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def delete(ctx: commands.Context):
    try:
        connection = sqlite3.connect('database.db')
        cursor = connection.cursor()

        server_id = ctx.guild.id
        channel_id = ctx.channel.id

        cursor.execute(f'''
            DELETE FROM update_info 
            WHERE server_id = {server_id} AND channel_id = {channel_id}
        ''')
        connection.commit()

        cursor.close()
        connection.close()

        await ctx.send(f'OutsideBot has been successfully removed from the database!')
    except Exception as e:
        await ctx.send(f'Deletion failed! Try again. \nError: {str(e)}')

async def daily_updates():
    try:
        connection = sqlite3.connect('database.db')
        cursor = connection.cursor()

        cursor.execute('SELECT * FROM update_info')
        rows = cursor.fetchall()

        for r in rows:
            server_id = r[0]
            channel_id = r[1]
            timezone = r[3]
            update_time = r[4]

            hr, min, _ = map(int, update_time.split(':'))
            tz = pytz.timezone(timezone)
            server_tz = pytz.timezone(server_timezone)

            utc_start_time = datetime.now(pytz.utc).replace(hour=hr, minute=min)
            server_start_time = utc_start_time.astimezone(server_tz).strftime('%H:%M')

            # Daily Poll Check
            schedule.every().day.at(server_start_time).do(lambda: asyncio.create_task(server_poll(server_id, channel_id)))

            # Daily Weather Update
            schedule.every().day.at(server_start_time).do(lambda: asyncio.create_task(toggle_reaction(server_id, channel_id)))
        connection.close()
    
    except Exception as e:
        print(f'Something went wrong checking while doing daily updates. \nError: {str(e)}')

# Server-side polling
async def server_poll(server_id, channel_id):
    try:
        # Fetch Relevant Information
        row = get_row(server_id, channel_id)
        print(f'Row: {row}')
        min_members = row[6]
        timezone = row[3]

        # Check if weekly poll exists
        channel = bot.get_channel(channel_id)
        poll = None
        async for m in channel.history(limit=100):
            if m.author == bot.user and m.embeds: 
                poll = m
                break
    
        if poll:
            tz = pytz.timezone(timezone)
            previous_sunday = get_previous_sunday(tz) 
            message_time = poll.created_at.replace(tzinfo=pytz.utc).astimezone(tz)
            
            if previous_sunday <= message_time:
                return
            
        # Create poll (embed)
        prev_sunday = get_previous_sunday(pytz.timezone(timezone))
        upcoming_saturday = prev_sunday + timedelta(days=6)
        embed = discord.Embed(title=f'Weekly Meeting Poll ({prev_sunday.strftime("%m/%d/%Y")} - {upcoming_saturday.strftime("%m/%d/%Y")})',
                        description=f'React to this meeting poll to set your availability for the week. \n\nWhen {min_members} or more participants are free on a day, an automatic arrangement will be made.',
                        colour=0x00ff1e)

        poll = await channel.send(embed=embed)
        for i in range(1, 8): 
            emoji = f'{i}\N{variation selector-16}\N{combining enclosing keycap}'
            await poll.add_reaction(emoji)
    except Exception as e: 
        print(f'Something went wrong when polling: \nError: {str(e)}')

# !poll
@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def poll(ctx: commands.Context):
    try:
        # Fetch Relevant Information
        row = get_row(ctx.guild.id, ctx.channel.id)
        print(f'Row: {row}')
        min_members = row[6]
        timezone = row[3]

        # Check if weekly poll exists
        poll = None
        async for m in ctx.channel.history(limit=100):
            if m.author == bot.user and m.embeds: 
                poll = m
                break
    
        if poll:
            tz = pytz.timezone(timezone)
            previous_sunday = get_previous_sunday(tz) 
            message_time = poll.created_at.replace(tzinfo=pytz.utc).astimezone(tz)
            
            if previous_sunday <= message_time:
                return
            
        # Create poll (embed)
        prev_sunday = get_previous_sunday(pytz.timezone(timezone))
        upcoming_saturday = prev_sunday + timedelta(days=6)
        embed = discord.Embed(title=f'Weekly Meeting Poll ({prev_sunday.strftime("%m/%d/%Y")} - {upcoming_saturday.strftime("%m/%d/%Y")})',
                        description=f'React to this meeting poll to set your availability for the week. \n\nWhen {min_members} or more participants are free on a day, an automatic arrangement will be made.',
                        colour=0x00ff1e)

        poll = await ctx.send(embed=embed)
        for i in range(1, 8): 
            emoji = f'{i}\N{variation selector-16}\N{combining enclosing keycap}'
            await poll.add_reaction(emoji)
    
    except Exception as e:
        await ctx.send(f'Something went wrong when polling: \nError: {str(e)}')

@bot.event
async def on_raw_reaction_add(payload):
    user = payload.member
    m = await bot.get_channel(payload.channel_id).fetch_message(payload.message_id)
    
    if m.author != bot.user or not m.embeds: return

    # Make sure this is the latest message sent by bot
    latest_msg = False
    async for o in m.channel.history(limit=100):
        if o.author == bot.user and o.embeds: 
            if o.id == m.id: latest_msg = True
            break
    if not latest_msg: return

    # Fetch Relevant Information
    row = get_row(m.guild.id, m.channel.id)
    min_members = row[6]
    timezone = row[3]

    # Tally selections
    poll_emojis = [f'{i}\N{variation selector-16}\N{combining enclosing keycap}' for i in range(1, 8)]
    days_selection = [0]*7

    for r in m.reactions:
        if r.emoji in poll_emojis:
            emoji_index = poll_emojis.index(r.emoji)
            if (r.count-1) >= min_members:
                days_selection[emoji_index] = 1

    # Queue a weather report (by retrieving info from database)
    out = await get_weather(m.guild.id, m.channel.id, days_selection)

    # Edit latest message
    tz = pytz.timezone(timezone)
    prev_sunday = get_previous_sunday(tz)
    upcoming_saturday = prev_sunday + timedelta(days=6)
    new_embed = discord.Embed(title=f'Weekly Meeting Poll ({prev_sunday.strftime("%m/%d/%Y")} - {upcoming_saturday.strftime("%m/%d/%Y")})',
                      description=f'React to this meeting poll to set your availability for the week.\n\nWhen {min_members} or more participants are free on a day, an automatic arrangement will be made.\n\n{out}' ,
                      colour=0x00ff1e)
    await m.edit(embed=new_embed)

# same functionality as adding
@bot.event
async def on_raw_reaction_remove(payload):
    user = payload.member
    m = await bot.get_channel(payload.channel_id).fetch_message(payload.message_id)
    
    if m.author != bot.user or not m.embeds: return

    # Make sure this is the latest message sent by bot
    latest_msg = False
    async for o in m.channel.history(limit=100):
        if o.author == bot.user and o.embeds: 
            if o.id == m.id: latest_msg = True
            break
    if not latest_msg: return

    # Fetch Relevant Information
    row = get_row(m.guild.id, m.channel.id)
    min_members = row[6]
    timezone = row[3]

    # Tally selections
    poll_emojis = [f'{i}\N{variation selector-16}\N{combining enclosing keycap}' for i in range(1, 8)]
    days_selection = [0]*7

    for r in m.reactions:
        if r.emoji in poll_emojis:
            emoji_index = poll_emojis.index(r.emoji)
            if (r.count-1) >= min_members:
                days_selection[emoji_index] = 1

    # Queue a weather report (by retrieving info from database)
    out = await get_weather(m.guild.id, m.channel.id, days_selection)

    # Edit latest message
    tz = pytz.timezone(timezone)
    prev_sunday = get_previous_sunday(tz)
    upcoming_saturday = prev_sunday + timedelta(days=6)
    new_embed = discord.Embed(title=f'Weekly Meeting Poll ({prev_sunday.strftime("%m/%d/%Y")} - {upcoming_saturday.strftime("%m/%d/%Y")})',
                      description=f'React to this meeting poll to set your availability for the week.\n\nWhen {min_members} or more participants are free on a day, an automatic arrangement will be made.\n\n{out}' ,
                      colour=0x00ff1e)
    await m.edit(embed=new_embed)

async def get_weather(server_id, channel_id, days_selection):
    try:
        # Fetch related information of ctx
        row = get_row(server_id, channel_id)
        location = row[2]
        timezone = row[3]
        update_time = row[4]
        forecast_duration = row[5]

        # Formatting Data
        tz = pytz.timezone(timezone)

        hr, min, _ = map(int, update_time.split(':'))
        utc_start_time = datetime.now(pytz.utc).replace(hour=hr, minute=min)
        forecast_hr_start = int(utc_start_time.astimezone(tz).strftime('%H'))

        # Valid Dates
        dates = []
        for i in range(len(days_selection)):
            if days_selection[i]:
                dt = get_previous_sunday(tz) + timedelta(days=i)
                dates.append(dt.strftime('%Y/%m/%d'))

        # Get Location Coordinates
        geolocator = Nominatim(user_agent='Chrome')
        loc = geolocator.geocode(location)

        latitude = loc.latitude
        longitude = loc.longitude

        # Get the forecast URL from the points endpoint
        points_response = requests.get(f'https://api.weather.gov/points/{latitude},{longitude}').json()
        forecast_hourly_url = points_response['properties']['forecastHourly']

        # Fetch the hourly forecast data
        forecast_response = requests.get(forecast_hourly_url).json()

        # Extract and print the relevant forecast information
        out = ''
        out += 'Hour ● Short Forecast ● Temperature (F) ● Precipitation Probability\n\n'
        cur_day = None
        for day_index in range(7):
            day_forecast = forecast_response['properties']['periods'][day_index*24 : (day_index+1)*24]
            for period in day_forecast:
                time = period['startTime']

                dt_object = datetime.strptime(time, '%Y-%m-%dT%H:%M:%S%z')
                date = dt_object.strftime('%Y/%m/%d')
                hour = dt_object.strftime('%H')
                if date not in dates: continue
                if cur_day is None or cur_day != date: 
                    cur_day = date
                    out += '**' + dt_object.strftime('%A') + '**' + '\n'
                if int(hour) < forecast_hr_start or int(hour) > (forecast_hr_start + forecast_duration) % 24: continue

                short_forecast = period['shortForecast']
                temperature = period['temperature']
                temp_unit = period['temperatureUnit']
                precip_prob = period.get('probabilityOfPrecipitation', {}).get('value', 'N/A')
                
                out += f'{str(hour)} ● {short_forecast} ● {temperature} {temp_unit} ● {precip_prob}%\n'
                if (int(hour) == (forecast_hr_start + forecast_duration) % 24):
                    out += '\n'
        return out
    except:
        print('error getting weather....')
        return

def get_previous_sunday(tz):
    now = datetime.now(tz)
    last_sunday = now - timedelta(days=(now.weekday() + 1) % 7)
    last_sunday = last_sunday.replace(hour=0, minute=0, second=0, microsecond=0)
    return last_sunday

async def toggle_reaction(server_id, channel_id):
    print(f'Bot reacted on server: {server_id}')
    channel = bot.get_channel(channel_id)
    latest_msg = None
    async for msg in channel.history(limit=100):
        if msg.author == bot.user and msg.embeds: 
            latest_msg = msg
        
    # triggers weather update (toggle emoji off and on)
    if latest_msg: 
        emoji = '7️⃣'  
        # Remove existing reactions of the same emoji if any
        for reaction in latest_msg.reactions:
            if str(reaction.emoji) == emoji:
                await reaction.remove(bot.user)

        # Add the emoji as a reaction
        await latest_msg.add_reaction(emoji)

def get_row(server_id, channel_id):
    connection = sqlite3.connect('database.db')
    cursor = connection.cursor()

    cursor.execute(f'''
    SELECT * FROM update_info
    WHERE server_id = {server_id} AND channel_id = {channel_id}
    ''')

    row = cursor.fetchone()
    connection.close()

    return row


bot.run(TOKEN)