"""
audio_player.py

A simple command-line audio player for Raspberry Pi 4.
Plays .m4a files stored in the audio/ folder.

No classes, no threading you have to manage, no queues, no config
files - just plain variables, a couple of small functions, and a
while loop that reads commands from the terminal.

Playback is handled by VLC (via the python-vlc bindings). VLC plays
audio only - it never touches the microphone.

SONG command: asks for a song name, finds the first matching video on
youtube.com (via yt-dlp) and plays it in the Brave browser on a
virtual display (Xvfb), so it works on a Pi with no screen attached.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import quote_plus

import vlc

# yt-dlp is only used to find the first YouTube result for a song name.
# If it is not installed, SONG falls back to opening the YouTube search page.
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# --------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------

# Folder that holds all the audio files, next to this script.
AUDIO_DIR = Path("/home/events/Desktop/chatbot/audio_player/audio")

# Brave browser command (installed from Brave's apt repository).
BRAVE_CMD = "brave-browser"
# Separate Brave profile just for songs. This makes the song window its own
# browser process, so STOP can close it without touching any other Brave
# window, and it never interferes with your normal Brave profile.
BRAVE_PROFILE_DIR = "/home/events/.brave-songplayer"

# The Pi runs with NO screen attached, so Brave is started on a virtual
# display (Xvfb). Audio does not depend on the display, so the song still
# plays through the Pi's speaker. Needs:  sudo apt install xvfb
USE_VIRTUAL_DISPLAY = True
VIRTUAL_DISPLAY = ":99"

# The Brave process that is currently playing a song (None if none).
song_process = None
# The virtual display process (None until the first song).
xvfb_process = None

# One VLC instance and one media player, created once and reused for
# every track. `player` is the object STOP calls .stop() on.
vlc_instance = vlc.Instance()
player = vlc_instance.media_player_new()


def on_playback_end(event):
    """
    VLC calls this by itself the moment a track finishes on its own
    (this is VLC's internal mechanism, not something we build - we
    just tell it which function to call). It lets us print
    "Playback Finished." without blocking the command loop while
    waiting for the track to end, which would stop STOP from working
    immediately.
    """
    print("Playback Finished.")


# Tell VLC to call on_playback_end() when a track reaches its end.
player.event_manager().event_attach(vlc.EventType.MediaPlayerEndReached, on_playback_end)


# --------------------------------------------------------------------
# Playback helper
# --------------------------------------------------------------------

def play_audio(filename):
    """Load and play one audio file from the audio/ folder."""
    file_path = AUDIO_DIR / filename

    if not file_path.exists():
        print(f"Error: file not found -> {file_path}")
        return

    media = vlc_instance.media_new(str(file_path))
    player.set_media(media)
    player.play()
    print(f"Playing: {filename}")


# --------------------------------------------------------------------
# YouTube / Brave helpers (used by the SONG command)
# --------------------------------------------------------------------

def find_youtube_url(song_name):
    """
    Return the watch URL of the first YouTube result for song_name.
    Falls back to the YouTube search-results page if yt-dlp is missing
    or the lookup fails.
    """
    search_page = f"https://www.youtube.com/results?search_query={quote_plus(song_name)}"

    if yt_dlp is None:
        print("yt-dlp not installed - opening YouTube search page instead.")
        return search_page

    try:
        options = {"quiet": True, "no_warnings": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch1:{song_name}", download=False)
        entries = info.get("entries") or []
        if entries and entries[0].get("id"):
            return f"https://www.youtube.com/watch?v={entries[0]['id']}"
    except Exception as e:
        print(f"YouTube lookup failed ({e}) - opening search page instead.")

    return search_page


def start_virtual_display():
    """
    Start Xvfb (a virtual screen) if it is not already running.
    Returns True if the virtual display is ready, False otherwise.
    """
    global xvfb_process
    if xvfb_process is not None and xvfb_process.poll() is None:
        return True

    try:
        xvfb_process = subprocess.Popen(
            ["Xvfb", VIRTUAL_DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("Error: Xvfb not found. Install it with: sudo apt install xvfb")
        xvfb_process = None
        return False

    time.sleep(1)  # give the virtual display a moment to come up
    if xvfb_process.poll() is not None:
        print("Error: virtual display could not start "
              f"(is {VIRTUAL_DISPLAY} already in use?).")
        xvfb_process = None
        return False
    return True


def stop_virtual_display():
    """Shut down the virtual display (called when the program exits)."""
    global xvfb_process
    if xvfb_process is not None and xvfb_process.poll() is None:
        xvfb_process.terminate()
    xvfb_process = None


def stop_song():
    """Close the Brave window started by SONG (if one is open)."""
    global song_process
    if song_process is not None and song_process.poll() is None:
        try:
            # Brave starts several helper processes - close the whole group.
            os.killpg(os.getpgid(song_process.pid), signal.SIGTERM)
        except Exception:
            song_process.terminate()
        print("Song window closed.")
    song_process = None


def play_song(song_name):
    """Find the song on YouTube and play it in Brave (works with no screen)."""
    global song_process

    try:
        # Only one thing at a time: stop VLC audio and any previous song.
        player.stop()
        stop_song()

        print(f"Searching YouTube for: {song_name}")
        url = find_youtube_url(song_name)

        # Start every song with a fresh, empty Brave profile so old tabs are
        # never restored and no old keyring/session state is reused.
        shutil.rmtree(BRAVE_PROFILE_DIR, ignore_errors=True)

        env = os.environ.copy()
        # Audio session of this user - needed when started over SSH/service.
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

        if USE_VIRTUAL_DISPLAY:
            if not start_virtual_display():
                return
            env["DISPLAY"] = VIRTUAL_DISPLAY
        else:
            env.setdefault("DISPLAY", ":0")

        song_process = subprocess.Popen(
            [
                BRAVE_CMD,
                f"--user-data-dir={BRAVE_PROFILE_DIR}",
                "--new-window",
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run",
                "--no-default-browser-check",
                # Keep Brave's data key in a plain file instead of the system
                # keyring -> no "choose password for new keyring" prompt.
                "--password-store=basic",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                url,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # own process group so STOP closes all of it
        )

        # If Brave dies right away, say so instead of pretending it is playing.
        time.sleep(3)
        if song_process.poll() is not None:
            print("Error: Brave closed immediately. Run 'brave-browser --version' to check it.")
            song_process = None
            return

        print(f"Playing in Brave: {url}")

    except FileNotFoundError:
        print(f"Error: '{BRAVE_CMD}' not found. Install Brave first.")
        song_process = None
    except Exception as e:
        print(f"Could not play song: {e}")
        song_process = None


# --------------------------------------------------------------------
# Help text
# --------------------------------------------------------------------
def print_help():
    """Print all supported commands."""
    print("Available Commands:")
    print("  0      - Play openingSpeech.mpga")
    print("  1      - Play q1_answer.mpga")
    print("  2      - Play q2_answer.mpga")
    print("  3      - Play q3_answer.mpga")
    print("  4      - Play q4_answer.mpga")
    print("  5      - Play q5_answer.mpga")
    print("  6      - Play q6_answer.mpga")
    print("  SONG   - Ask for a song name and play it from YouTube in Brave")
    print("  STOP   - Stop the currently playing audio")
    print("  HELP   - Show this help message")
    print("  EXIT   - Exit the program")

# --------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------

print("Audio Player Ready.")

# To add a 5th audio file: drop the .m4a file in audio/, then add
#     elif command == "5":
#         play_audio("your_file.m4a")
# below, next to the existing commands.

try:
    while True:
        command = input("Command > ").strip().upper()

        if command == "0":
            play_audio("openingSpeech.mpga")
        elif command == "1":
            play_audio("q1_answer.mpga")
        elif command == "2":
            play_audio("q2_answer.mpga")
        elif command == "13":
            play_audio("song1.mp3")
        elif command == "14":
            play_audio("song2.mp3")
        elif command == "15":
            play_audio("song3.mp3")
        elif command == "6":
            play_audio("q6_answer.mpga")
        elif command == "SONG":
            song_name = input("Which song do you want to play? > ").strip()
            if song_name:
                play_song(song_name)
            else:
                print("No song name entered.")
        elif command == "HELP":
            print_help()
        elif command == "STOP":
            player.stop()
            stop_song()
            print("Stopped.")
        elif command == "EXIT":
            player.stop()
            stop_song()
            stop_virtual_display()
            print("Exiting Audio Player. Goodbye!")
            break
        else:
            print("Unknown command.")

except KeyboardInterrupt:
    # Ctrl+C - stop cleanly instead of showing an error traceback.
    player.stop()
    stop_song()
    stop_virtual_display()
    print("\nInterrupted. Exiting Audio Player. Goodbye!")