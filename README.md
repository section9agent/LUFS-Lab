# LUFS-Lab
========
Brings audio files to a target loudness (LUFS) with a true-peak limiter.
Built for music at -14 LUFS, with a voice/vocals mode, a batch "match" mode,
and WAV / FLAC / MP3 output. Runs on Windows, macOS and Linux.

QUICK START
-----------
Windows : install Python 3.10+ from python.org (tick "Add python.exe to PATH"),
          then double-click  run.bat
macOS   : install Python 3.10+ from python.org (it includes the window toolkit),
          then double-click  run.command
          (first time: right-click > Open, because the file was downloaded)
Linux   : needs python3 with tkinter. If the launcher says something is missing it
          tells you the exact command for your distribution (e.g.
          sudo apt install python3-tk python3-venv). Then run:  ./run.sh
          (if it says permission denied:  chmod +x *.sh run.command)

The first run downloads a few small packages into a private folder (.venv) next to the
program; later runs start immediately. Nothing is installed system-wide.

STAND-ALONE PROGRAMS (no Python needed afterwards)
--------------------------------------------------
Each system builds its own - a program can only be built on the kind of computer it is for.
  Windows : double-click  build_exe.bat   -> dist\LUFS-Lab.exe
  macOS   : run  ./build_mac.sh           -> dist/LUFS Lab.app  (+ a .zip)
            (runs on the same chip type you built on: Apple Silicon or Intel.
             The app is not signed: first launch, right-click > Open.)
  Linux   : run  ./build_linux.sh         -> dist/LUFS-Lab  and  dist/LUFS-Lab-<arch>.AppImage
            The AppImage is one file that runs on most distributions - just make it
            executable and double-click it. It works on distributions as new as, or
            newer than, the one you build on, so build on an older one for wider reach.
            ./install_linux_launcher.sh [path-to-AppImage]  adds it to your applications menu.
No Mac or Linux machine? Put this folder in a GitHub repository: the included workflow
(.github/workflows/build.yml) builds all of them on GitHub's servers - open the Actions
tab, run "Build LUFS Lab", and download the results.

USING IT
--------
1. Add files: drag audio files or folders into the window, or use "Add files..." /
   "Add folder..." (a folder adds the audio files directly inside it).
2. Choose Music or Voice / vocals.
     Music : one clean gain change to the target, then the true-peak limiter.
             Dynamics are left alone.
     Voice : first evens out loud and quiet passages (Gentle / Medium / Strong),
             ignoring silence and breaths, then hits the target.
3. Target loudness (default -14) and true-peak ceiling (default -1.0 dBTP).
4. Optional - "Match the volume of all files to each other" (2+ files):
   measures every file first, then brings them ALL to the batch's average
   loudness, so the whole set sits at one volume. The target box is not used then.
   (Without this box every file already goes to your target, so they also match each
   other - this option is for when you want the set's own average level instead.)
5. Pick where to save and the format, press Start. When it finishes a window tells you
   the result, lists any file that could not be processed and why, and offers to open
   the folder.

WHERE FILES GO (The original files will remain untouched)
  - Default: a new folder called "leveled" inside each file's own folder, with the
    original file names.
  - Or: next to each original with "_leveled" added to the name.
  - Or: one folder of your choice.

FROM A TERMINAL (optional, good for scripts)
  ./run.sh ~/Music/Album --match                        (Windows: py lufs_lab.py ...)
  ./run.sh song1.wav song2.flac --target -14 --beside
  ./run.sh vocal.wav --mode voice --strength medium -o ~/out --format "MP3 192 kbps"

SUPPORTED FILES
  In : WAV, FLAC, MP3, OGG, AIFF.
  Out: WAV or FLAC (16- or 24-bit), or MP3 at 128, 192 or 320 kbps.
  (M4A/AAC is not supported - convert to WAV/FLAC first.)
  Any length: audio is streamed in 30-second pieces, so long files do not fill RAM.

GOOD TO KNOW
  - MP3 is lossy, so the program checks the finished MP3 and, if encoding nudged the
    loudness or pushed a peak over your ceiling, quietly re-encodes until it lands
    (MP3 files take a bit longer than WAV/FLAC). For the best quality keep your
    master as WAV/FLAC and make MP3s only as the final step.
  - MP3 handles mono or stereo only (not 5.1) and sample rates up to 48 kHz; higher
    rates (88.2/96 kHz and up) are converted to 44.1/48 kHz automatically.
  - If a track is so peaky that reaching the level would need extreme limiting, it
    stops short and the log says so, rather than crushing the audio.
  - 16-bit output gets a touch of TPDF dither.
  - True-peak is measured by 4x oversampling with a sharp filter and kept 0.1 dB
    under your ceiling. Different meters can disagree by a few tenths of a dB.
  - Loudness follows ITU-R BS.1770-4 / EBU R128 and matched ffmpeg's ebur128 meter
    within ~0.01 LU in testing.
  - Drag-and-drop uses the tkinterdnd2 package. If it fails to load, the window
    still works with the buttons. (Windows blocks drops into programs started
    "as administrator" - run it normally. On Linux it works under X11 and XWayland.)

