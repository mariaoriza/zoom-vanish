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

1. **Open "Zoom (Vanish)"** from your app menu. It turns on Vanish Cam and
   opens Zoom. When you quit Zoom, Vanish Cam stops and the camera light goes
   off. (If Zoom only hides to the tray when you close its window, use its
   Quit option.)

2. **First time only:** in Zoom, Settings > Video > Camera > **Vanish Cam**.
   Zoom remembers it. When you open plain Zoom instead, Vanish Cam doesn't
   exist and Zoom falls back to your normal webcam.

3. **Room photos are per place.** After about a second and a half (time for
   the webcam to adjust its exposure), it compares what the camera sees with
   your saved room photos and uses the matching one. If none matches, a
   notification asks you to press **Ctrl+Alt+C** and step out of frame during
   the 5-second countdown. Best done before joining a call, since people would
   see you walk away. Capturing again in the same place replaces that place's
   old photo.

4. **Vanish:** wave goodbye, or press **Ctrl+Alt+V**. Same again to come back.

| Shortcut | Action |
|---|---|
| Ctrl+Alt+V | vanish / appear |
| Ctrl+Alt+C | capture the room for this place |

setup.sh creates both shortcuts on GNOME and XFCE. On other desktops, bind
`~/.local/bin/zoom-vanish --toggle` and `--capture` yourself.
`zoom-vanish --quit` stops it.

Messages appear as desktop notifications; details go to
`~/.local/share/zoom-vanish/zoom-vanish.log`.

### Zoom virtual backgrounds and filters

Zoom applies its own effects on top of whatever Vanish Cam sends, so you can
keep using them. With a virtual background on, vanishing leaves just the
virtual background, and the room photo never shows. During the dust
animation, Zoom's background detection may handle the particles unevenly.
Check it in Zoom's video preview first.

### Running it from a terminal

For testing or tuning, run `~/.local/bin/zoom-vanish --wave` (Zoom closed
first, so the webcam is free). Then these keys work in the terminal:

| Key | Action |
|---|---|
| `c` | capture the room |
| `v` / space | vanish / appear |
| `d` | dust effect on / off |
| `w` | goodbye-wave detection on / off |
| `s` | finger-snap detection on / off |
| `q` | quit |

If your webcam isn't `/dev/video0`, add `--camera /dev/video2`
(`v4l2-ctl --list-devices` lists them). To make the menu entry use it too,
edit the `Exec=` line in `~/.local/share/applications/zoom-vanish.desktop`.

### Waving goodbye

On by default from the menu entry. Wave at the camera like saying
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
- If the camera moves or the lighting changes a lot after the room photo, it
  may not find you, and falls back to a plain fade. Press Ctrl+Alt+C to
  update that place's photo.
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

### How it recognises a place

It compares the top of the picture and the left and right edges (where
background shows even with you sitting there) with each saved photo, after
evening out brightness. Two places that look nearly identical, such as two
plain walls of the same color, can be mistaken for each other. Up to 20 places
are kept; the oldest is forgotten after that.

## Uninstall

```
rm -rf ~/.local/share/zoom-vanish ~/.local/bin/zoom-vanish
rm ~/.local/share/applications/zoom-vanish.desktop
sudo rm /etc/modprobe.d/zoom-vanish.conf /etc/modules-load.d/zoom-vanish.conf
sudo apt remove v4l2loopback-dkms v4l2loopback-utils
```

Then delete the two shortcuts: on GNOME in Settings > Keyboard > View and
Customize Shortcuts > Custom Shortcuts, on XFCE in Settings > Keyboard >
Application Shortcuts.

## What it does and doesn't touch

Everything runs on your computer. No network access, no accounts. Room
photos are stored in `~/.local/share/zoom-vanish/rooms/`, readable only by
your user. The microphone is
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
