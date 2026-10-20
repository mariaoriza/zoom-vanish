# Zoom Vanish

Vanish from a Zoom call and come back, on Debian. Optionally dissolve into dust
on the way out, and trigger it by waving goodbye or snapping your fingers.

Zoom's desktop app can't be modified like a browser tab, so this works one
level down: it runs as a **virtual camera**. Your real webcam goes into this
program, and Zoom watches the program's output.

```
webcam (/dev/video0) -> zoom-vanish -> "Vanish Cam" (/dev/video10) -> Zoom
```

When you vanish, the virtual camera shows a photo of your empty room with a
bit of grain so it doesn't look frozen. It also works for Google Meet, Teams,
or anything else that lets you pick a camera.

## Install (once)

```
cd ~/Downloads/zoom-vanish
bash setup.sh
```

It installs `v4l2loopback` (the virtual camera kernel module), a private Python
environment, and a `zoom-vanish` command. If it stops at the module step with a
Secure Boot message, follow the printed steps (one reboot) and run it again.

## Use

1. **Start it before Zoom grabs your webcam** (only one program can use the
   real camera):

   ```
   ~/.local/bin/zoom-vanish
   ```

   If your webcam isn't `/dev/video0`, pass it: `zoom-vanish --camera /dev/video2`
   (`v4l2-ctl --list-devices` lists them).

2. **In Zoom:** Settings > Video > Camera > **Vanish Cam**.

3. **Capture the empty room:** press `c` in the terminal and step out of frame
   during the 5-second countdown. Best done before joining the call, since
   people would see you walk away. The photo is saved and reused next time.
   Retake it if you move the camera or the light changes (lamp on/off, sunset).

4. **Vanish:** press `v` (or space). Press again to reappear.

| Key | Action |
|---|---|
| `c` | capture the empty room |
| `v` / space | vanish / appear |
| `d` | dust effect on / off |
| `w` | goodbye-wave detection on / off |
| `s` | finger-snap detection on / off |
| `q` | quit |

### A shortcut that works while Zoom is focused

The keys above only work while the terminal is focused. For a shortcut that
works anywhere, GNOME Settings > Keyboard > View and Customize Shortcuts >
Custom Shortcuts > add one:

- Name: `Vanish`
- Command: `/home/YOURUSER/.local/bin/zoom-vanish --toggle`
- Shortcut: for example Alt+Shift+V

(`--capture` works the same way for the room photo.)

### Waving goodbye

Start with `zoom-vanish --wave`, or press `w`. Wave at the camera like saying
goodbye: open hand, raised (not down by the desk), swinging left and right
at least three times. Wave again to come back. One long wave only counts
once; it re-arms after you stop.

It uses Google's MediaPipe hand model, which runs on your computer (no
internet needed after setup) about 15 times a second on a small copy of the
picture. What counts as a wave:

- the hand is open in at least 80% of the moments looked at (a fist doesn't count),
- three or more left-right swings in a row, each lasting about 0.1 to 0.55 s,
- swings at least about 4.5% of the picture width,
- not mostly up-and-down motion.

Tuning:

```
zoom-vanish --wave --wave-debug               # prints what it sees when you move your hand
zoom-vanish --wave --wave-sensitivity 1.5     # smaller waves count
zoom-vanish --wave --wave-sensitivity 0.7     # needs bigger waves (fewer accidents)
```

If you talk with your hands a lot, open-palm side-to-side gestures could set
it off. Test it before a real call.

### Finger snaps

Start with `zoom-vanish --snap`, or press `s`. It listens to your microphone
for a short, sharp, high-pitched burst. It's a heuristic, not a trained
model, so claps or knocks can also trigger it, and a quiet snap far from the
mic may not. Tune it:

```
zoom-vanish --snap --snap-debug                     # prints a score for loud sounds
zoom-vanish --snap --snap-sensitivity 1.5           # triggers more easily
zoom-vanish --snap --snap-sensitivity 0.7           # fewer false triggers
```

## How the dust effect finds you

No AI model: it compares each frame with the empty-room photo, and whatever
differs is you. That keeps it fast and offline, with these limits:

- Parts of you the same color as the wall behind you get no dust (they still
  disappear, just without particles).
- If the camera moves or the lighting changes after the room photo, it may not
  find you, and falls back to a plain fade. Retake the photo.
- Webcam auto-exposure can shift the whole image when you leave the frame. If
  the room photo looks brighter or darker than the live picture, lock
  exposure: `v4l2-ctl -d /dev/video0 --list-ctrls` shows the names; usually
  `v4l2-ctl -d /dev/video0 -c auto_exposure=1`.
- `--threshold` (default 26): raise it if random background spots turn to dust,
  lower it if parts of you are missed.

## Options

```
zoom-vanish --help
```

Turn triggers on at start with `--wave` and/or `--snap`. Others: `--width/--height/--fps` (default 1280x720 at 30; try
`--width 960 --height 540` if the dust animation stutters), `--duration`
(dust length, default 2.6 s), `--countdown`, `--no-dust`.

## Uninstall

```
rm -rf ~/.local/share/zoom-vanish ~/.local/bin/zoom-vanish
sudo rm /etc/modprobe.d/zoom-vanish.conf /etc/modules-load.d/zoom-vanish.conf
sudo apt remove v4l2loopback-dkms v4l2loopback-utils
```

## What it does and doesn't touch

Everything runs on your computer. No network access, no accounts. The room
photo is stored at `~/.local/share/zoom-vanish/room.png`. The microphone is
only opened when snap detection is on. Wave detection only looks at the
webcam picture the program already has; the hand model is downloaded once by
setup.sh from Google and then runs offline.

## Credits

Idea inspired by [deniza-png/snap](https://github.com/deniza-png/snap), a
Google Meet browser extension. This is an independent implementation for
desktop video apps on Linux; no code was copied.

Hand tracking uses Google's [MediaPipe](https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer)
(Apache 2.0), downloaded during setup.

## License

MIT. See [LICENSE](LICENSE).
