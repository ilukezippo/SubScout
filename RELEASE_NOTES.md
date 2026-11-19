### What's new in v1.1
- **New Anime tab:** point it at your anime folder and it downloads Arabic/English subtitles per episode.
  - Understands fansub file names: `[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv`, `Show_012v2`, `Ep 12`, `#12`, and absolute numbers like `One Piece - 1100`
  - Cleans folder names such as `[Judas] Frieren (Season 2) [1080p]`, and takes the season from the folder (`Season 2`, `S2`, `2nd Season`)
  - Keeps styled **.ass** fansubs (saved as `.ar.ass` / `.en.ass`) and picks the right episode out of season packs
- **All tabs:** existing `.ass` / `.ssa` subtitles now count as "already has subtitles", and `.ass` files are used when a site has no `.srt`

If you're on v1.0, SubScout will offer this update when it starts. Click **Update now**.

### Download & run
1. Download **SubScout.exe** below. It's a single file and needs no Python or install.
2. Double-click it. The first time, Windows may show *"Windows protected your PC"* because the exe isn't code-signed: click **More info → Run anyway**.
3. Open **⚙ Settings → Sources** to add your free API keys (links are in the window).

Works on Windows 10 and 11 (64-bit).
