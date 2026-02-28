import os
import subprocess
import yt_dlp
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, MessageHandler, filters
from telegram.error import TelegramError
import re
from urllib.parse import urlparse

# API Token for the bot (obtained from @BotFather)
API_TOKEN = '7922399482:AAEcO0_YR3Zlicz5RF_0YzzvTyFnOQxfpgk'

# Temporary download path
TEMP_DOWNLOAD_FOLDER = r'./downloads'

# Cookie filename stored by the bot (inside TEMP_DOWNLOAD_FOLDER)
COOKIES_FILENAME = 'cookies.txt'

# Telegram size limit (50 MB)
TELEGRAM_MAX_SIZE_MB = 50
# Telegram caption max length
TELEGRAM_CAPTION_MAX = 1024

# Function to handle real-time download progress
async def download_progress(d, message):
    if d['status'] == 'downloading':
        percentage = d.get('downloaded_bytes', 0) / d.get('total_bytes', 1) * 100
        # Update the progress by editing the same message
        if int(percentage) % 10 == 0:  # Update every 10% to avoid too many edits
            await message.edit_text(f"Download progress: {percentage:.2f}%")
    elif d['status'] == 'finished':
        await message.edit_text("Download complete, processing file...")

# Function to download videos or audios (YouTube, Twitter/X, and TikTok)
async def download_video(url, destination_folder, message, format="video"):
    try:
        # Determine the format
        if format == "audio":
            format_type = 'bestaudio/best'
            ext = 'mp3'
        else:
            format_type = 'best'
            ext = 'mp4'

        # yt-dlp configuration with progress_hooks
        options = {
            'outtmpl': f'{destination_folder}/%(id)s.%(ext)s',  # Use the video ID to avoid filename issues
            'format': format_type,  # Select the format based on user input
            'restrictfilenames': True,  # Limit special characters
            'progress_hooks': [lambda d: asyncio.create_task(download_progress(d, message))],  # Hook to show real-time progress
        }

        # If cookies are provided via env var or stored file, add to options
        cookie_env = os.environ.get('YT_DLP_COOKIES')
        cookie_candidates = []
        if cookie_env:
            cookie_candidates.append(cookie_env)
        cookie_candidates.append(os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME))
        cookie_candidates.append(os.path.join(TEMP_DOWNLOAD_FOLDER, 'cookies.json'))
        for cpath in cookie_candidates:
            try:
                if cpath and os.path.exists(cpath):
                    # Normalize cookie file (some exporters use spaces or '#HttpOnly_' prefixes)
                    norm_path = os.path.join(destination_folder, 'cookies_normalized.txt')
                    try:
                        normalize_cookiefile(cpath, norm_path)
                        options['cookiefile'] = norm_path
                        print(f"Using cookiefile (normalized): {norm_path}")
                        break
                    except Exception:
                        # fallback to original
                        options['cookiefile'] = cpath
                        print(f"Using cookiefile: {cpath}")
                        break
            except Exception:
                continue

        # Download the video or audio and save metadata (like tweet text) to a .txt file
        with yt_dlp.YoutubeDL(options) as ydl:
            # Extract info first to get metadata without downloading
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                info = None

            # Perform the actual download
            ydl.download([url])

        # If metadata was available, save the description/text to a .txt file
        try:
            if info and 'id' in info:
                description = info.get('description') or info.get('fulltitle') or ''
                if description:
                    txt_path = os.path.join(destination_folder, f"{info['id']}.txt")
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(description)
        except Exception as e:
            print(f"Warning: could not write metadata file: {e}")

        return True
    except Exception as e:
        print(f"Error during download: {e}")
        return False


def find_supported_url(text: str):
    """Return the first supported URL found in text, or None."""
    if not text:
        return None
    # Find all http(s) URLs
    urls = re.findall(r'https?://[^\s]+', text)
    for u in urls:
        # Strip surrounding punctuation
        u_clean = u.strip('"\'"').rstrip('.,;:!?)]}')
        try:
            p = urlparse(u_clean)
            host = p.netloc.lower()
        except Exception:
            continue
        # Accept known domains (youtube, youtu.be, twitter/x, tiktok, instagram)
        if host.endswith('youtube.com') or host == 'youtu.be' or host.endswith('twitter.com') or host.endswith('x.com') or host.endswith('tiktok.com') or host.endswith('instagram.com') or host.endswith('instagr.am'):
            return u_clean
    return None


def extract_url_from_update(update: Update):
    """Try to extract a supported URL from the Telegram Update (text, caption, or entities)."""
    # Try plain text/caption first
    text = update.message.text or ''
    url = find_supported_url(text)
    if url:
        return url

    caption = getattr(update.message, 'caption', None) or ''
    url = find_supported_url(caption)
    if url:
        return url

    # Try entities in text
    entities = getattr(update.message, 'entities', None) or []
    for ent in entities:
        try:
            if ent.type == 'text_link' and getattr(ent, 'url', None):
                candidate = ent.url
            elif ent.type == 'url' and text:
                candidate = text[ent.offset: ent.offset + ent.length]
            else:
                continue
        except Exception:
            continue
        candidate = candidate.strip('"\'"').rstrip('.,;:!?)]}')
        if find_supported_url(candidate):
            return candidate

    # Try caption_entities
    cap_ents = getattr(update.message, 'caption_entities', None) or []
    for ent in cap_ents:
        try:
            if ent.type == 'text_link' and getattr(ent, 'url', None):
                candidate = ent.url
            elif ent.type == 'url' and caption:
                candidate = caption[ent.offset: ent.offset + ent.length]
            else:
                continue
        except Exception:
            continue
        candidate = candidate.strip('"\'"').rstrip('.,;:!?)]}')
        if find_supported_url(candidate):
            return candidate

    return None

# Function to reduce video quality if it's too large using ffmpeg
def reduce_quality_ffmpeg(video_path, output_path, target_size_mb=50):
    try:
        # Command to reduce video quality using ffmpeg
        command = [
            'ffmpeg', '-i', video_path,
            '-b:v', '500k',  # Adjust the video bitrate (can be modified as needed)
            '-vf', 'scale=iw/2:ih/2',  # Reduce resolution by half
            '-c:a', 'aac',  # Encode audio with AAC
            '-b:a', '128k',  # Adjust the audio bitrate
            output_path
        ]

        # Execute the ffmpeg command
        subprocess.run(command, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error reducing video quality with ffmpeg: {e}")
        return False

# Function to handle the /start command
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text('Send a YouTube, Twitter/X, TikTok, or Instagram link (or just paste a link) and the bot will download it.\n'
                                    'If the file is larger than 50 MB, the quality will be reduced to send it.')


async def setcookies(update: Update, context: CallbackContext):
    await update.message.reply_text('To set cookies, upload your `cookies.txt` (or cookies.json) as a document with filename containing "cookie".\n'
                                    'You can also set environment variable `YT_DLP_COOKIES` to point to a cookies file on disk.')


async def removecookies(update: Update, context: CallbackContext):
    path = os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME)
    try:
        if os.path.exists(path):
            os.remove(path)
            await update.message.reply_text('Removed stored cookies.')
        else:
            await update.message.reply_text('No stored cookies were found.')
    except Exception as e:
        await update.message.reply_text(f'Error removing cookies: {e}')


async def handle_document(update: Update, context: CallbackContext):
    # Save uploaded cookies document if filename looks like a cookies file
    doc = update.message.document
    if not doc or not getattr(doc, 'file_name', None):
        return
    fname = doc.file_name.lower()
    if 'cookie' in fname or fname.endswith('.json') or fname.endswith('.txt'):
        os.makedirs(TEMP_DOWNLOAD_FOLDER, exist_ok=True)
        # If JSON, save as cookies.json then convert; otherwise save directly as cookies.txt
        try:
            file = await doc.get_file()
            if fname.endswith('.json'):
                json_path = os.path.join(TEMP_DOWNLOAD_FOLDER, 'cookies.json')
                await file.download_to_drive(custom_path=json_path)
                # Try to convert JSON -> Netscape cookies.txt
                try:
                    convert_json_cookies_to_netscape(json_path, os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME))
                    await update.message.reply_text(f'Converted and saved cookies to {os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME)}')
                except Exception as e:
                    await update.message.reply_text(f'Saved JSON cookies to {json_path} but failed to convert: {e}')
            else:
                save_path = os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME)
                await file.download_to_drive(custom_path=save_path)
                await update.message.reply_text(f'Saved cookies to {save_path}')
        except Exception as e:
            await update.message.reply_text(f'Failed to save cookies: {e}')
    else:
        # Not a cookies file; ignore or inform
        await update.message.reply_text('Document received but filename does not look like a cookies file.\n'
                                    'If this is your cookies file, include "cookie" in the filename or use /setcookies for instructions.')


def convert_json_cookies_to_netscape(json_path: str, out_path: str):
    """Convert a JSON cookie export (list or {'cookies': [...]}) to Netscape cookies.txt format.

    Expects typical browser-exported JSON cookie formats.
    """
    import json
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict) and 'cookies' in data:
        cookies = data['cookies']
    elif isinstance(data, list):
        cookies = data
    else:
        # Try common wrapper keys
        for k in ('cookie', 'cookies', 'items'):
            if k in data and isinstance(data[k], list):
                cookies = data[k]
                break
        else:
            raise ValueError('Unrecognized JSON cookie format')

    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get('domain') or c.get('host') or ''
        if domain.startswith('.'): 
            domain = domain
        flag = 'TRUE' if domain.startswith('.') else 'FALSE'
        path = c.get('path', '/')
        secure = 'TRUE' if c.get('secure') or c.get('isSecure') else 'FALSE'
        expiry = str(int(c.get('expiry') or c.get('expires') or 0))
        name = c.get('name') or c.get('key') or ''
        value = c.get('value') or c.get('val') or ''
        # Ensure domain field is not empty
        if not domain:
            continue
        lines.append('\t'.join([domain, flag, path, secure, expiry, name, value]))

    with open(out_path, 'w', encoding='utf-8') as out:
        out.write('\n'.join(lines))


def normalize_cookiefile(in_path: str, out_path: str):
    """Normalize cookie file into Netscape (tab-separated) format.

    Handles space-separated exports and removes '#HttpOnly_' prefixes when present.
    """
    import re
    with open(in_path, 'r', encoding='utf-8', errors='ignore') as inp:
        lines = inp.readlines()

    out_lines = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        # Keep header/comments
        if s.startswith('# Netscape') or s.startswith('# http') or s.startswith('# This file'):
            out_lines.append(s)
            continue
        # Remove leading '#HttpOnly_' used by some exporters
        s2 = re.sub(r'^#HttpOnly_[\.]?', '', s)
        # Remove accidental leading '#' before domain
        if s2.startswith('#'):
            s2 = s2.lstrip('#')
        parts = re.split(r'\s+', s2)
        # If already tab-separated, preserve
        if '\t' in ln and len(parts) >= 7:
            out_lines.append('\t'.join(parts))
            continue
        if len(parts) >= 7:
            out_lines.append('\t'.join(parts[:7]))
        else:
            # fallback: keep original line
            out_lines.append(s)

    with open(out_path, 'w', encoding='utf-8') as out:
        out.write('\n'.join(out_lines) + '\n')


async def checkcookies(update: Update, context: CallbackContext):
    """Check stored cookies for an Instagram sessionid entry."""
    candidates = [os.path.join(TEMP_DOWNLOAD_FOLDER, COOKIES_FILENAME), os.path.join(TEMP_DOWNLOAD_FOLDER, 'cookies.json')]
    found = False
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    txt = f.read()
                if 'sessionid' in txt.lower():
                    await update.message.reply_text(f'Found sessionid in {p}')
                    found = True
                    break
            except Exception:
                continue
    if not found:
        await update.message.reply_text('No sessionid found in stored cookies. Make sure you exported cookies in Netscape format while logged in to Instagram.')

# Function to handle the /download command with format options
async def download(update: Update, context: CallbackContext):
    try:
        # Extract the text sent in the message
        message_text = update.message.text or ''

        # Determine URL and format. Support both `/download <url>` and plain messages with a URL.
        url = None
        format = 'video'

        if message_text.strip().startswith('/download'):
            params = message_text.split()
            if len(params) >= 2:
                url = params[1].strip()
            if len(params) >= 3 and params[2].lower() == 'audio':
                format = 'audio'
        else:
            url = find_supported_url(message_text)
            if not url:
                # Fallback: try to extract from entities/caption
                url = extract_url_from_update(update)
            if 'audio' in message_text.split():
                format = 'audio'

        # Validate URL
        if not url:
            await update.message.reply_text('Please provide a valid YouTube, Twitter/X, or TikTok URL.')
            return

        destination_folder = TEMP_DOWNLOAD_FOLDER  # Use the temporary download folder
        os.makedirs(destination_folder, exist_ok=True)

        # Send the initial message and keep it for updates
        message = await update.message.reply_text(f'Starting the {format} download from: {url}')

        # Start the download and update the same message
        success_download = await download_video(url, destination_folder, message, format)

        if not success_download:
            await message.edit_text('Error during the video download. Please try again later.')
            return

        # Get the name of the downloaded media file (prefer media extensions, ignore .txt)
        media_exts = {'.mp4', '.mkv', '.webm', '.mov', '.flv', '.mp3', '.m4a', '.aac', '.wav'}
        all_files = [f for f in os.listdir(destination_folder)]
        media_files = [os.path.join(destination_folder, f) for f in all_files if os.path.splitext(f)[1].lower() in media_exts]
        if not media_files:
            # fallback: any file except .txt
            media_files = [os.path.join(destination_folder, f) for f in all_files if not f.lower().endswith('.txt')]
        if not media_files:
            # last resort: take any file
            media_files = [os.path.join(destination_folder, f) for f in all_files]

        if not media_files:
            await message.edit_text('Downloaded file not found.')
            return

        video_filename = max(media_files, key=os.path.getctime)

        # Check the file size
        file_size_mb = os.path.getsize(video_filename) / (1024 * 1024)
        if file_size_mb > TELEGRAM_MAX_SIZE_MB:
            await message.edit_text(f'The file is too large ({file_size_mb:.2f} MB). '
                                    f'Reducing the quality to meet the 50 MB limit...')

            # Attempt to reduce the quality using ffmpeg
            output_filename = os.path.join(destination_folder, 'compressed_' + os.path.basename(video_filename))
            success_reduce = reduce_quality_ffmpeg(video_filename, output_filename, TELEGRAM_MAX_SIZE_MB)

            if not success_reduce:
                await message.edit_text('Error reducing the video quality. Please try again later.')
                return

            # Switch to the compressed file for sending
            video_filename = output_filename

        # Send the video/audio file to the user
        await message.edit_text(f'Sending the {format}...')
        # If a .txt with the same id exists, use it as the caption
        caption = None
        try:
            txt_path = os.path.splitext(video_filename)[0] + '.txt'
            if os.path.exists(txt_path):
                with open(txt_path, 'r', encoding='utf-8') as tf:
                    caption = tf.read()
        except Exception as e:
            print(f"Warning reading caption file: {e}")

        # Truncate caption if too long for Telegram; send only up to TELEGRAM_CAPTION_MAX characters
        caption_to_send = None
        if caption:
            if len(caption) > TELEGRAM_CAPTION_MAX:
                caption_to_send = caption[:TELEGRAM_CAPTION_MAX]
            else:
                caption_to_send = caption

        try:
            with open(video_filename, 'rb') as vf:
                if format == 'audio':
                    await update.message.reply_audio(audio=vf, caption=caption_to_send if caption_to_send else None)
                else:
                    await update.message.reply_video(video=vf, caption=caption_to_send if caption_to_send else None)
        except TelegramError as e:
            await message.edit_text(f'Error sending the file: {e}')
            print(f"Error sending the file: {e}")
        finally:
            # Delete the downloaded media file and associated txt metadata
            try:
                if os.path.exists(video_filename):
                    os.remove(video_filename)
                txt_path = os.path.splitext(video_filename)[0] + '.txt'
                if os.path.exists(txt_path):
                    os.remove(txt_path)
            except Exception:
                pass

    except Exception as e:
        await update.message.reply_text('An unexpected error occurred. Please try again later.')
        print(f"Error in the download function: {e}")

# Main function to run the bot
def main():
    # Create the bot using ApplicationBuilder
    application = ApplicationBuilder().token(API_TOKEN).build()

    # Handled commands
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('download', download))
    application.add_handler(CommandHandler('setcookies', setcookies))
    application.add_handler(CommandHandler('removecookies', removecookies))
    # Handle plain text messages (non-commands) and auto-start download if they contain supported links
    async def _message_listener(update: Update, context: CallbackContext):
        # Only trigger when the message contains a supported domain
        text = update.message.text or ''
        if find_supported_url(text):
            await download(update, context)

    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), _message_listener))
    # Handle uploaded documents (cookies files)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()
