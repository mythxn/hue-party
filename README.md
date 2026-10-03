# hue-party

A tiny, dependency-free Python controller for Philips Hue lights (Hue API v2), with slow ambient "drift" modes for night lighting.

Hue bridge only. IKEA bulbs work if they've been added to your Hue bridge; there is also experimental, untested support for an IKEA DIRIGERA hub (`pair ikea`).

## Setup

```bash
./hue.py discover                 # find your bridge
./hue.py pair hue <bridge-ip>     # press the bridge's round button first
./hue.py lights
```

Pairing saves a key to `.hue.json` (git-ignored; keep it private).

## Usage

```bash
./hue.py on | off [NAME...]
./hue.py set [NAME...] -b 30 -c "#ff8800"      # or -k 2700 for white
./hue.py rename "old name" "new name"
./hue.py night --brightness 10 --period 20     # slowly shifting night palette
./hue.py lava --theme cyberpunk --min-brightness 40 --max-brightness 90 \
              --slowness 0.3 --interval 4 --dim ceiling
```

`lava` gives every light its own unsynced colour and brightness cycle. Themes: `lava`, `cyberpunk`. Use `--room NAME` to limit to a room and `--dim NAME...` to keep some lights dimmer.

Requires macOS's `/usr/bin/python3` or any Python 3.9+ that is allowed on your local network.
